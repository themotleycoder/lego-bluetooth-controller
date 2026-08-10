#!/usr/bin/env python3
from enum import IntEnum, IntFlag

# Train command constants
TRAIN_COMMAND = {"STOP": 0, "FORWARD": 1, "BACKWARD": 2}

# Bluetooth constants
LEGO_MANUFACTURER_IDS = [
    919,
    0x397,
]  # LEGO manufacturer ID can be either 919 (0x0397) or 0x397
COMMAND_CHANNEL = 21  # Channel for train commands

# Stock LEGO hub GATT service/characteristic (LEGO Wireless Protocol 3.0).
# Used for direct GATT control of hubs running stock firmware (currently
# trains, via controllers/train_controller.py and servers/lego_service.py).
LEGO_HUB_SERVICE = "00001623-1212-efde-1623-785feabcd123"
LEGO_HUB_CHAR = "00001624-1212-efde-1623-785feabcd123"
PORT_A = 0x00  # Motor port for trains

# Pybricks GATT service/characteristics. Used for direct GATT control of
# hubs running Pybricks firmware (switches, via
# controllers/switch_controller.py and hubs/switch_receiver_*.py) -- the
# same protocol pybricksdev's `run` command uses to talk to a running user
# program over stdin/stdout, rather than the broadcast/observe protocol.
PYBRICKS_SERVICE = "c5f50001-8280-46da-89f4-6d8051e4aeef"
PYBRICKS_COMMAND_EVENT_CHAR = "c5f50002-8280-46da-89f4-6d8051e4aeef"
PYBRICKS_HUB_CAPABILITIES_CHAR = "c5f50003-8280-46da-89f4-6d8051e4aeef"


class PybricksCommand(IntEnum):
    """Commands written to PYBRICKS_COMMAND_EVENT_CHAR (frame: [command_id] + payload)."""

    STOP_USER_PROGRAM = 0
    START_USER_PROGRAM = 1
    START_REPL = 2
    WRITE_USER_PROGRAM_META = 3
    WRITE_USER_RAM = 4
    REBOOT_TO_UPDATE_MODE = 5
    WRITE_STDIN = 6


class PybricksEvent(IntEnum):
    """Notification types on PYBRICKS_COMMAND_EVENT_CHAR (frame: [event_id] + payload)."""

    STATUS_REPORT = 0  # payload: <I little-endian PybricksStatusFlag bitfield
    WRITE_STDOUT = 1  # payload: raw bytes the hub wrote to stdout


class PybricksStatusFlag(IntFlag):
    """Flags decoded from a STATUS_REPORT event payload."""

    BATTERY_LOW_VOLTAGE_WARNING = 1 << 0
    BATTERY_LOW_VOLTAGE_SHUTDOWN = 1 << 1
    BATTERY_HIGH_CURRENT = 1 << 2
    BLE_ADVERTISING = 1 << 3
    BLE_LOW_SIGNAL = 1 << 4
    POWER_BUTTON_PRESSED = 1 << 5
    USER_PROGRAM_RUNNING = 1 << 6


# Minimum Pybricks protocol version (from the Hub Capabilities characteristic)
# required for the WRITE_STDIN command used to send switch commands.
PYBRICKS_MIN_PROTOCOL_VERSION_FOR_STDIN = (1, 3, 0)
