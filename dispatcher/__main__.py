"""
Standalone entry point for exercising the dispatcher state machine without
real hardware or an MQTT broker.

Usage:
    python -m dispatcher --mock
    python -m dispatcher --mock --scenario path/to/scenario.json
    python -m dispatcher --mock --duration 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any, AsyncIterator, List, Optional

from config import get_settings
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import TagEvent
from dispatcher.track_model import TrackModel
from utils.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


# Demo-only wiring/routes for --mock runs: TRN-A takes the inner loop
# (C-A-H-F-E-D), TRN-B takes the outer loop (B-J-I-F-E), sharing the F-E
# block between the two loops -- exercising block contention too. Real
# deployments configure this via Settings.switch_wiring / .train_routes
# instead (see dispatcher/factory.py::build_dispatcher).
DEMO_SWITCH_WIRING = {
    "A": (101, "SWITCH_A"),
    "B": (101, "SWITCH_B"),
    "C": (101, "SWITCH_C"),
    "F": (102, "SWITCH_A"),
    "H": (102, "SWITCH_B"),
    "I": (102, "SWITCH_C"),
    "J": (102, "SWITCH_D"),
}
DEMO_TRAIN_ROUTES = {
    "TRN-A": (12, ["C", "A", "H", "F", "E", "D"]),
    "TRN-B": (22, ["B", "J", "I", "F", "E"]),
}

# Sensor ids (as strings) a train reports along its route -- see the sensor
# ids on each edge in TrackModel._build_edges. Each entry marks the end of a
# chain of blocks (some with no sensor of their own) that gets confirmed
# together; see TrackModel.next_block_chain_for_train.
DEFAULT_SCENARIO: List[dict] = [
    {"delay_s": 0.2, "train_id": "TRN-A", "tag_uid": "4"},  # ends chain [CA, AH]
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "1"},  # ends chain [BJ]
    {
        "delay_s": 1.0,
        "train_id": "TRN-A",
        "tag_uid": "6",
    },  # ends chain [HF, FE, ED, DC]
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "3"},  # ends chain [JI_S]
    {"delay_s": 1.0, "train_id": "TRN-A", "tag_uid": "4"},  # ends chain [CA, AH]
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "5"},  # ends chain [IF, FE, EB]
    {
        "delay_s": 1.0,
        "train_id": "TRN-A",
        "tag_uid": "6",
    },  # ends chain [HF, FE, ED, DC]
]


def build_demo_track_model() -> TrackModel:
    """Build the real TrackModel with demo wiring/routes for --mock runs."""
    track_model = TrackModel()
    for switch_id, (hub_id, port_name) in DEMO_SWITCH_WIRING.items():
        track_model.configure_switch_wiring(switch_id, hub_id, port_name)
    for train_id, (hub_id, route) in DEMO_TRAIN_ROUTES.items():
        track_model.register_train(train_id, hub_id, route)
    return track_model


class FakeMqttBridge:
    """Replays a scripted sequence of tag events; no real broker involved."""

    def __init__(self, scenario: List[dict]) -> None:
        """Store the scenario to replay once started."""
        self._scenario = scenario
        self._queue: "asyncio.Queue[TagEvent]" = asyncio.Queue()
        self._player_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Begin replaying the scenario in the background."""
        self._player_task = asyncio.create_task(self._play())

    async def _play(self) -> None:
        for step in self._scenario:
            await asyncio.sleep(step["delay_s"])
            event = TagEvent(
                train_id=step["train_id"],
                tag_uid=step["tag_uid"],
                timestamp=time.time(),
            )
            logger.info(f"[mock] publishing {event}")
            await self._queue.put(event)

    async def events(self) -> AsyncIterator[TagEvent]:
        """Yield replayed tag events as they're scheduled."""
        while True:
            yield await self._queue.get()

    def publish_command(
        self, train_id: str, action: str, value: Optional[int] = None
    ) -> None:
        """Log the command instead of publishing to a real broker."""
        logger.info(f"[mock] command -> {train_id}: {action} {value}")

    async def stop(self) -> None:
        """Cancel the scenario player."""
        if self._player_task is not None:
            self._player_task.cancel()
            try:
                await self._player_task
            except asyncio.CancelledError:
                pass


class StubTrainController:
    """Logs train commands instead of touching BLE hardware."""

    def __init__(self) -> None:
        """Pretend both sample-topology trains are already BLE-registered."""
        self.train_statuses = {12: {}, 22: {}}

    async def handle_command(self, hub_id: str, power: int) -> None:
        """Log the power command that would have been sent over BLE."""
        logger.info(
            f"[mock] TrainController.handle_command(hub_id={hub_id}, power={power})"
        )


class StubSwitchController:
    """Logs switch commands instead of touching BLE hardware."""

    async def send_command_with_retry(
        self, hub_id: int, switch_name: str, position: int, max_retries: int = 3
    ) -> bool:
        """Log the switch command and report success."""
        logger.info(
            f"[mock] SwitchController.send_command_with_retry(hub_id={hub_id}, "
            f"switch_name={switch_name}, position={position})"
        )
        return True


def _load_scenario(path: Optional[str]) -> List[dict]:
    """Load a JSON scenario file, or fall back to the built-in default."""
    if not path:
        return DEFAULT_SCENARIO
    with open(path) as f:
        return json.load(f)


async def _run(scenario_path: Optional[str], duration: Optional[float]) -> None:
    settings = get_settings()
    track_model = build_demo_track_model()
    block_manager = BlockManager(track_model)
    bridge = FakeMqttBridge(_load_scenario(scenario_path))
    dispatcher = Dispatcher(
        track_model=track_model,
        block_manager=block_manager,
        bridge=bridge,  # type: ignore[arg-type]
        train_controller=StubTrainController(),  # type: ignore[arg-type]
        switch_controller=StubSwitchController(),  # type: ignore[arg-type]
        settings=settings,
    )

    try:
        if duration:
            try:
                await asyncio.wait_for(dispatcher.run(), timeout=duration)
            except asyncio.TimeoutError:
                logger.info(f"Mock run duration ({duration}s) elapsed")
        else:
            await dispatcher.run()
    except KeyboardInterrupt:
        pass
    finally:
        await dispatcher.stop()
        logger.info(f"Final train positions: {track_model.train_position}")
        occupied = {
            bid: block.occupied_by
            for bid, block in track_model.blocks.items()
            if block.occupied_by is not None
        }
        logger.info(f"Final occupied blocks: {occupied}")


def main() -> None:
    """Parse CLI args and run the dispatcher standalone."""
    parser = argparse.ArgumentParser(description="Run the RFID dispatcher standalone")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use a fake MQTT bridge and stub controllers instead of real hardware",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="Path to a JSON scenario file overriding the default mock tag sequence",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Exit automatically after this many seconds (default: run until Ctrl+C)",
    )
    args = parser.parse_args()

    if not args.mock:
        parser.error("Only --mock mode is currently supported for standalone runs")

    setup_logging()
    try:
        asyncio.run(_run(args.scenario, args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
