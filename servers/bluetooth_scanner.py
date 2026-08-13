#!/usr/bin/env python3
import asyncio
import time
from typing import Optional
from bleak import BleakScanner
import subprocess
from utils.logging_config import get_logger

logger = get_logger(__name__)


class BetterBleScanner:
    def __init__(self, adapter: Optional[str] = None):
        self.adapter = adapter
        self.scanner = None
        self._scanning = False
        self._lock = asyncio.Lock()  # Add lock for thread safety

    async def start_scan(self, callback):
        """Start a new scan with forced cleanup"""
        async with self._lock:  # Use lock to prevent concurrent scans
            try:
                if self._scanning:
                    logger.info("Scanner already running, stopping first...")
                    await self.stop_scan()
                    await asyncio.sleep(1)  # Give time for cleanup

                logger.info("Resetting Bluetooth...")
                await self.reset_bluetooth()
                await asyncio.sleep(1)  # Wait for reset

                logger.debug("Creating new scanner...")
                self.scanner = BleakScanner(callback, adapter=self.adapter)
                logger.debug("Starting scan...")
                await self.scanner.start()
                self._scanning = True
                logger.info("Scanning started successfully")

            except Exception as e:
                logger.error(f"Error starting scan: {e}", exc_info=True)
                self._scanning = False
                self.scanner = None
                raise

    async def stop_scan(self):
        """Stop scanning with forced cleanup"""
        async with self._lock:
            if self.scanner and self._scanning:
                try:
                    logger.debug("Stopping scanner...")
                    await self.scanner.stop()
                    self._scanning = False
                    self.scanner = None
                    logger.info("Scanner stopped successfully")
                except Exception as e:
                    logger.warning(f"Warning - error stopping scanner: {e}")
                finally:
                    self._scanning = False
                    self.scanner = None

    async def reset_bluetooth(self):
        """Reset Bluetooth to a known state"""
        try:
            logger.info("Resetting Bluetooth...")

            if self.adapter:
                # Adapter-scoped reset so this doesn't affect other adapters
                # in a multi-adapter setup.
                commands = [
                    ["sudo", "hciconfig", self.adapter, "down"],
                    ["sudo", "hciconfig", self.adapter, "up"],
                ]
            else:
                # Simple reset using bluetoothctl (affects the default adapter)
                commands = [
                    ["sudo", "bluetoothctl", "power", "off"],
                    ["sudo", "bluetoothctl", "power", "on"],
                ]

            for cmd in commands:
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    await asyncio.sleep(0.5)  # Longer delay between commands
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Warning - command {cmd} failed: {e}")
                    continue

            logger.info("Bluetooth reset complete")
        except Exception as e:
            logger.error(f"Error during Bluetooth reset: {e}", exc_info=True)
            raise

    @property
    def is_scanning(self):
        """Check if currently scanning"""
        return self._scanning and self.scanner is not None
