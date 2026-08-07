import asyncio
from enum import Enum
from typing import Dict, List, Optional
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from utils.constants import LEGO_HUB_SERVICE, LEGO_HUB_CHAR


class HubConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class ConnectedHub:
    def __init__(self, device: BLEDevice):
        self.device = device
        self.state = HubConnectionState.DISCONNECTED
        self._state_callbacks = []
        self.client: Optional[BleakClient] = None

    def add_state_callback(self, callback):
        self._state_callbacks.append(callback)

    def remove_state_callback(self, callback):
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def update_state(self, new_state: HubConnectionState):
        self.state = new_state
        for callback in self._state_callbacks:
            callback(new_state)


class LegoService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LegoService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._connected_hubs: Dict[str, ConnectedHub] = {}
        self.max_connections = 2  # Limit to 2 trains

    @property
    def can_connect_more(self) -> bool:
        return len(self._connected_hubs) < self.max_connections

    @property
    def connected_hubs(self) -> List[ConnectedHub]:
        return list(self._connected_hubs.values())

    def _notification_handler(self, sender: int, data: bytearray):
        """Handle incoming notifications from the hub."""
        print(f'Received value: {" ".join([f"{b:02x}" for b in data])}')

    async def scan_for_devices(self, timeout: int = 10) -> List[BLEDevice]:
        """Scan for LEGO Hub devices."""
        devices = []
        seen_devices = set()

        def _device_filter(device: BLEDevice):
            return (
                LEGO_HUB_SERVICE.lower()
                in (service.lower() for service in (device.metadata.get("uuids", [])))
                if device.metadata.get("uuids")
                else False
            )

        try:
            print("Scanning for LEGO Hubs...")
            async with BleakScanner(detection_callback=lambda d, a: None) as scanner:
                await asyncio.sleep(timeout)
                for device in await scanner.get_discovered_devices():
                    if (
                        _device_filter(device)
                        and device.address not in seen_devices
                        and device.address not in self._connected_hubs
                    ):
                        print(f"Found LEGO Hub: {device.name}")
                        seen_devices.add(device.address)
                        devices.append(device)
        except Exception as e:
            print(f"Scan error: {e}")
            raise

        return devices

    async def connect(self, device: BLEDevice):
        """Connect to a LEGO Hub device."""
        if not self.can_connect_more:
            raise Exception("Maximum number of connections reached")

        hub = ConnectedHub(device)
        self._connected_hubs[device.address] = hub
        hub.update_state(HubConnectionState.CONNECTING)

        max_retries = 3
        retry_delay = 1  # seconds
        last_exception = None

        for attempt in range(max_retries):
            try:
                print(
                    f"Connecting to {device.name} (attempt {attempt + 1}/{max_retries})..."
                )
                client = BleakClient(device)
                hub.client = client

                await client.connect()
                await client.start_notify(LEGO_HUB_CHAR, self._notification_handler)

                hub.update_state(HubConnectionState.CONNECTED)
                print(f"Successfully connected to {device.name}")
                return  # Success - exit the retry loop

            except Exception as e:
                last_exception = e
                print(f"Connection error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:  # Don't wait after the last attempt
                    await asyncio.sleep(retry_delay)
                    continue

                # Final attempt failed
                hub.update_state(HubConnectionState.ERROR)
                await self.disconnect(device.address)
                raise last_exception

    async def disconnect(self, device_address: str):
        """Disconnect from a LEGO Hub device."""
        try:
            hub = self._connected_hubs.get(device_address)
            if hub and hub.client:
                await hub.client.disconnect()
                hub.update_state(HubConnectionState.DISCONNECTED)
                self._connected_hubs.pop(device_address)
        except Exception as e:
            print(f"Disconnect error: {e}")

    async def disconnect_all(self):
        """Disconnect from all connected LEGO Hub devices."""
        for device_address in list(self._connected_hubs.keys()):
            await self.disconnect(device_address)

    async def set_motor_power(self, device_address: str, port: int, power: int):
        """Set the power of a motor."""
        hub = self._connected_hubs.get(device_address)
        if not hub or hub.state != HubConnectionState.CONNECTED:
            raise Exception("Device not connected")

        # Clamp power between -100 and 100
        power = max(-100, min(100, power))
        power_byte = (256 + power) if power < 0 else power

        # Command format according to LWP 3.0:
        # [Length][Hub ID][Port Output Command][Port ID][Startup & Completion][Write Direct Mode][Mode][Power]
        command = bytearray(
            [
                0x08,  # Length (8 bytes)
                0x00,  # Hub ID
                0x81,  # Port Output Command
                port,  # Port ID
                0x11,  # Execute Immediately (0x11)
                0x51,  # Write Direct Mode
                0x00,  # Mode = 0 (setPower)
                power_byte,  # Power value (-100 to 100)
            ]
        )

        print(f'Sending motor command: {" ".join([f"0x{b:02x}" for b in command])}')

        try:
            await hub.client.write_gatt_char(LEGO_HUB_CHAR, command, response=True)
            print("Motor command sent successfully")
        except Exception as e:
            print(f"Error sending motor command: {e}")
            raise

    async def stop_motor(self, device_address: str, port: int):
        """Stop a motor."""
        await self.set_motor_power(device_address, port, 0)

    async def rotate_motor(
        self,
        device_address: str,
        port: int,
        direction: str,
        power: int,
        duration: float,
    ):
        """
        Rotate a motor in the specified direction for a given duration.

        Args:
            device_address: The hub's device address
            port: The motor port number
            direction: Either 'forwards' or 'backwards'
            power: Power level (0-100)
            duration: Time to rotate in seconds
        """
        if direction not in ["forwards", "backwards"]:
            raise ValueError("Direction must be 'forwards' or 'backwards'")

        # Convert direction to power (forwards = positive, backwards = negative)
        actual_power = abs(power) if direction == "forwards" else -abs(power)

        try:
            # Start motor
            await self.set_motor_power(device_address, port, actual_power)
            # Wait for specified duration
            await asyncio.sleep(duration)
            # Stop motor
            await self.stop_motor(device_address, port)
        except Exception as e:
            # Ensure motor is stopped even if there's an error
            await self.stop_motor(device_address, port)
            raise

    async def move_forwards(
        self, device_address: str, port: int, duration: float, power: int = 50
    ):
        """Move a motor forwards for a specified duration."""
        await self.rotate_motor(device_address, port, "forwards", power, duration)

    async def move_backwards(
        self, device_address: str, port: int, duration: float, power: int = 50
    ):
        """Move a motor backwards for a specified duration."""
        await self.rotate_motor(device_address, port, "backwards", power, duration)

    async def control_port(self, device_address: str, port: int, action: str):
        """
        Control a port's open/close state with a quick motor movement.

        Args:
            device_address: The hub's device address
            port: The motor port number
            action: Either 'open' or 'close'
        """
        if action not in ["st", "sw"]:
            raise ValueError("Action must be 'st' or 'sw'")

        # Use quick, powerful movement
        duration = 0.1  # 100ms
        power = 70  # 70% power

        try:
            if action == "st":
                await self.move_backwards(device_address, port, duration, power)
            else:  # close
                await self.move_forwards(device_address, port, duration, power)
        except Exception as e:
            print(f"Error controlling port: {e}")
            raise

    def get_current_state(self, device_address: str) -> HubConnectionState:
        """Get the current connection state of a hub."""
        return self._connected_hubs.get(device_address, ConnectedHub(None)).state


async def process_command(service: LegoService, device: BLEDevice, command: str):
    """Process a user command to control the hub."""
    try:
        parts = command.lower().split()
        if not parts:
            return

        if parts[0] == "help":
            print(
                """
Available commands:
  forward <port> <duration> [power]  - Move motor forward (e.g., 'forward 0 2.0 50')
  backward <port> <duration> [power] - Move motor backward (e.g., 'backward 0 2.0 50')
  stop <port>                       - Stop motor (e.g., 'stop 0')
  power <port> <power>              - Set motor power directly (e.g., 'power 0 50')
  port <port> <open|close>          - Control port state (e.g., 'port 0 open')
  status                            - Show connection status
  quit                              - Exit the program
  help                              - Show this help message
            """.strip()
            )
            return

        if parts[0] == "quit":
            return "quit"

        if parts[0] == "status":
            state = service.get_current_state(device.address)
            print(f"Hub status: {state.value}")
            return

        if parts[0] == "stop" and len(parts) == 2:
            port = int(parts[1])
            await service.stop_motor(device.address, port)
            print(f"Stopped motor on port {port}")
            return

        if parts[0] == "power" and len(parts) == 3:
            port = int(parts[1])
            power = int(parts[2])
            await service.set_motor_power(device.address, port, power)
            print(f"Set motor power on port {port} to {power}")
            return

        if parts[0] == "port" and len(parts) == 3:
            port = int(parts[1])
            action = parts[2]
            if action in ["st", "sw"]:
                await service.control_port(device.address, port, action)
                print(f"Port {port} {action}ed")
                return

        if parts[0] in ["forward", "backward"] and len(parts) >= 3:
            port = int(parts[1])
            duration = float(parts[2])
            power = int(parts[3]) if len(parts) > 3 else 50

            if parts[0] == "forward":
                await service.move_forwards(device.address, port, duration, power)
                print(f"Moved forward on port {port} for {duration}s at power {power}")
            else:
                await service.move_backwards(device.address, port, duration, power)
                print(f"Moved backward on port {port} for {duration}s at power {power}")
            return

        print("Invalid command. Type 'help' for available commands.")

    except (ValueError, IndexError):
        print("Invalid command format. Type 'help' for usage examples.")
    except Exception as e:
        print(f"Error executing command: {e}")


async def main():
    """Interactive command loop for controlling the LEGO hub."""
    service = LegoService()

    try:
        # Scan for devices
        print("Scanning for LEGO Hubs...")
        devices = await service.scan_for_devices()
        if not devices:
            print("No LEGO Hubs found")
            return

        # Connect to the first device found
        device = devices[0]
        print(f"Connecting to {device.name}...")
        await service.connect(device)
        print("\nConnected! Type 'help' for available commands.")

        # Command loop
        while True:
            try:
                command = input("\nEnter command: ").strip()
                result = await process_command(service, device, command)
                if result == "quit":
                    break
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error: {e}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        print("\nDisconnecting...")
        await service.disconnect_all()
        print("Disconnected. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram terminated by user")
