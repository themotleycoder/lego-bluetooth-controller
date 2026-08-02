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

# --- RC522 SPI wiring ---
# Defaults below match the standard RP2040/RP2350 SPI0 pin group.
SPI_ID = 0
SPI_SCK_PIN = 6
SPI_MOSI_PIN = 7
SPI_MISO_PIN = 4
RST_PIN = 22
CS_PIN = 5

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
