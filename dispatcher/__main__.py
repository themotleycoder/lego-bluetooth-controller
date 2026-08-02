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
from dispatcher.track_model import build_sample_topology
from utils.logging_config import get_logger, setup_logging

logger = get_logger(__name__)


DEFAULT_SCENARIO: List[dict] = [
    {"delay_s": 0.2, "train_id": "TRN-A", "tag_uid": "T8"},
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "T1"},
    {"delay_s": 1.0, "train_id": "TRN-A", "tag_uid": "T9"},
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "T2"},
    {"delay_s": 1.0, "train_id": "TRN-A", "tag_uid": "T10"},
    {"delay_s": 1.0, "train_id": "TRN-B", "tag_uid": "T3"},
]


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

    async def handle_command(self, hub_id: int, power: int) -> None:
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
    track_model = build_sample_topology()
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
        logger.info(
            "Final block states: "
            f"{ {eid: s.value for eid, s in track_model.block_state.items()} }"
        )


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
