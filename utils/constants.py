#!/usr/bin/env python3

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
