"""
LEGO Control Server Package

This package provides functionality for controlling LEGO Powered Up devices via Bluetooth:
- Switch control for train track switches
- Basic train control (stop, forward, backward)
- Bluetooth scanning and communication
"""

from utils.constants import TRAIN_COMMAND, COMMAND_CHANNEL, LEGO_MANUFACTURER_IDS
from .bluetooth_scanner import BetterBleScanner

# LegoController is intentionally not re-exported here (import it directly
# from servers.main). servers.main imports controllers.switch_controller,
# which imports servers.bluetooth_scanner -- eagerly re-exporting it at
# package-init time creates a circular import for any caller that reaches
# controllers.* before servers.main has finished loading.

__all__ = [
    "TRAIN_COMMAND",
    "COMMAND_CHANNEL",
    "LEGO_MANUFACTURER_IDS",
    "BetterBleScanner",
]
