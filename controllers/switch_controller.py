#!/usr/bin/env python3
import asyncio
import struct
import time
from typing import Dict, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from servers.bluetooth_scanner import BetterBleScanner
from utils.constants import (
    PYBRICKS_COMMAND_EVENT_CHAR,
    PYBRICKS_HUB_CAPABILITIES_CHAR,
    PybricksCommand,
    PybricksEvent,
)
from utils.logging_config import get_logger

logger = get_logger(__name__)


class SwitchController:
    """
    Controls LEGO track switches running Pybricks firmware over a direct
    GATT connection to the Pybricks GATT service, rather than the
    broadcast/observe protocol this used to use. Switch commands are sent
    as WRITE_STDIN writes and status arrives via WRITE_STDOUT notifications
    on PYBRICKS_COMMAND_EVENT_CHAR -- the same protocol pybricksdev's `run`
    command uses to talk to a running user program. See
    hubs/switch_receiver_*.py for the hub-side stdin/stdout loop this talks
    to.

    Switch hubs are identified throughout the public API/dispatcher by an
    integer hub_id (the old broadcast-channel id, kept for backwards
    compatibility) which is resolved to a BLE address via `hub_mapping`
    (Settings.switch_hub_mapping) -- unlike TrainController, which uses the
    BLE address as the hub's public identity directly, since one switch
    hub_id can have multiple switches wired to its ports.

    SwitchController owns the process's single continuous BLE discovery
    scan (self.scanner) -- TrainController has no scanner of its own and
    piggybacks on this one via set_device_seen_callback, since BlueZ only
    allows one active discovery session per D-Bus client and a second
    concurrent scan/implicit-connect-scan would collide with this one. See
    TrainController's docstring for the full explanation.
    """

    def __init__(
        self,
        hub_mapping: Optional[Dict[int, str]] = None,
        max_reconnect_attempts: int = 5,
        reconnect_delay: float = 3.0,
    ):
        self.running = True
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay

        self._hub_mapping: Dict[int, str] = dict(hub_mapping or {})
        self._address_to_hub_id: Dict[str, int] = {
            address.upper(): hub_id for hub_id, address in self._hub_mapping.items()
        }

        self.scanner = BetterBleScanner()

        self._clients: Dict[str, BleakClient] = {}  # address -> client
        self._connecting: set = set()  # addresses currently connecting
        self._connection_state: Dict[int, str] = {}  # hub_id -> state

        self.switch_statuses: Dict[int, dict] = {}
        self.last_update_times: Dict[int, float] = {}
        self.reliability_stats: Dict[
            str, dict
        ] = {}  # switch_name -> {attempts, successes}

        # Invoked for every BLE device this controller's scan sees, not just
        # switch hubs -- lets TrainController piggyback on this scan to find
        # train hubs without running its own competing BlueZ discovery
        # session (see set_device_seen_callback).
        self._device_seen_callback = None

    def set_device_seen_callback(self, callback):
        """Register a callback(device, advertisement_data) fired for every device this scanner sees."""
        self._device_seen_callback = callback

    # ------------------------------------------------------------------
    # Wire format helpers
    # ------------------------------------------------------------------

    def decode_port_connections(self, port_connections):
        """Decode the port-connection bitmap byte into a SWITCH_x -> 0/1 dict."""
        switch_states = {}
        for port, bit in [("A", 0b1000), ("B", 0b0100), ("C", 0b0010), ("D", 0b0001)]:
            switch_states[f"SWITCH_{port}"] = int(bool(port_connections & bit))
        return switch_states

    def decode_switch_status(self, status_byte):
        """Decode the switch-position bitmap byte into a SWITCH_x -> 0/1 dict."""
        switch_positions = {}
        for port, bit in [("A", 0b1000), ("B", 0b0100), ("C", 0b0010), ("D", 0b0001)]:
            switch_positions[f"SWITCH_{port}"] = int(bool(status_byte & bit))
        return switch_positions

    def encode_switch_command(self, switch_name, position):
        """Encode a WRITE_STDIN payload: [switch_num(1-4), position(0/1)]."""
        switch_letter = switch_name[-1]
        if switch_letter not in "ABCD":
            raise ValueError(f"Invalid switch name: {switch_name}")
        if not isinstance(position, int) or position not in (0, 1):
            raise ValueError(f"Invalid position: {position}. Must be 0 or 1")

        switch_num = ord(switch_letter) - ord("A") + 1
        return bytes([switch_num, position])

    # ------------------------------------------------------------------
    # Discovery + connection management
    # ------------------------------------------------------------------

    def handle_device_seen(self, device: BLEDevice, advertisement_data) -> None:
        """
        Called for every device the shared scan sees. Forwards to any
        registered callback (TrainController piggybacks here), then
        connects known switch hubs via GATT using this already-resolved
        BLEDevice -- mirrors TrainController.handle_device_seen, and for
        the same reason: a bare-address BleakClient.connect() would
        trigger bleak's implicit discovery scan, colliding with this one.
        """
        if self._device_seen_callback:
            self._device_seen_callback(device, advertisement_data)

        address_upper = device.address.upper()
        if address_upper not in self._address_to_hub_id:
            return
        if device.address in self._clients or device.address in self._connecting:
            return
        asyncio.create_task(self._connect_hub(device.address, device))

    async def _connect_hub(self, address: str, device: Optional[BLEDevice] = None):
        """Connect to a switch hub, verify Pybricks protocol support, and start its program."""
        if address in self._clients or address in self._connecting:
            return

        hub_id = self._address_to_hub_id.get(address.upper())
        self._connecting.add(address)
        if hub_id is not None:
            self._connection_state[hub_id] = "connecting"
        try:
            for attempt in range(self.max_reconnect_attempts):
                try:
                    client = BleakClient(
                        device or address,
                        disconnected_callback=lambda c, addr=address: self._on_disconnect(
                            addr
                        ),
                    )
                    await client.connect()

                    if not await self._prepare_hub(client, address):
                        await client.disconnect()
                        raise RuntimeError("Hub capability check failed")

                    self._clients[address] = client
                    if hub_id is not None:
                        self._connection_state[hub_id] = "connected"
                    logger.info(f"Connected to switch hub {address} (hub_id={hub_id})")
                    return

                except Exception as e:
                    logger.warning(
                        f"Connect attempt {attempt + 1}/{self.max_reconnect_attempts} "
                        f"failed for switch hub {address}: {e}"
                    )
                    if attempt < self.max_reconnect_attempts - 1:
                        await asyncio.sleep(self.reconnect_delay)

            if hub_id is not None:
                self._connection_state[hub_id] = "error"
            logger.error(
                f"Failed to connect to switch hub {address} after "
                f"{self.max_reconnect_attempts} attempts"
            )
        finally:
            self._connecting.discard(address)

    async def _prepare_hub(self, client: BleakClient, address: str) -> bool:
        """Confirm Pybricks protocol support, subscribe to status, start the hub's program."""
        try:
            capabilities = await client.read_gatt_char(PYBRICKS_HUB_CAPABILITIES_CHAR)
        except Exception as e:
            logger.error(f"Could not read hub capabilities for {address}: {e}")
            return False

        # max_char_size is the first field in both the v1.2 (<HII, 8 bytes)
        # and v1.5 (<HIIB, 11 bytes) Hub Capabilities layouts.
        if len(capabilities) < 8:
            logger.error(
                f"Unexpected hub capabilities payload for {address}: {capabilities!r}"
            )
            return False
        max_char_size = struct.unpack("<H", capabilities[:2])[0]

        if max_char_size < 2:
            logger.error(
                f"Switch hub {address} max_char_size ({max_char_size}) too small "
                f"for the 2-byte switch command payload"
            )
            return False

        await client.start_notify(
            PYBRICKS_COMMAND_EVENT_CHAR, self._make_notification_handler(address)
        )

        try:
            await client.write_gatt_char(
                PYBRICKS_COMMAND_EVENT_CHAR,
                bytes([PybricksCommand.START_USER_PROGRAM]),
                response=True,
            )
        except Exception as e:
            # BUSY (program already running) is expected here and harmless.
            logger.debug(f"START_USER_PROGRAM for {address}: {e}")

        return True

    def _on_disconnect(self, address: str):
        """
        Handle an unexpected hub disconnect by clearing state. Reconnection
        happens the next time `handle_device_seen` reports this address
        (from the ongoing scan), not immediately here -- see
        TrainController's equivalent for why.
        """
        hub_id = self._address_to_hub_id.get(address.upper())
        logger.warning(f"Switch hub {address} (hub_id={hub_id}) disconnected")
        if hub_id is not None:
            self._connection_state[hub_id] = "disconnected"
        self._clients.pop(address, None)

    def _make_notification_handler(self, address: str):
        """Build a per-hub notification callback decoding Pybricks Command/Event frames."""
        hub_id = self._address_to_hub_id.get(address.upper())

        def handler(_sender, data: bytearray):
            if not data:
                return
            event_id, payload = data[0], bytes(data[1:])
            if event_id == PybricksEvent.WRITE_STDOUT:
                self._handle_stdout(hub_id, payload)
            elif event_id == PybricksEvent.STATUS_REPORT:
                logger.debug(f"Switch hub {address} status report: {payload!r}")

        return handler

    def _handle_stdout(self, hub_id: Optional[int], payload: bytes):
        """Decode a 2-byte [status_byte, port_connections] status frame from the hub."""
        if hub_id is None or len(payload) < 2:
            return

        status_byte, port_connections = payload[0], payload[1]
        current_time = time.time()

        self.switch_statuses[hub_id] = {
            "status": status_byte,
            "switch_positions": self.decode_switch_status(status_byte),
            "switch_states": self.decode_port_connections(port_connections),
            "connected": True,
            "timestamp": current_time,
            "name": "Technic Hub",
        }
        self.last_update_times[hub_id] = current_time

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def send_command_with_retry(
        self, hub_id, switch_name, position, max_retries=3
    ):
        """Send a switch command, retrying with backoff and verifying the position changed."""
        if switch_name not in self.reliability_stats:
            self.reliability_stats[switch_name] = {"attempts": 0, "successes": 0}
        self.reliability_stats[switch_name]["attempts"] += 1

        for attempt in range(max_retries):
            if attempt > 0:
                logger.info(
                    f"Retry {attempt + 1}/{max_retries} for switch {switch_name}"
                )
                await asyncio.sleep(0.5 * attempt)

            try:
                await self._send_command(hub_id, switch_name, position)

                if await self._verify_switch_position(
                    hub_id, switch_name, position, timeout=2.0
                ):
                    logger.info(
                        f"Switch {switch_name} successfully changed to "
                        f"{'DIVERGING' if position else 'STRAIGHT'}"
                    )
                    self.reliability_stats[switch_name]["successes"] += 1
                    return True

            except Exception as e:
                logger.warning(f"Command attempt {attempt + 1} failed: {e}")

        logger.error(
            f"Failed to change switch {switch_name} after {max_retries} attempts"
        )
        stats = self.reliability_stats[switch_name]
        success_rate = stats["successes"] / stats["attempts"] * 100
        logger.info(
            f"Switch {switch_name} reliability: {success_rate:.1f}% "
            f"({stats['successes']}/{stats['attempts']} successful)"
        )
        return False

    async def _send_command(self, hub_id, switch_name, position):
        """Write a WRITE_STDIN command carrying the switch payload, acked (response=True)."""
        address = self._hub_mapping.get(hub_id)
        if address is None:
            raise ValueError(f"No BLE address configured for switch hub_id {hub_id}")

        client = self._clients.get(address)
        if client is None or not client.is_connected:
            raise ConnectionError(f"Switch hub {hub_id} ({address}) is not connected")

        payload = self.encode_switch_command(switch_name, position)
        await client.write_gatt_char(
            PYBRICKS_COMMAND_EVENT_CHAR,
            bytes([PybricksCommand.WRITE_STDIN]) + payload,
            response=True,
        )

    async def _verify_switch_position(
        self, hub_id, switch_name, expected_position, timeout=2.0
    ):
        """Poll switch_statuses (populated by WRITE_STDOUT notifications) until it matches."""
        start_time = time.time()
        check_interval = 0.05

        while time.time() - start_time < timeout:
            status = self.switch_statuses.get(hub_id)
            if status is not None:
                current_pos = status.get("switch_positions", {}).get(switch_name)
                if current_pos == expected_position:
                    return True
                if not status.get("switch_states", {}).get(switch_name):
                    logger.warning(
                        f"Warning: switch {switch_name} appears disconnected"
                    )

            await asyncio.sleep(check_interval)

        logger.warning(
            f"Verification failed for switch {switch_name}, expected {expected_position}"
        )
        return False

    # ------------------------------------------------------------------
    # Status monitoring
    # ------------------------------------------------------------------

    async def start_status_monitoring(self):
        """Run the shared BLE discovery scan and connect known switch hubs as they're seen."""
        logger.info("Starting switch status monitoring...")
        self.running = True

        if not self._hub_mapping:
            logger.warning(
                "No switch hub addresses configured (SWITCH_HUB_MAPPING) -- "
                "SwitchController has nothing to connect to"
            )

        while self.running:
            try:
                logger.debug("Setting up scanner...")
                await self.scanner.start_scan(self.handle_device_seen)
                logger.debug("Scanner started, waiting for events...")

                while self.running:
                    await asyncio.sleep(0.05)

            except Exception as e:
                logger.error(f"Error in monitor loop: {e}", exc_info=True)
                logger.info("Waiting before retry...")
                await asyncio.sleep(1)
            finally:
                await self.scanner.stop_scan()

    def get_connected_switches(self):
        """Return status/reliability info for every currently-connected switch hub."""
        connected_switches = {}
        current_time = time.time()

        for hub_id, address in self._hub_mapping.items():
            client = self._clients.get(address)
            if client is None or not client.is_connected:
                continue

            status = self.switch_statuses.get(hub_id, {})
            timestamp = float(status.get("timestamp", current_time))

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
                "last_update_seconds_ago": round(current_time - timestamp, 2),
                "name": status.get("name", "Technic Hub"),
                "status": status.get("status"),
                "connected": True,
                "rssi": None,
                "reliability": reliability_data,
            }

        return connected_switches

    async def stop_status_monitoring(self):
        """Cleanup and stop monitoring"""
        logger.info("Stopping switch status monitoring...")
        self.running = False
        await self.scanner.stop_scan()
        for address, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {address}: {e}")
        self._clients.clear()
        logger.info("Switch monitor stopped")
