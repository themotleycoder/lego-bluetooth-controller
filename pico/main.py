"""
Pico 2 W RFID tag-reporting firmware.

Polls a PN532 NFC reader over I2C and publishes tag reads to MQTT for the
central dispatcher to consume. Also subscribes to a per-train command topic,
but for now only logs received commands -- actual train control still goes
through the existing LEGO hub BLE path on the central Pi, not the Pico.
"""

import time

import machine
import network
import ujson as json
from umqtt.simple import MQTTClient

import config
from pn532 import PN532_I2C

led = machine.Pin("LED", machine.Pin.OUT)

_last_uid = None
_miss_count = 0
_pending_tag = None  # (tag_uid, timestamp) buffered after a failed publish
_wdt = None
_battery_voltage = 0.0
_last_battery_read_ms = 0
# Fallback matches config.example.py -- getattr so devices with an older
# config.py (missing this field) degrade gracefully instead of crashing.
_BATTERY_READ_INTERVAL_MS = getattr(config, "BATTERY_READ_INTERVAL_MS", 30000)


def read_vsys_voltage():
    """
    Read VSYS voltage on Pico W / Pico 2 W.

    GPIO25 gates the FET between the VSYS voltage divider and GPIO29.
    Must briefly reconfigure pins, take the reading, then restore them
    so the CYW43 wireless chip continues working -- GPIO29/ADC3 is shared
    with the wireless chip's SPI CLK, so reading it while WiFi is active
    without this dance returns garbage. Called on a slow cadence (see
    _BATTERY_READ_INTERVAL_MS), not every loop iteration, since the pin
    reconfiguration briefly interrupts the wireless SPI bus.
    """
    pin25 = machine.Pin(25, machine.Pin.OUT)
    pin25.value(1)  # Enable the VSYS voltage divider FET gate
    machine.Pin(29, machine.Pin.IN)  # Set as input, no pull

    adc = machine.ADC(3)  # ADC channel 3 = GPIO29
    raw = adc.read_u16()

    pin25.value(0)  # Restore so wireless chip SPI keeps working

    # 16-bit ADC -> voltage, then x3 to undo the onboard divider
    vsys = (raw / 65535) * 3.3 * 3
    return round(vsys, 2)


def _blink(times, on_ms=100, off_ms=100):
    """Flash the onboard LED `times` times."""
    for _ in range(times):
        led.on()
        time.sleep_ms(on_ms)
        led.off()
        time.sleep_ms(off_ms)


def connect_wifi():
    """Connect to WiFi, flashing the LED while connecting. Returns True on success."""
    wlan = network.WLAN(network.STA_IF)

    if wlan.isconnected():
        led.on()
        return True

    # Full reset of the wireless chip to avoid wedged state on cold boot
    wlan.active(False)
    time.sleep(1)
    wlan.active(True)
    time.sleep(1)

    print("Connecting to WiFi: {}".format(config.WIFI_SSID))
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

    for _attempt in range(config.WIFI_CONNECT_RETRIES):
        status = wlan.status()
        if status < 0:
            print("WiFi error, status:", status)
            break
        if wlan.isconnected():
            print("WiFi connected:", wlan.ifconfig())
            led.on()
            return True
        led.value(not led.value())
        time.sleep_ms(config.WIFI_RETRY_DELAY_MS)

    print(
        "WiFi connection failed after {} attempts, status: {}".format(
            config.WIFI_CONNECT_RETRIES, wlan.status()
        )
    )
    _blink(6, on_ms=50, off_ms=50)
    return False


def _sync_time():
    """
    Best-effort NTP sync for human-readable timestamps.

    The dispatcher treats MQTT arrival order at the broker as authoritative
    for event ordering, not this timestamp, so a failed sync is not fatal.
    """
    try:
        import ntptime

        ntptime.settime()
    except Exception as e:
        print("NTP sync failed (non-fatal):", e)


def on_command_message(topic, msg):
    """
    Handle an incoming command message.

    Placeholder for now: logs {"action": "set_speed"|"stop"|"reverse", ...}
    but does not yet drive the LEGO hub -- that stays on the central Pi.
    """
    try:
        payload = json.loads(msg)
        print("Command received on {}: {}".format(topic, payload))
    except Exception as e:
        print("Failed to parse command message:", e)


def connect_mqtt():
    """Connect to the MQTT broker and subscribe to the command topic."""
    client = MQTTClient(
        config.TRAIN_ID,
        config.MQTT_BROKER_HOST,
        port=config.MQTT_BROKER_PORT,
        user=config.MQTT_USERNAME,
        password=config.MQTT_PASSWORD,
        keepalive=config.MQTT_KEEPALIVE,
    )
    client.set_callback(on_command_message)

    try:
        client.connect()
        client.subscribe(config.MQTT_COMMAND_TOPIC)
        print("MQTT connected, subscribed to", config.MQTT_COMMAND_TOPIC)
        return client
    except Exception as e:
        print("MQTT connect failed:", e)
        return None


def build_reader():
    """Construct the PN532 reader over I2C from config.

    Hardware reset via RST pin is required -- the PN532 throws
    OSError EIO on the first I2C write without it.
    """
    i2c = machine.I2C(
        config.I2C_ID,
        sda=machine.Pin(config.I2C_SDA_PIN),
        scl=machine.Pin(config.I2C_SCL_PIN),
        freq=config.I2C_FREQ,
    )

    rst = machine.Pin(config.PN532_RST_PIN, machine.Pin.OUT)
    rst.value(0)
    time.sleep_ms(100)
    rst.value(1)
    time.sleep_ms(500)

    reader = PN532_I2C(i2c)

    try:
        fw = reader.firmware_version()
        print("PN532 firmware:", fw)
    except Exception as e:
        print("PN532 init failed:", e)
        raise

    reader.set_mode(0x01)
    return reader


def _uid_to_hex(uid_bytes):
    """Convert a UID (bytes or list of ints) to uppercase hex string."""
    return "".join("{:02X}".format(b) for b in uid_bytes)


def read_tag_uid(reader):
    """
    Poll the PN532 once. Returns a hex UID string on a *new* tag
    presentation, or None otherwise.

    Dedup: the same UID only re-triggers a publish after
    RFID_CLEAR_AFTER_MISSES consecutive empty polls, since passive RFID
    reads flicker in and out as the tag passes under the reader.
    """
    global _last_uid, _miss_count

    timeout = getattr(config, "PN532_READ_TIMEOUT_MS", 200)
    result = reader.list_passive_target(timeout=timeout)

    if not result:
        _miss_count += 1
        if _miss_count >= config.RFID_CLEAR_AFTER_MISSES:
            _last_uid = None
        return None

    # result is [tg, sens_res, sel_res, uid_bytes_list]
    # uid_bytes_list is a list of ints, e.g. [71, 8, 249, 4] -> "4708F904"
    uid_bytes = result[3] if len(result) > 3 else result[-1]
    uid = _uid_to_hex(uid_bytes)

    _miss_count = 0

    if uid == _last_uid:
        return None

    _last_uid = uid
    return uid


def publish_tag_event(client, tag_uid):
    """Publish a tag-read event. Returns False (does not raise) on failure."""
    payload = json.dumps(
        {
            "train_id": config.TRAIN_ID,
            "tag_uid": tag_uid,
            "timestamp": time.time(),
            "battery_v": _battery_voltage,
        }
    )
    try:
        client.publish(config.MQTT_TAG_TOPIC, payload)
        return True
    except Exception as e:
        print("Publish failed:", e)
        return False


def publish_status(client, status):
    """Publish a status message on the train's status topic."""
    payload = json.dumps(
        {
            "train_id": config.TRAIN_ID,
            "status": status,
            "timestamp": time.time(),
            "battery_v": _battery_voltage,
        }
    )
    try:
        topic = "train/{}/status".format(config.TRAIN_ID)
        client.publish(topic, payload)
        print("Published status:", status)
        return True
    except Exception as e:
        print("Status publish failed:", e)
        return False


def main():
    """Connect WiFi/MQTT, then poll the PN532 forever, publishing tag reads."""
    global _pending_tag, _wdt, _battery_voltage, _last_battery_read_ms

    wifi_ok = connect_wifi()
    if wifi_ok:
        _sync_time()
        try:
            _wdt = machine.WDT(timeout=config.WATCHDOG_TIMEOUT_MS)
        except Exception as e:
            print("Watchdog unavailable (non-fatal):", e)
            _wdt = None

    wlan = network.WLAN(network.STA_IF)
    mqtt_client = connect_mqtt() if wifi_ok else None

    reader = build_reader()
    _battery_voltage = read_vsys_voltage()
    _last_battery_read_ms = time.ticks_ms()

    if mqtt_client is not None:
        publish_status(mqtt_client, "ready")

    print("Entering main loop")
    while True:
        if _wdt is not None:
            _wdt.feed()

        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, _last_battery_read_ms) >= _BATTERY_READ_INTERVAL_MS:
            _battery_voltage = read_vsys_voltage()
            _last_battery_read_ms = now_ms

        if not wlan.isconnected():
            print("WiFi dropped, reconnecting...")
            mqtt_client = None
            if connect_wifi():
                mqtt_client = connect_mqtt()
                if mqtt_client is not None:
                    publish_status(mqtt_client, "reconnected")
                reader = build_reader()  # reinit I2C after WiFi reset

        if wlan.isconnected() and mqtt_client is None:
            time.sleep_ms(config.MQTT_RECONNECT_DELAY_MS)
            mqtt_client = connect_mqtt()
            if mqtt_client is not None:
                publish_status(mqtt_client, "reconnected")

        if mqtt_client is not None:
            try:
                mqtt_client.check_msg()
            except Exception as e:
                print("MQTT check_msg failed, will reconnect:", e)
                mqtt_client = None

        if mqtt_client is not None and _pending_tag is not None:
            tag_uid, _ts = _pending_tag
            if publish_tag_event(mqtt_client, tag_uid):
                _pending_tag = None

        tag_uid = read_tag_uid(reader)
        if tag_uid is not None:
            print("Tag read:", tag_uid)
            if mqtt_client is None or not publish_tag_event(mqtt_client, tag_uid):
                _pending_tag = (tag_uid, time.time())

        time.sleep_ms(config.RFID_POLL_INTERVAL_MS)


if __name__ == "__main__":
    main()
