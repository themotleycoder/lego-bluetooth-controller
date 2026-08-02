"""
Standalone RFID reader test for the Pico 2 W + MFRC522.

Hardware bring-up script: polls the reader and prints each new tag's UID as
it's presented. No WiFi/MQTT/config.py needed -- just this file and
mfrc522.py on the device. Useful for verifying wiring before running the
full pico/main.py firmware.

Usage (VS Code + MicroPico extension, or any MicroPython tool):
  1. Upload mfrc522.py to the Pico (from this pico/ directory).
  2. Open this file and run it on the Pico ("Run current file on Pico").
  3. Present a tag -- its UID should print to the console.
"""

import time
from mfrc522 import MFRC522

# SPI pin wiring -- match pico/config.example.py's defaults, adjust if wired differently
SPI_ID = 0
SCK_PIN = 2
MOSI_PIN = 3
MISO_PIN = 4
RST_PIN = 0
CS_PIN = 1

POLL_INTERVAL_MS = 100
CLEAR_AFTER_MISSES = 3  # consecutive empty reads before the same tag can re-trigger


def uid_to_hex(uid_bytes):
    return "".join("{:02X}".format(b) for b in uid_bytes)


def main():
    reader = MFRC522(
        sck=SCK_PIN, mosi=MOSI_PIN, miso=MISO_PIN, rst=RST_PIN, cs=CS_PIN, spi_id=SPI_ID
    )
    print("RFID reader initialized. Present a tag...")

    last_uid = None
    miss_count = 0

    while True:
        status, _bits = reader.request(reader.REQIDL)
        if status == reader.OK:
            status, raw_uid = reader.SelectTagSN()
            if status == reader.OK:
                uid = uid_to_hex(raw_uid)
                miss_count = 0
                if uid != last_uid:
                    print("Tag detected: UID =", uid)
                    last_uid = uid
                time.sleep_ms(POLL_INTERVAL_MS)
                continue

        miss_count += 1
        if miss_count >= CLEAR_AFTER_MISSES:
            if last_uid is not None:
                print("Tag removed")
            last_uid = None
        time.sleep_ms(POLL_INTERVAL_MS)


if __name__ == "__main__":
    main()
