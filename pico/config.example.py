# Per-train configuration for the Pico 2 W RFID firmware.
#
# Copy this file to config.py (gitignored) on each physical train's Pico and
# fill in real values. Every train needs its own config.py with a unique
# TRAIN_ID; MQTT_TAG_TOPIC/MQTT_COMMAND_TOPIC below must match the templates
# configured server-side in the dispatcher's Settings
# (mqtt_tag_topic_template / mqtt_command_topic_template).

# --- WiFi ---
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"
WIFI_CONNECT_RETRIES = 10
WIFI_RETRY_DELAY_MS = 1000

# --- MQTT ---
MQTT_BROKER_HOST = "192.168.1.10"  # IP of the Raspberry Pi running Mosquitto
MQTT_BROKER_PORT = 1883
MQTT_KEEPALIVE = 60
MQTT_USERNAME = None  # set to a string to enable broker auth
MQTT_PASSWORD = None
MQTT_RECONNECT_DELAY_MS = 2000

# --- Train identity ---
TRAIN_ID = "TRN-A"  # unique per physical train, e.g. "TRN-A", "TRN-B"

# MQTT topics -- must match config.py's mqtt_tag_topic_template /
# mqtt_command_topic_template on the server (defaults shown below).
MQTT_TAG_TOPIC = "train/{}/tag".format(TRAIN_ID)
MQTT_COMMAND_TOPIC = "train/{}/command".format(TRAIN_ID)

# --- PN532 NFC reader (I2C) ---
# DIP switches on the PN532 V3 board must be set to I2C mode
# (switch 1 = ON, switch 2 = OFF).
I2C_ID = 0
I2C_SDA_PIN = 4  # GP4 -- I2C0 SDA (alternate pin)
I2C_SCL_PIN = 5  # GP5 -- I2C0 SCL (alternate pin)
I2C_FREQ = 400000
PN532_RST_PIN = 22  # GP22 -- required for cold-boot init
PN532_READ_TIMEOUT_MS = 200  # list_passive_target timeout per poll

# --- Timing ---
RFID_POLL_INTERVAL_MS = 100
# Consecutive empty reads required before the same tag can re-trigger a
# publish; passive RFID reads flicker, so this avoids re-publishing the same
# tag on a single momentary dropout.
RFID_CLEAR_AFTER_MISSES = 3

# --- Watchdog ---
# machine.WDT cannot be disarmed once armed on the RP2350, so it's only
# armed after the first successful WiFi connect (see main.py).
WATCHDOG_TIMEOUT_MS = 8000

# --- Battery monitoring ---
# LiPo (503450, 3.0V empty - 4.2V full) via VSYS. Reading it briefly
# interrupts the CYW43 wireless chip's SPI bus (see read_vsys_voltage in
# main.py), so it's sampled on this slower cadence, not every poll.
BATTERY_READ_INTERVAL_MS = 30000
