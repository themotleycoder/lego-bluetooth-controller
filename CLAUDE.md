# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python service that controls LEGO Powered Up trains and switches over Bluetooth Low Energy (BLE), exposed via a FastAPI REST API. It's designed to run on a Raspberry Pi (or other Linux host with BLE hardware). Both trains and switches are now controlled via direct GATT connections over BlueZ/D-Bus (through `bleak`), not raw HCI — the process itself doesn't need to run as root, though `servers/bluetooth_scanner.py` still shells out to `sudo bluetoothctl power off/on` to reset the adapter before each scan, which needs a passwordless sudoers rule (see `RASPBERRY_PI_DEPLOY.md`).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Format code (CI enforces this — run before committing)
black .

# Run the full test suite (with coverage, per pytest.ini)
pytest

# Run a single test file / test
pytest tests/test_api.py
pytest tests/test_api.py::TestTrainEndpoints::test_train_power_with_valid_auth

# Run the REST API locally (requires PYTHONPATH set to repo root, and sudo for BLE)
sudo PYTHONPATH=. uvicorn webservice.train_service:app --host 0.0.0.0 --port 8000 --reload

# Run the interactive CLI controller instead of the REST API
sudo python3 servers/main.py

# Run the RFID dispatcher standalone, no MQTT broker or BLE hardware needed
python -m dispatcher --mock
python -m dispatcher --mock --duration 10   # auto-exit after N seconds
```

There are VSCode launch configs (`.vscode/launch.json`) for both entry points (`servers/main.py` and the uvicorn web service).

CI (`.github/workflows/ci.yml`) runs `black --check .`, then `pytest --cov=.`, and fails if coverage drops below 70%.

## Architecture

### Two independent GATT-based communication paths

This codebase talks to LEGO hubs in **two different ways** — both are direct GATT connections via `BleakClient` now, but to different services/protocols — don't conflate them:

1. **Pybricks GATT connection (switches)** — `controllers/switch_controller.py` connects to each switch hub's Pybricks GATT service (`PYBRICKS_SERVICE`) and talks the same stdin/stdout protocol `pybricksdev run` uses to drive a running user program: switch commands are `WRITE_STDIN` writes (2-byte payload `[switch_num(1-4), position(0/1)]`) and status arrives via `WRITE_STDOUT` notifications on `PYBRICKS_COMMAND_EVENT_CHAR`, decoded by `hubs/switch_receiver_*.py`'s binary status frame (`[status_byte, port_connections]`). This replaced an older broadcast/observe protocol (raw `hcitool` advertising packets, no GATT) — if you find references to that elsewhere (old comments, `LEGO_MANUFACTURER_IDS` in `utils/constants.py`), they're vestigial. Switch hubs run **Pybricks MicroPython** firmware (see `hubs/switch_receiver_*.py` below) and advertise a *rotating* BLE address that changes on every power-on, so hubs are identified by a stable advertised name (`switch-<hub_id>`, e.g. `"switch-4"`) parsed out of the advertisement, not by address — hub_id stays an integer throughout, unlike trains (see below).
2. **Direct GATT connection (trains)** — `controllers/train_controller.py` connects directly to each train hub via `BleakClient` (GATT service `00001623-...`, characteristic `00001624-...`), the same protocol `servers/lego_service.py` (a separate, standalone interactive CLI tool, not used by the FastAPI web service) has always used. Train hubs run **stock LEGO Powered Up firmware**, not Pybricks, and keep a fixed public BLE address (unlike Pybricks switch hubs), so they're identified by that address rather than a name. `TrainController` has no BLE scanner of its own, and does not connect by bare address either — on Linux, BlueZ only allows one active discovery ("StartDiscovery") session per D-Bus client, and bleak's `BleakClient.connect()` triggers an *implicit* discovery scan internally whenever the target address isn't already cached by BlueZ, which collides with `SwitchController`'s already-running continuous scan (`[org.bluez.Error.InProgress]`, not a transient race — it never recovers on its own since the switch scan never stops). Instead, `SwitchController.set_device_seen_callback` (wired up in `LegoController.__init__`) forwards every device its scan sees to `TrainController.handle_device_seen`, which connects configured train hubs using the already-resolved `BLEDevice` object — no second discovery ever happens. Connections are persistent `BleakClient`s per BLE address (reconnecting whenever `handle_device_seen` next reports a disconnected hub), and motor commands are `write_gatt_char` calls encoding a LEGO Wireless Protocol 3.0 payload. Trains are identified throughout by their **BLE address** (e.g. `"90:84:2B:18:28:36"`, from `Settings.train_hub_mapping`), not an integer hub ID — this differs from switches, which are addressed by integer hub ID (parsed from the advertised name, see above).

### Request flow (REST API → hub)

`webservice/train_service.py` (FastAPI app) → `servers/main.py::LegoController` (holds one `TrainController` and one `SwitchController`) → controller's `handle_command`/`send_command_with_retry` → either:
- **switches**: `SwitchController._send_command` writes a `WRITE_STDIN`-wrapped command to the connected hub's `PYBRICKS_COMMAND_EVENT_CHAR`, then `_verify_switch_position` polls `switch_statuses` (populated by the hub's `WRITE_STDOUT` status notifications) until the position matches or it times out, or
- **trains**: writes a motor-power GATT command directly to the train hub's persistent `BleakClient` connection.

### Status flow (hub → REST API)

- **Switches**: `servers/bluetooth_scanner.py::BetterBleScanner` wraps `bleak.BleakScanner` with forced reset/cleanup/retry logic and is now a thin, generic scanner — it does no LEGO-specific filtering itself. `SwitchController.handle_device_seen` (registered as the scan callback) does the filtering: it matches the Pybricks service UUID and the advertised `switch-<hub_id>` name pattern, then connects via GATT (see above) rather than reading status from advertisements. Live status comes from `WRITE_STDOUT` notifications decoded into a `switch_statuses` dict keyed by hub ID. A status entry is considered "connected" only if updated within the last 5 seconds.
- **Trains**: `TrainController` has no scanner of its own; it learns about hubs via `handle_device_seen`, fed by `SwitchController`'s scan (see above), and subscribes to GATT notifications (`start_notify`) on each connected hub for liveness. `get_connected_trains()` returns a dict keyed by BLE address with connection state (`connected`/`connecting`/`disconnected`/`error`), name, and last-activity time (`rssi` is currently always `None` -- it would need to come from `handle_device_seen`'s advertisement data, which isn't wired up).

Both back the `/connected/trains` and `/connected/switches` endpoints.

### Hub-side code (`hubs/`) runs on a different runtime

Files in `hubs/` (`switch_receiver_motor.py`, `switch_receiver_dcmotor.py`) are **not** run by this Python service — they're downloaded to and run directly on the LEGO Technic Hub itself under **Pybricks MicroPython** (`from pybricks...` imports), via `pybricksdev run ble --name switch-<hub_id> hubs/switch_receiver_*.py` (the same download mechanism persists across power cycles — no separate "flash" step). They implement the other end of the Pybricks stdin/stdout protocol described above: polling `stdin` for the 2-byte command frame, driving the matching motor, and writing the 2-byte status frame to `stdout` — **never** `print()`, since Pybricks routes `print()` through the same `stdout` stream the binary status protocol uses, which would interleave and corrupt it (use `hub.light` for on-hub diagnostics instead). `switch_receiver_motor.py` drives each motor with `run_until_stalled` into the switch mechanism's actual physical end-stop (calibrating against the STRAIGHT end-stop at startup) rather than a fixed `run_target` angle — a fixed angle reliably reached that encoder position without the physical switch actually completing its throw, since the real throw range depends on the gear train and doesn't map to a clean guessed angle; driving into the end-stop is self-calibrating and immune to that. When changing the switch command/status wire format in `controllers/switch_controller.py`, the corresponding `hubs/switch_receiver_*.py` script must be updated (and re-downloaded to the hub) to match. There are two switch hub variants because Motor (with rotation feedback, so it can `run_until_stalled`) and DCMotor (open-loop, timed movement only) require different control strategies. `hubs/train_receiver.py` is **legacy/unused** — train hubs were switched from Pybricks to stock firmware, so trains are now controlled over GATT (see above) and nothing uploads or runs this file anymore.

### RFID dispatcher (`dispatcher/`) and Pico firmware (`pico/`)

The dispatcher adds autonomous, block-protected train movement on top of the manual BLE control path above — RFID tags at track block boundaries let the system know where each train is, instead of relying purely on manual `/train` calls. See `dispatcher/CLAUDE.md` for how the dispatcher package and Pico firmware work internally.

### Configuration

`config.py` defines a single `pydantic-settings` `Settings` class (`.env`-driven, see `.env.example`) covering server host/port, API keys, CORS origins, logging, BLE timing/retry parameters, validation ranges (e.g. valid switch names/positions, power range), and dispatcher/MQTT settings (`mqtt_broker_host`/`port`/`username`/`password`, `dispatcher_enabled`, `dispatcher_watchdog_timeout`, `train_hub_mapping`). Access it via `get_settings()`; comma-separated fields (`api_keys`, `allowed_origins`, `valid_switch_names`, `valid_switch_positions`, `train_hub_mapping`) expose parsed `*_list`/`*_dict` properties — use those rather than splitting the raw string yourself.

### Auth & rate limiting

`middleware/auth.py` implements API-key auth via an `X-API-Key` header (`APIKeyHeader` + `verify_api_key`), toggled by `settings.require_auth`. Every mutating endpoint in `webservice/train_service.py` calls `await verify_api_key(api_key)` explicitly inside the handler (in addition to declaring the `Depends`) — follow that pattern for new endpoints. Endpoints are also decorated with `slowapi` `@limiter.limit(...)`; `/health` is the only endpoint that skips auth.

### Tests

`tests/conftest.py` sets required env vars before any app import and provides fixtures that mock BLE entirely (`mock_bluetooth_scanner`, `mock_lego_controller`) plus a FastAPI `TestClient`/`AsyncClient`. Tests never touch real Bluetooth hardware — follow the existing mocking pattern (patch `servers.bluetooth_scanner.BetterBleScanner`; `mock_lego_controller` patches the already-constructed `webservice.train_service.controller` singleton directly, not the `LegoController` class — see that fixture's docstring for why) when adding new tests. `tests/test_dispatcher/` covers the dispatcher package the same way, with MQTT fully mocked (patch `paho.mqtt.client.Client`) — see `dispatcher/__main__.py`'s `FakeMqttBridge`/stub controllers for the same pattern used outside pytest.
