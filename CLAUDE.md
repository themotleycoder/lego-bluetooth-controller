# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python service that controls LEGO Powered Up trains and switches over Bluetooth Low Energy (BLE), exposed via a FastAPI REST API. It's designed to run on a Raspberry Pi (or other Linux host with BLE hardware) since it shells out to `bluetoothctl`/`hcitool` and requires root for raw HCI commands.

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

### Two independent BLE communication paths

This codebase talks to LEGO hubs in **two different ways** — don't conflate them:

1. **Broadcast/observe protocol (the one actually used in production)** — `controllers/train_controller.py` and `controllers/switch_controller.py` communicate with hub-side code by sending raw BLE advertising packets via `hcitool cmd` subprocess calls (LEGO manufacturer ID `919` / `0x397`), and read hub status back from BLE advertisement manufacturer data captured by `servers/bluetooth_scanner.py`. No GATT connection is ever established for trains/switches. This is why the service needs `sudo` and direct HCI access rather than just BLE permissions.
2. **Direct GATT connection** — `servers/lego_service.py` is a separate, standalone interactive CLI tool that connects directly to a hub via `BleakClient` (GATT service `00001623-...`, characteristic `00001624-...`) and writes motor commands directly. It is not used by the FastAPI web service or `servers/main.py`; treat it as a separate legacy/experimental utility.

### Request flow (REST API → hub)

`webservice/train_service.py` (FastAPI app) → `servers/main.py::LegoController` (holds one `TrainController` and one `SwitchController`) → controller's `handle_command`/`send_command_with_retry` → internal `asyncio.Queue` → background `_process_commands`/`_execute_command` task → `hcitool` subprocess calls that advertise a manufacturer-data payload encoding the command on a specific broadcast "channel" (`command_channel`, aka hub ID).

### Status flow (hub → REST API)

`servers/bluetooth_scanner.py::BetterBleScanner` wraps `bleak.BleakScanner` with forced reset/cleanup/retry logic. Both controllers register a detection callback with it; the callback filters advertisements by device name (`"Train"` / `"Technic Hub"`) and manufacturer ID `919`, decodes the payload into `train_statuses`/`switch_statuses` dicts keyed by hub ID. These dicts back the `/connected/trains` and `/connected/switches` endpoints. A status entry is considered "connected" only if updated within the last 5 seconds.

### Hub-side code (`hubs/`) runs on a different runtime

Files in `hubs/` (`train_receiver.py`, `switch_receiver_motor.py`, `switch_receiver_dcmotor.py`) are **not** run by this Python service — they're uploaded to and run directly on the LEGO hub itself under **Pybricks MicroPython** (`from pybricks...` imports). They implement the other end of the broadcast/observe protocol: listening on a `COMMAND_CHANNEL`, decoding the same manufacturer-data payload format, driving motors, and broadcasting status back on a `STATUS_CHANNEL`. When changing the command/status wire format in `controllers/`, the corresponding `hubs/*.py` script must be updated (and re-flashed to the hub) to match. There are two switch hub variants because Motor (with rotation feedback) and DCMotor (open-loop, timed movement only) require different control strategies.

### RFID dispatcher (`dispatcher/`) and Pico firmware (`pico/`)

The dispatcher adds autonomous, block-protected train movement on top of the manual BLE control path above — RFID tags at track block boundaries let the system know where each train is, instead of relying purely on manual `/train` calls. See `dispatcher/CLAUDE.md` for how the dispatcher package and Pico firmware work internally.

### Configuration

`config.py` defines a single `pydantic-settings` `Settings` class (`.env`-driven, see `.env.example`) covering server host/port, API keys, CORS origins, logging, BLE timing/retry parameters, validation ranges (e.g. valid switch names/positions, power range), and dispatcher/MQTT settings (`mqtt_broker_host`/`port`/`username`/`password`, `dispatcher_enabled`, `dispatcher_watchdog_timeout`, `train_hub_mapping`). Access it via `get_settings()`; comma-separated fields (`api_keys`, `allowed_origins`, `valid_switch_names`, `valid_switch_positions`, `train_hub_mapping`) expose parsed `*_list`/`*_dict` properties — use those rather than splitting the raw string yourself.

### Auth & rate limiting

`middleware/auth.py` implements API-key auth via an `X-API-Key` header (`APIKeyHeader` + `verify_api_key`), toggled by `settings.require_auth`. Every mutating endpoint in `webservice/train_service.py` calls `await verify_api_key(api_key)` explicitly inside the handler (in addition to declaring the `Depends`) — follow that pattern for new endpoints. Endpoints are also decorated with `slowapi` `@limiter.limit(...)`; `/health` is the only endpoint that skips auth.

### Tests

`tests/conftest.py` sets required env vars before any app import and provides fixtures that mock BLE entirely (`mock_bluetooth_scanner`, `mock_lego_controller`) plus a FastAPI `TestClient`/`AsyncClient`. Tests never touch real Bluetooth hardware — follow the existing mocking pattern (patch `servers.bluetooth_scanner.BetterBleScanner` / `webservice.train_service.LegoController`) when adding new tests. `tests/test_dispatcher/` covers the dispatcher package the same way, with MQTT fully mocked (patch `paho.mqtt.client.Client`) — see `dispatcher/__main__.py`'s `FakeMqttBridge`/stub controllers for the same pattern used outside pytest.

**Known pre-existing issue:** running the full suite together (`pytest`, no `-k`) can hang on macOS partway through `tests/test_api.py`. Root cause: `conftest.py`'s `mock_lego_controller` fixture patches `webservice.train_service.LegoController`, but the module-level `controller = LegoController()` already ran with the *real* class on first import — the patch is too late to affect it, so later tests drive a real `BetterBleScanner`/`BleakScanner` against actual CoreBluetooth, which deadlocks a scanner lock during shutdown. This reproduces identically on unmodified code and is unrelated to the dispatcher; it likely stays green in CI because Linux fails those same calls fast instead of hanging. Prefer running individual test files/dirs (e.g. `pytest tests/test_dispatcher/ tests/test_config.py tests/test_auth.py`) until this is fixed.
