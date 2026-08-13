import asyncio
import time
from typing import Dict, Iterable, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from servers.bluetooth_scanner import BetterBleScanner
from utils.constants import (
    LEGO_HUB_CHAR,
    LWP3_MSG_HUB_PROPERTIES,
    LWP3_OP_UPDATE,
    LWP3_PROP_BATTERY_VOLTAGE,
    LWP3_SUBSCRIBE_BATTERY_VOLTAGE,
    PORT_A,
)
from utils.logging_config import get_logger

logger = get_logger(__name__)


class TrainController:
    """
    Controls LEGO trains running stock hub firmware over a direct GATT
    connection (LEGO Wireless Protocol 3.0), rather than the Pybricks
    broadcast/observe protocol used for switches.

    Hubs are identified by their BLE address (e.g. "90:84:2B:18:28:36").

    TrainController runs in one of two modes, depending on whether an
    `adapter` is configured:

    - No adapter configured (default, single-adapter deployments):
      TrainController does NOT run its own BLE discovery scan. Connecting
      by bare address doesn't avoid this either -- bleak's Linux/BlueZ
      backend triggers an implicit discovery scan internally to resolve an
      address it doesn't already have cached, which collides with
      SwitchController's continuous scan the exact same way an explicit
      scanner would (BlueZ rejects a second concurrent discovery request
      from the same D-Bus client on the same adapter with "Operation
      already in progress", and it never recovers on its own since the
      switch scan runs indefinitely). Instead, SwitchController's
      already-running scan forwards every device it sees to
      `handle_device_seen`, and configured train hubs are connected using
      the already-resolved BLEDevice object -- no separate discovery
      needed. See `SwitchController.set_device_seen_callback`, wired up in
      `servers/main.py::LegoController.__init__`.
    - Adapter configured (dual-adapter deployments, e.g. two USB dongles):
      TrainController runs its own continuous scan on its own adapter via
      `self.scanner`, entirely independent of SwitchController's scan --
      there's no discovery collision because each adapter is a distinct
      BlueZ D-Bus object with its own discovery session. In this mode
      `servers/main.py::LegoController.__init__` does NOT wire the
      SwitchController piggyback callback, since a BLEDevice discovered on
      one adapter can't be used to connect via a different adapter.
    """

    def __init__(
        self,
        known_addresses: Optional[Iterable[str]] = None,
        max_reconnect_attempts: int = 5,
        reconnect_delay: float = 3.0,
        adapter: Optional[str] = None,
    ):
        self.running = True
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self._known_addresses = list(known_addresses or [])
        self._known_addresses_upper = {a.upper() for a in self._known_addresses}

        # Used for reset_bluetooth() always, and for this controller's own
        # continuous scan when `adapter` is configured (see class docstring).
        self._adapter = adapter
        self.scanner = BetterBleScanner(adapter=adapter)

        self._clients: Dict[str, BleakClient] = {}
        self._connecting: set = set()
        self._connection_state: Dict[str, str] = {}  # address -> state
        self._hub_names: Dict[str, str] = {}
        self._hub_rssi: Dict[str, Optional[int]] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_command_time: Dict[str, float] = {}
        self._hub_battery_percentage: Dict[str, int] = {}

        self._active_trains = set()

        self.command_queue = asyncio.Queue()
        self.command_task = None

    async def reset_bluetooth(self):
        """Disconnect all hubs and reset the Bluetooth adapter."""
        for address, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {address}: {e}")

        self._clients.clear()
        self._connection_state = {
            addr: "disconnected" for addr in self._connection_state
        }
        await self.scanner.reset_bluetooth()

    def mark_train_active(self, hub_id: str):
        """Mark a train as active for more frequent updates"""
        self._active_trains.add(hub_id)

    def mark_train_inactive(self, hub_id: str):
        """Mark a train as inactive"""
        self._active_trains.discard(hub_id)

    async def _mark_inactive_later(self, hub_id: str):
        """Mark train as inactive after delay"""
        await asyncio.sleep(5)
        self.mark_train_inactive(hub_id)

    # ------------------------------------------------------------------
    # Discovery + connection management
    # ------------------------------------------------------------------

    async def _connect_hub(self, address: str, device: Optional[BLEDevice] = None):
        """
        Connect to a hub and subscribe to status notifications, with retry.

        `device` should be an already-discovered BLEDevice (from
        `handle_device_seen`) whenever possible -- connecting via a bare
        address string makes bleak resolve it with its own implicit scan,
        which collides with SwitchController's scan. See the class
        docstring.
        """
        if address in self._clients or address in self._connecting:
            return

        self._connecting.add(address)
        self._connection_state[address] = "connecting"
        self._hub_names.setdefault(address, "LEGO Hub")
        try:
            for attempt in range(self.max_reconnect_attempts):
                try:
                    client = BleakClient(
                        device or address,
                        adapter=self._adapter,
                        disconnected_callback=lambda c, addr=address: self._on_disconnect(
                            addr
                        ),
                    )
                    await client.connect()
                    await client.start_notify(
                        LEGO_HUB_CHAR, self._make_notification_handler(address)
                    )
                    await self._subscribe_battery(client, address)

                    self._clients[address] = client
                    self._connection_state[address] = "connected"
                    self._last_seen[address] = time.time()
                    logger.info(f"Connected to train hub {address}")
                    return

                except Exception as e:
                    logger.warning(
                        f"Connect attempt {attempt + 1}/{self.max_reconnect_attempts} "
                        f"failed for {address}: {e}"
                    )
                    if attempt < self.max_reconnect_attempts - 1:
                        await asyncio.sleep(self.reconnect_delay)

            self._connection_state[address] = "error"
            logger.error(
                f"Failed to connect to train hub {address} after "
                f"{self.max_reconnect_attempts} attempts"
            )
        finally:
            self._connecting.discard(address)

    async def _subscribe_battery(self, client: BleakClient, address: str) -> None:
        """Enable Battery Voltage (percentage) hub-property updates, best-effort."""
        try:
            await client.write_gatt_char(
                LEGO_HUB_CHAR, LWP3_SUBSCRIBE_BATTERY_VOLTAGE, response=True
            )
        except Exception as e:
            logger.warning(f"Could not subscribe to battery updates for {address}: {e}")

    def _on_disconnect(self, address: str):
        """
        Handle an unexpected hub disconnect by clearing state.

        Reconnection happens the next time `handle_device_seen` reports this
        address (from SwitchController's ongoing scan) -- not immediately
        here, since connecting via a bare address without an already-seen
        BLEDevice would trigger a colliding implicit discovery. BLE
        advertisements are frequent, so this is normally within a second or
        two.
        """
        logger.warning(f"Train hub {address} disconnected")
        self._connection_state[address] = "disconnected"
        self._clients.pop(address, None)

    def handle_device_seen(self, device: BLEDevice, advertisement_data) -> None:
        """
        Called for every device seen by the active scan -- SwitchController's
        scan in piggyback mode, or this controller's own scan when a
        dedicated `adapter` is configured. If it's a configured train hub
        that isn't already connected/connecting, connect to it using this
        already-resolved BLEDevice.
        """
        if device.address.upper() not in self._known_addresses_upper:
            return
        if device.address in self._clients or device.address in self._connecting:
            return
        asyncio.create_task(self._connect_hub(device.address, device))

    def _make_notification_handler(self, address: str):
        """Build a per-hub notification callback that updates last-seen time
        and decodes Battery Voltage hub-property updates (LWP3)."""

        def handler(_sender, data: bytearray):
            self._last_seen[address] = time.time()
            logger.debug(
                f"Notification from {address}: " f"{' '.join(f'{b:02x}' for b in data)}"
            )
            if (
                len(data) >= 6
                and data[2] == LWP3_MSG_HUB_PROPERTIES
                and data[3] == LWP3_PROP_BATTERY_VOLTAGE
                and data[4] == LWP3_OP_UPDATE
            ):
                self._hub_battery_percentage[address] = data[5]

        return handler

    async def start_status_monitoring(self):
        """
        Start the command queue processor and wait for configured train hubs
        to be connected via `handle_device_seen`.

        If this controller was constructed with its own `adapter`, it also
        starts its own continuous BLE discovery scan here, independent of
        SwitchController's scan. Otherwise `handle_device_seen` is fed by
        SwitchController's scan instead (see the class docstring).
        """
        logger.info("Starting train status monitoring (GATT)...")
        self.running = True
        self.command_task = asyncio.create_task(self._process_commands())

        if not self._known_addresses:
            logger.warning(
                "No train hub addresses configured (TRAIN_HUB_MAPPING) -- "
                "TrainController has nothing to connect to"
            )

        if self._adapter:
            logger.info(f"Starting train discovery scan on adapter {self._adapter}")
            asyncio.create_task(self.scanner.start_scan(self.handle_device_seen))

        while self.running:
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _process_commands(self):
        """Background task to process commands from queue with batching"""
        while self.running:
            try:
                commands = []
                try:
                    while len(commands) < 5:
                        command = self.command_queue.get_nowait()
                        commands.append(command)
                except asyncio.QueueEmpty:
                    if not commands:
                        commands.append(await self.command_queue.get())

                for command in commands:
                    await self._execute_command(command)
                    self.command_queue.task_done()

                await asyncio.sleep(0.02)

            except Exception as e:
                logger.error(f"Error processing command batch: {e}", exc_info=True)

    async def _execute_command(self, command):
        """Send a single motor power command over the hub's GATT connection."""
        hub_id, power, result_future = command
        client = self._clients.get(hub_id)
        if client is None or not client.is_connected:
            logger.warning(f"Cannot send command to train {hub_id}: not connected")
            if not result_future.done():
                result_future.set_exception(
                    ConnectionError(f"Train {hub_id} is not connected")
                )
            return

        power_byte = (256 + power) if power < 0 else power
        payload = bytes([0x08, 0x00, 0x81, PORT_A, 0x11, 0x51, 0x00, power_byte])

        try:
            await client.write_gatt_char(LEGO_HUB_CHAR, payload, response=True)
            self._last_command_time[hub_id] = time.time()
            if not result_future.done():
                result_future.set_result(None)
        except Exception as e:
            logger.error(f"Error sending motor command to {hub_id}: {e}", exc_info=True)
            if not result_future.done():
                result_future.set_exception(e)

    async def handle_command(self, hub_id: str, power: int):
        """Queue a power command and wait for it to actually reach the hub."""
        if hub_id not in self._connection_state:
            available_trains = list(self._connection_state.keys())
            raise ValueError(
                f"Train {hub_id} not found. Available trains: {available_trains}"
            )

        logger.info(f"Setting train {hub_id} power to: {power}%")
        self.mark_train_active(hub_id)
        clamped_power = max(min(power, 100), -100)
        result_future = asyncio.get_running_loop().create_future()
        await self.command_queue.put((hub_id, clamped_power, result_future))
        asyncio.create_task(self._mark_inactive_later(hub_id))
        await result_future

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_connected_trains(self) -> dict:
        """Return information about all known trains, keyed by BLE address."""
        try:
            current_time = time.time()
            trains = {}

            for address, state in self._connection_state.items():
                last_seen = self._last_seen.get(address)
                trains[address] = {
                    "connected": state == "connected",
                    "state": state,
                    "name": self._hub_names.get(address, "LEGO Hub"),
                    "rssi": self._hub_rssi.get(address),
                    "last_update_seconds_ago": (
                        round(current_time - last_seen, 2)
                        if last_seen is not None
                        else None
                    ),
                    "last_command_time": self._last_command_time.get(address),
                    "active": address in self._active_trains,
                    "battery_percentage": self._hub_battery_percentage.get(address),
                }

            return trains
        except Exception as e:
            logger.error(f"Error in get_connected_trains: {e}", exc_info=True)
            return {}

    async def stop_status_monitoring(self):
        """Stop monitoring for status updates"""
        logger.info("Stopping train status monitoring...")
        self.running = False
        if self._adapter:
            await self.scanner.stop_scan()
        if self.command_task:
            self.command_task.cancel()
            try:
                await self.command_task
            except asyncio.CancelledError:
                pass
        for address, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {address}: {e}")
        self._clients.clear()
        logger.info("Train monitor stopped")
