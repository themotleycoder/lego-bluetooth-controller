#!/usr/bin/env python3
import asyncio
import subprocess

from config import get_settings
from controllers.switch_controller import SwitchController
from controllers.train_controller import TrainController


class LegoController:
    def __init__(self):
        known_switch_hub_ids = {
            hub_id for hub_id, _ in get_settings().switch_wiring_dict.values()
        }
        self.switch_controller = SwitchController(known_hub_ids=known_switch_hub_ids)
        known_addresses = get_settings().train_hub_mapping_dict.values()
        self.train_controller = TrainController(known_addresses=known_addresses)
        # TrainController has no scanner of its own -- it connects using
        # devices discovered by the switch controller's scan (the only BLE
        # discovery session in the process; see TrainController's docstring).
        self.switch_controller.set_device_seen_callback(
            self.train_controller.handle_device_seen
        )
        self.running = True

    async def initialize(self):
        """Async initialization method"""
        await self.switch_controller.scanner.reset_bluetooth()

    def extract_number_and_command(self, s: str) -> tuple[int, str]:
        # Find all digits at the start of the string
        number = ""
        pos = 0
        for char in s:
            if char.isdigit():
                number += char
                pos += 1
            else:
                break

        # Get remaining string starting from where numbers ended
        command = s[pos:]

        return int(number) if number else 0, command

    async def run(self):
        """Main run loop with proper task management"""
        print("Starting Lego Controller...")
        self.switch_controller.scanner.reset_bluetooth()
        # self.train_controller.reset_bluetooth()

        try:
            # Create tasks for status monitoring. Only the switch controller
            # scans (continuous BLE discovery); the train controller
            # connects directly to configured hub addresses.
            switch_monitor_task = asyncio.create_task(
                self.switch_controller.start_status_monitoring()
            )
            train_monitor_task = asyncio.create_task(
                self.train_controller.start_status_monitoring()
            )

            print("\nCommands:")
            print("as: Switch A to STRAIGHT")
            print("ad: Switch A to DIVERGING")
            print("bs: Switch B to STRAIGHT")
            print("bd: Switch B to DIVERGING")
            print("\nTrain Commands (address is the train hub's BLE address):")
            print("train <address> stop")
            print("train <address> forward [power]   (default power 40)")
            print("train <address> backward [power]  (default power 40)")
            print("r: Reset Bluetooth")
            print("q: Quit")

            while self.running:
                try:
                    # Use asyncio.create_task for input to prevent blocking
                    raw = await asyncio.get_event_loop().run_in_executor(
                        None, input, "> "
                    )
                    raw = raw.strip()
                    if not raw:
                        continue

                    if raw.lower().startswith("train "):
                        parts = raw.split()
                        if len(parts) < 3:
                            print(
                                "Usage: train <address> <stop|forward|backward> [power]"
                            )
                            continue

                        address = parts[1]
                        action = parts[2].lower()
                        power = (
                            int(parts[3])
                            if len(parts) > 3 and parts[3].isdigit()
                            else 40
                        )

                        if action == "stop":
                            await self.train_controller.handle_command(address, 0)
                        elif action == "forward":
                            await self.train_controller.handle_command(address, power)
                        elif action == "backward":
                            await self.train_controller.handle_command(address, -power)
                        else:
                            print(f"Unknown train action: {action}")
                        continue

                    hub, cmd = self.extract_number_and_command(raw)

                    if cmd.lower() == "q":
                        self.running = False
                    elif cmd.lower() == "r":
                        await self.switch_controller.stop_status_monitoring()
                        await self.train_controller.stop_status_monitoring()
                        self.switch_controller.scanner.reset_bluetooth()
                        await self.train_controller.reset_bluetooth()
                        switch_monitor_task.cancel()  # Cancel old monitoring tasks
                        train_monitor_task.cancel()
                        switch_monitor_task = asyncio.create_task(
                            self.switch_controller.start_status_monitoring()
                        )
                        train_monitor_task = asyncio.create_task(
                            self.train_controller.start_status_monitoring()
                        )
                    elif cmd.lower() == "as":
                        await self.switch_controller.send_command_with_retry(
                            hub, "SWITCH_A", 0
                        )
                    elif cmd.lower() == "ad":
                        await self.switch_controller.send_command_with_retry(
                            hub, "SWITCH_A", 1
                        )
                    elif cmd.lower() == "bs":
                        await self.switch_controller.send_command_with_retry(
                            hub, "SWITCH_B", 0
                        )
                    elif cmd.lower() == "bd":
                        await self.switch_controller.send_command_with_retry(
                            hub, "SWITCH_B", 1
                        )
                    else:
                        print(f"Unknown command: {raw}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"Error processing command: {e}")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            # Clean up
            self.running = False
            await self.switch_controller.stop_status_monitoring()
            await self.train_controller.stop_status_monitoring()
            if "switch_monitor_task" in locals():
                switch_monitor_task.cancel()
            if "train_monitor_task" in locals():
                train_monitor_task.cancel()
            subprocess.run(["sudo", "hcitool", "cmd", "0x08", "0x000A", "00"])


if __name__ == "__main__":
    asyncio.run(LegoController().run())
