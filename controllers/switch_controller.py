#!/usr/bin/env python3
import asyncio
import struct
import subprocess
import time

from servers.bluetooth_scanner import BetterBleScanner
from utils.constants import LEGO_MANUFACTURER_IDS
from utils.logging_config import get_logger

logger = get_logger(__name__)


class SwitchController:
    def __init__(self):
        self.scanner = BetterBleScanner()
        self.command_number = 0x01
        self.last_command = None
        self.last_status = None
        self.switch_statuses = {}
        self.last_update_times = {}
        self.running = True
        self.command_queue = asyncio.Queue()
        self.command_task = None
        self.reliability_stats = {}  # Track success/failure for each switch
        # Invoked for every BLE device this controller's scan sees, not just
        # Technic Hubs -- lets TrainController piggyback on this scan to
        # find train hubs without running its own competing BlueZ discovery
        # session (see set_device_seen_callback).
        self._device_seen_callback = None

    def set_device_seen_callback(self, callback):
        """Register a callback(device, advertisement_data) fired for every device this scanner sees."""
        self._device_seen_callback = callback

    def decode_port_connections(self, port_connections):
        """
        Improved port connection debugging and decoding.
        Hub sends port bits in format: port_bit = 1 << (ord('D') - ord(port))
        """
        switch_states = {}
        binary = format(port_connections & 0x0F, "04b")

        # Detailed logging
        logger.debug("Port Connection Analysis:")
        logger.debug(f"Raw value: {port_connections}")
        logger.debug(f"Binary: {binary}")

        for port, bit in [("A", 0b1000), ("B", 0b0100), ("C", 0b0010), ("D", 0b0001)]:
            connected = bool(port_connections & bit)
            switch_states[f"SWITCH_{port}"] = int(connected)
            logger.debug(
                f"Port {port}: {'Connected' if connected else 'Disconnected'} (bit: {bit:04b})"
            )

        return switch_states

    def decode_switch_status(self, status_byte):
        """
        Improved switch position decoding with validation
        """
        switch_positions = {}
        binary = format(status_byte & 0x0F, "04b")
        logger.debug("Switch Status Analysis:")
        logger.debug(f"Raw status: {status_byte}")
        logger.debug(f"Binary: {binary}")

        for port, bit in [("A", 0b1000), ("B", 0b0100), ("C", 0b0010), ("D", 0b0001)]:
            position = bool(status_byte & bit)
            switch_positions[f"SWITCH_{port}"] = int(position)
            logger.debug(
                f"Switch {port}: {'DIVERGING' if position else 'STRAIGHT'} (bit: {bit:04b})"
            )

        return switch_positions

    def encode_switch_command(self, switch_name, position):
        """
        Enhanced switch command encoding with validation
        """
        switch_letter = switch_name[-1]
        if switch_letter not in "ABCD":
            raise ValueError(f"Invalid switch name: {switch_name}")

        switch_num = ord(switch_letter) - ord("A") + 1
        if not isinstance(position, int) or position not in [0, 1]:
            raise ValueError(f"Invalid position: {position}. Must be 0 or 1")

        command_value = (switch_num * 1000) + position
        logger.debug(
            f"Encoded command: Switch {switch_letter} (num {switch_num}) to position {position}"
        )
        return command_value

    async def send_command_with_retry(
        self, hub_id, switch_name, position, max_retries=3
    ):
        """Enhanced command sending with better retry logic and verification"""
        initial_state = None
        if hub_id in self.switch_statuses:
            initial_state = (
                self.switch_statuses[hub_id]
                .get("switch_positions", {})
                .get(switch_name)
            )

        # Update reliability tracking
        if switch_name not in self.reliability_stats:
            self.reliability_stats[switch_name] = {"attempts": 0, "successes": 0}
        self.reliability_stats[switch_name]["attempts"] += 1

        for attempt in range(max_retries):
            if attempt > 0:
                logger.info(
                    f"Retry {attempt + 1}/{max_retries} for switch {switch_name}"
                )
                await asyncio.sleep(0.5 * attempt)  # Increasing backoff delay

            try:
                # Send command with more reliable timing
                await self._send_command_robust(hub_id, switch_name, position)

                # Wait and verify the change
                if await self._verify_switch_position(
                    hub_id, switch_name, position, timeout=2.0
                ):
                    logger.info(
                        f"Switch {switch_name} successfully changed to {'DIVERGING' if position else 'STRAIGHT'}"
                    )
                    self.reliability_stats[switch_name]["successes"] += 1
                    return True

            except Exception as e:
                logger.warning(f"Command attempt {attempt + 1} failed: {e}")

        logger.error(
            f"Failed to change switch {switch_name} after {max_retries} attempts"
        )
        success_rate = (
            self.reliability_stats[switch_name]["successes"]
            / self.reliability_stats[switch_name]["attempts"]
            * 100
        )
        logger.info(
            f"Switch {switch_name} reliability: {success_rate:.1f}% "
            f"({self.reliability_stats[switch_name]['successes']}/"
            f"{self.reliability_stats[switch_name]['attempts']} successful)"
        )
        return False

    async def _send_command_robust(self, hub_id, switch_name, position):
        """More robust command sending with proper timing and validation"""
        try:
            # Stop any existing advertising
            subprocess.run(
                ["sudo", "hcitool", "cmd", "0x08", "0x000A", "00"],
                check=True,
                capture_output=True,
            )
            await asyncio.sleep(0.1)

            # Encode command
            command_value = self.encode_switch_command(switch_name, position)
            value_bytes = struct.pack("<h", command_value)

            payload = (
                bytes(
                    [
                        0x08,  # Length
                        0xFF,  # Manufacturer specific data
                        0x97,
                        0x03,  # LEGO manufacturer ID
                        int(hub_id),  # Channel
                        0x00,  # Single value indicator
                        0x62,  # Data type header (INT16)
                    ]
                )
                + value_bytes
            )

            # Set advertising parameters with longer intervals for reliability
            subprocess.run(
                [
                    "sudo",
                    "hcitool",
                    "cmd",
                    "0x08",
                    "0x0006",
                    "A0",
                    "00",  # 160ms interval
                    "A0",
                    "00",  # Same interval
                    "03",  # non-connectable
                    "00",
                    "00",
                    "00",
                    "00",
                    "00",
                    "00",
                    "00",
                    "00",
                    "07",
                    "00",
                ],
                check=True,
                capture_output=True,
            )
            await asyncio.sleep(0.1)

            # Set and start advertising with command repeat
            cmd = ["sudo", "hcitool", "cmd", "0x08", "0x0008"]
            cmd.append(format(len(payload), "x"))
            cmd.extend([format(b, "02x") for b in payload])

            # Send command multiple times for reliability
            for _ in range(2):
                subprocess.run(cmd, check=True, capture_output=True)
                await asyncio.sleep(0.1)
                subprocess.run(
                    ["sudo", "hcitool", "cmd", "0x08", "0x000A", "01"],
                    check=True,
                    capture_output=True,
                )
                await asyncio.sleep(0.2)

        except subprocess.CalledProcessError as e:
            logger.error(f"Bluetooth command failed: {e.stderr.decode()}")
            raise
        except Exception as e:
            logger.error(f"Error in robust command send: {e}", exc_info=True)
            raise

    async def _verify_switch_position(
        self, hub_id, switch_name, expected_position, timeout=2.0
    ):
        """Enhanced position verification with detailed diagnostics"""
        start_time = time.time()
        check_interval = 0.1
        last_position = None
        position_changes = 0

        while time.time() - start_time < timeout:
            try:
                if hub_id in self.switch_statuses:
                    status = self.switch_statuses[hub_id]
                    current_pos = status.get("switch_positions", {}).get(switch_name)

                    # Track position changes
                    if current_pos != last_position:
                        position_changes += 1
                        last_position = current_pos
                        logger.debug(f"Switch position changed to: {current_pos}")

                    if current_pos == expected_position:
                        return True

                    # Connection status check
                    if not status.get("switch_states", {}).get(switch_name):
                        logger.warning(
                            f"Warning: Switch {switch_name} appears disconnected"
                        )
                        logger.debug(f"Full status: {status}")

                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.error(f"Error checking switch position: {e}", exc_info=True)

        logger.warning(
            f"Verification failed. Position changes observed: {position_changes}"
        )
        logger.debug(
            f"Last known position: {last_position}, Expected: {expected_position}"
        )
        return False

    async def start_status_monitoring(self):
        """Enhanced status monitoring with command processing"""
        # Start command processor
        self.command_task = asyncio.create_task(self._process_commands())

        async def status_callback(device, advertisement_data):
            try:
                if self._device_seen_callback:
                    self._device_seen_callback(device, advertisement_data)

                if device.name and "Technic Hub" in device.name:
                    if 919 in advertisement_data.manufacturer_data:
                        data = advertisement_data.manufacturer_data[919]
                        logger.debug(f"Raw manufacturer data: {[hex(x) for x in data]}")

                        if len(data) >= 7:
                            try:
                                port_connections = data[-1]
                                status_byte = data[2]
                                hub_id = data[2]
                                current_time = time.time()
                                last_update = self.last_update_times.get(hub_id, 0)

                                if current_time - last_update >= 1.0:
                                    switch_positions = self.decode_switch_status(
                                        status_byte
                                    )
                                    switch_states = self.decode_port_connections(
                                        port_connections
                                    )

                                    status_data = {
                                        "status": status_byte,
                                        "switch_positions": switch_positions,
                                        "switch_states": switch_states,
                                        "connected": True,
                                        "timestamp": current_time,
                                        "name": device.name,
                                        "rssi": advertisement_data.rssi,
                                    }

                                    self.switch_statuses[hub_id] = status_data.copy()
                                    self.last_status = status_data.copy()
                                    self.last_update_times[hub_id] = current_time

                                    logger.debug(f"Status update from {device.name}:")
                                    logger.debug(f"RSSI: {advertisement_data.rssi} dBm")
                                    logger.debug(
                                        f"Switch positions: {switch_positions}"
                                    )
                                    logger.debug(f"Connected ports: {switch_states}")

                            except Exception as e:
                                logger.error(
                                    f"Error processing switch data: {e}", exc_info=True
                                )
                                import traceback

                                traceback.print_exc()

            except Exception as e:
                logger.error(f"Error in switch status callback: {e}", exc_info=True)
                import traceback

                traceback.print_exc()

        logger.info("Starting switch status monitoring...")
        while self.running:
            try:
                logger.debug("Setting up scanner...")
                await self.scanner.start_scan(status_callback)
                logger.debug("Scanner started, waiting for events...")

                while self.running:
                    await asyncio.sleep(0.05)  # Reduced sleep for better responsiveness

            except Exception as e:
                logger.error(f"Error in monitor loop: {e}", exc_info=True)
                logger.info("Waiting before retry...")
                await asyncio.sleep(1)
            finally:
                await self.scanner.stop_scan()

    async def _process_commands(self):
        """Background task to process switch commands"""
        while self.running:
            try:
                command = await self.command_queue.get()
                hub_id, switch_name, position = command

                success = await self.send_command_with_retry(
                    hub_id, switch_name, position
                )
                if not success:
                    logger.warning(f"Warning: Command failed for switch {switch_name}")

                self.command_queue.task_done()
                await asyncio.sleep(0.2)  # Prevent command flooding

            except Exception as e:
                logger.error(f"Error processing switch command: {e}", exc_info=True)

    def get_connected_switches(self):
        """Enhanced connected switch information"""
        current_time = time.time()
        connected_switches = {}

        for hub_id, status in self.switch_statuses.items():
            timestamp = float(status.get("timestamp", 0))
            last_update = current_time - timestamp

            if last_update < 5:  # Consider connected if updated in last 5 seconds
                reliability_data = {}
                for switch_name in ["SWITCH_A", "SWITCH_B", "SWITCH_C", "SWITCH_D"]:
                    if switch_name in self.reliability_stats:
                        stats = self.reliability_stats[switch_name]
                        success_rate = (
                            stats["successes"] / stats["attempts"] * 100
                            if stats["attempts"] > 0
                            else 0
                        )
                        reliability_data[switch_name] = {
                            "success_rate": round(success_rate, 1),
                            "attempts": stats["attempts"],
                            "successes": stats["successes"],
                        }

                connected_switches[hub_id] = {
                    "switch_positions": status.get("switch_positions", {}),
                    "switch_states": status.get("switch_states", {}),
                    "last_update_seconds_ago": round(last_update, 2),
                    "name": status.get("name"),
                    "status": status.get("status"),
                    "connected": True,
                    "rssi": status.get("rssi"),
                    "reliability": reliability_data,
                }

        return connected_switches

    async def stop_status_monitoring(self):
        """Cleanup and stop monitoring"""
        logger.info("Stopping switch status monitoring...")
        self.running = False
        if self.command_task:
            self.command_task.cancel()
            try:
                await self.command_task
            except asyncio.CancelledError:
                pass
        await self.scanner.stop_scan()
        logger.info("Switch monitor stopped")
