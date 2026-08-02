"""
Pico 2 W RFID tag-reporting firmware.

Polls an MFRC522 reader over SPI and publishes tag reads to MQTT for the
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
from mfrc522 import MFRC522

led = machine.Pin("LED", machine.Pin.OUT)

_last_uid = None
_miss_count = 0
_pending_tag = None  # (tag_uid, timestamp) buffered after a failed publish
_wdt = None


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
    """Construct the MFRC522 reader from the configured SPI pins."""
    return MFRC522(
        sck=config.SPI_SCK_PIN,
        mosi=config.SPI_MOSI_PIN,
        miso=config.SPI_MISO_PIN,
        rst=config.RST_PIN,
        cs=config.CS_PIN,
        spi_id=config.SPI_ID,
    )


def _uid_to_hex(uid_bytes):
    return "".join("{:02X}".format(b) for b in uid_bytes)


def read_tag_uid(reader):
    """
    Poll the reader once. Returns a hex UID string on a *new* tag
    presentation, or None otherwise.

    Dedup: the same UID only re-triggers a publish after
    RFID_CLEAR_AFTER_MISSES consecutive empty polls, since passive RFID
    reads flicker in and out as the tag passes under the reader.
    """
    global _last_uid, _miss_count

    status, _bits = reader.request(reader.REQIDL)
    if status != reader.OK:
        _miss_count += 1
        if _miss_count >= config.RFID_CLEAR_AFTER_MISSES:
            _last_uid = None
        return None

    status, raw_uid = reader.SelectTagSN()
    if status != reader.OK:
        _miss_count += 1
        if _miss_count >= config.RFID_CLEAR_AFTER_MISSES:
            _last_uid = None
        return None

    _miss_count = 0
    uid = _uid_to_hex(raw_uid)

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
    """Connect WiFi/MQTT, then poll the RC522 forever, publishing tag reads."""
    global _pending_tag, _wdt

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

    if mqtt_client is not None:
        publish_status(mqtt_client, "ready")

    reader = build_reader()

    print("Entering main loop")
    while True:
        if _wdt is not None:
            _wdt.feed()

        if not wlan.isconnected():
            print("WiFi dropped, reconnecting...")
            mqtt_client = None
            if connect_wifi():
                mqtt_client = connect_mqtt()
                if mqtt_client is not None:
                    publish_status(mqtt_client, "reconnected")
                reader = build_reader()  # reinit SPI after WiFi reset

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
