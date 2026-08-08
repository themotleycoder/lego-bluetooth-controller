import asyncio
import time
from typing import Dict, Iterable, Optional

from bleak import BleakClient

from servers.bluetooth_scanner import BetterBleScanner
from utils.constants import LEGO_HUB_CHAR, PORT_A
from utils.logging_config import get_logger

logger = get_logger(__name__)


class TrainController:
    """
    Controls LEGO trains running stock hub firmware over a direct GATT
    connection (LEGO Wireless Protocol 3.0), rather than the Pybricks
    broadcast/observe protocol used for switches.

    Hubs are identified by their BLE address (e.g. "90:84:2B:18:28:36") and
    connected to directly -- no BLE discovery scan is used. A scan would
    require its own BlueZ "StartDiscovery" session, which collides with
    SwitchController's continuous scan on the same adapter/D-Bus connection
    (BlueZ rejects a second concurrent discovery request from the same
    client with "Operation already in progress", and it never recovers on
    its own since the switch scan runs indefinitely). Connecting by known
    address instead uses BlueZ's Connect(), which doesn't require an active
    discovery session, so it coexists fine with switch scanning.
    """

    def __init__(
        self,
        known_addresses: Optional[Iterable[str]] = None,
        max_reconnect_attempts: int = 5,
        reconnect_delay: float = 3.0,
    ):
        self.running = True
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self._known_addresses = list(known_addresses or [])

        # Adapter-level reset only (bluetoothctl power cycle).
        self._adapter = BetterBleScanner()

        self._clients: Dict[str, BleakClient] = {}
        self._connecting: set = set()
        self._connection_state: Dict[str, str] = {}  # address -> state
        self._hub_names: Dict[str, str] = {}
        self._hub_rssi: Dict[str, Optional[int]] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_command_time: Dict[str, float] = {}

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
        await self._adapter.reset_bluetooth()

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

    async def _connect_hub(self, address: str):
        """Connect to a hub by address and subscribe to status notifications, with retry."""
        if address in self._clients or address in self._connecting:
            return

        self._connecting.add(address)
        self._connection_state[address] = "connecting"
        self._hub_names.setdefault(address, "LEGO Hub")
        try:
            for attempt in range(self.max_reconnect_attempts):
                try:
                    client = BleakClient(
                        address,
                        disconnected_callback=lambda c, addr=address: self._on_disconnect(
                            addr
                        ),
                    )
                    await client.connect()
                    await client.start_notify(
                        LEGO_HUB_CHAR, self._make_notification_handler(address)
                    )

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

    def _on_disconnect(self, address: str):
        """Handle an unexpected hub disconnect by clearing state and reconnecting."""
        logger.warning(f"Train hub {address} disconnected")
        self._connection_state[address] = "disconnected"
        self._clients.pop(address, None)
        if self.running:
            asyncio.create_task(self._connect_hub(address))

    def _make_notification_handler(self, address: str):
        """Build a per-hub notification callback that updates last-seen time."""

        def handler(_sender, data: bytearray):
            self._last_seen[address] = time.time()
            logger.debug(
                f"Notification from {address}: " f"{' '.join(f'{b:02x}' for b in data)}"
            )

        return handler

    async def start_status_monitoring(self):
        """Connect to every configured train hub and keep the queue processor running."""
        logger.info("Starting train status monitoring (GATT)...")
        self.running = True
        self.command_task = asyncio.create_task(self._process_commands())

        if not self._known_addresses:
            logger.warning(
                "No train hub addresses configured (TRAIN_HUB_MAPPING) -- "
                "TrainController has nothing to connect to"
            )

        for address in self._known_addresses:
            asyncio.create_task(self._connect_hub(address))

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
        hub_id, power = command
        client = self._clients.get(hub_id)
        if client is None or not client.is_connected:
            logger.warning(f"Cannot send command to train {hub_id}: not connected")
            return

        power_byte = (256 + power) if power < 0 else power
        payload = bytes([0x08, 0x00, 0x81, PORT_A, 0x11, 0x51, 0x00, power_byte])

        try:
            await client.write_gatt_char(LEGO_HUB_CHAR, payload, response=True)
            self._last_command_time[hub_id] = time.time()
        except Exception as e:
            logger.error(f"Error sending motor command to {hub_id}: {e}", exc_info=True)
            raise

    async def handle_command(self, hub_id: str, power: int):
        """Queue a power command for processing"""
        try:
            if hub_id not in self._connection_state:
                available_trains = list(self._connection_state.keys())
                raise ValueError(
                    f"Train {hub_id} not found. Available trains: {available_trains}"
                )

            logger.info(f"Setting train {hub_id} power to: {power}%")
            self.mark_train_active(hub_id)
            clamped_power = max(min(power, 100), -100)
            await self.command_queue.put((hub_id, clamped_power))

            asyncio.create_task(self._mark_inactive_later(hub_id))

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Error queueing train command: {e}", exc_info=True)
            raise

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
                }

            return trains
        except Exception as e:
            logger.error(f"Error in get_connected_trains: {e}", exc_info=True)
            return {}

    async def stop_status_monitoring(self):
        """Stop monitoring for status updates"""
        logger.info("Stopping train status monitoring...")
        self.running = False
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
