# Pico 2 W RFID Firmware

MicroPython firmware for a Raspberry Pi Pico 2 W + PN532 NFC/RFID reader
mounted on each train. Polls for RFID tags embedded at track block
boundaries and publishes reads over MQTT to the central Raspberry Pi, which
runs the `dispatcher` package in this repo. This firmware runs on the Pico's
own MicroPython interpreter -- it is not part of this repo's Python
package/pytest/CI, the same way `hubs/` is separate Pybricks firmware for
the LEGO hubs themselves.

## Wiring

Default pin mapping (override in `config.py` if wired differently):

| PN532 pin | Pico 2 W pin | config.py field |
|-----------|--------------|------------------|
| SDA       | GP4          | `I2C_SDA_PIN`    |
| SCL       | GP5          | `I2C_SCL_PIN`    |
| RSTO/RST  | GP22         | `PN532_RST_PIN`  |
| VCC       | 3V3          | --               |
| GND       | GND          | --               |

The PN532 V3 board has a bank of DIP switches that select its interface --
they must be set to **I2C mode** (switch 1 = ON, switch 2 = OFF) or the
Pico will never see it on the bus. `PN532_RST_PIN` is a hardware reset the
firmware pulses on every `build_reader()` call; without it the PN532 throws
`OSError EIO` on its first I2C write after power-on.

Power the Pico from the onboard 503450 LiPo (3.7V, 1000mAh) via a TP4056
charger into `VSYS`/`GND`. The firmware periodically samples `VSYS` itself
(see Battery monitoring below) so no extra wiring is needed to report
charge level.

## Flashing

1. Flash the latest stable MicroPython UF2 for Pico 2 W (hold BOOTSEL while
   plugging in, drag the UF2 onto the mounted drive).
2. Copy files onto the board with [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
   (or Thonny's file browser):

   ```bash
   cp pico/config.example.py pico/config.py
   # edit pico/config.py: WIFI_SSID/PASSWORD, MQTT_BROKER_HOST, TRAIN_ID, pins

   mpremote connect <port> mkdir :umqtt
   mpremote connect <port> cp pico/umqtt/__init__.py :umqtt/__init__.py
   mpremote connect <port> cp pico/umqtt/simple.py :umqtt/simple.py
   mpremote connect <port> cp pico/pn532.py :pn532.py
   mpremote connect <port> cp pico/config.py :config.py
   mpremote connect <port> cp pico/main.py :main.py
   ```

   `umqtt.simple` is **not** frozen into standard Raspberry Pi Pico/rp2
   MicroPython builds (unlike some ESP8266/ESP32 builds) -- it's vendored
   in `pico/umqtt/` and must be copied to the device as shown above, or
   you'll hit `ImportError: no module named 'umqtt'`. `ntptime` typically
   does ship with standard builds; if it's missing on yours, `main.py`
   already treats a failed NTP sync as non-fatal.
3. Reset the board (or `mpremote connect <port> reset`). `main.py` runs
   automatically on boot.

`pico/test_rfid.py` is a standalone wiring-verification script left over
from the previous MFRC522/SPI reader and has not been ported to the PN532 --
treat it as stale until it's updated.

## Per-train setup

Each physical train needs its own `config.py` (gitignored -- never commit
real WiFi credentials):

- `TRAIN_ID` must be unique per train (e.g. `"TRN-A"`, `"TRN-B"`) and match
  the `train_hub_mapping` entry configured server-side.
- `MQTT_TAG_TOPIC` / `MQTT_COMMAND_TOPIC` are derived from `TRAIN_ID` and
  must match the server's `mqtt_tag_topic_template` /
  `mqtt_command_topic_template` settings (defaults already agree).
- Double-check the I2C pin map matches your physical wiring, and that the
  PN532's DIP switches are set to I2C mode.

## Battery monitoring

`main.py` samples `VSYS` (the LiPo's voltage, via the Pico's onboard
divider on ADC3/GPIO29) every `BATTERY_READ_INTERVAL_MS` (default 30s) and
includes it as `battery_v` on every tag-read and status MQTT payload. It's
sampled on a slow cadence rather than every poll because GPIO29 is shared
with the CYW43 wireless chip's SPI CLK -- reading it too often would
interrupt the WiFi link. The dispatcher surfaces the latest reading per
train via `TrackModel.get_battery()` / the `/dispatcher/trains` endpoint.

## Verification

- Watch the onboard LED: flashing while connecting to WiFi, solid once
  connected, fast-flashing if WiFi connection is exhausted.
- `mpremote connect <port> repl` to watch connect/publish logs live.
- On the Pi: `mosquitto_sub -t 'train/+/tag'` should print a JSON line each
  time a tag passes under the reader, including a `battery_v` field.

## Troubleshooting

- **VS Code/Pylance shows `Import "umqtt.simple" could not be resolved`
  (or similar for `machine`, `network`, `ntptime`)**: expected — these
  modules only exist inside the MicroPython firmware on the device itself,
  not in a desktop Python environment. Harmless for running the code; the
  repo's `pyrightconfig.json` suppresses the warning for `hubs/` and `pico/`
  specifically (reload the VS Code window if it's still showing after
  pulling that file).
- **Board reboots repeatedly / watchdog resets**: the RP2350 hardware
  watchdog tops out around ~8.3 seconds -- if you raise
  `WATCHDOG_TIMEOUT_MS` above that, `machine.WDT()` will raise at startup.
  Keep it at or below the default.
- **`PN532 init failed` / `OSError EIO` at startup**: verify the DIP
  switches are set to I2C mode and that `PN532_RST_PIN` is wired -- the
  PN532 needs the hardware reset pulse `build_reader()` sends before its
  first successful I2C transaction after power-on.
- **No tag reads at all**: verify I2C wiring against the pin table above;
  an SDA/SCL swap is the most common mistake.
- **MQTT connects but reads never publish**: check that `TRAIN_ID` matches
  what the dispatcher's `train_hub_mapping` setting expects, and that the
  tag's UID has been added to the server-side track topology (a tag with an
  unrecognized UID is logged and ignored by the dispatcher).
- **MQTT auth failures**: confirm `MQTT_USERNAME`/`MQTT_PASSWORD` in
  `config.py` match the broker's configured credentials (or are both `None`
  if the broker requires no auth).
