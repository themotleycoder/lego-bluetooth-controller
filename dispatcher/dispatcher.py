"""
Main orchestrator for the RFID dispatcher.

Consumes RFID tag events from the MQTT bridge, updates the track model,
enforces block/switch locking via BlockManager, and drives trains through
the existing BLE-based TrainController/SwitchController. Manual REST
commands (POST /train, /selfdrive) bypass the dispatcher entirely in this
version -- operators are responsible for not issuing conflicting manual
commands to a train under dispatcher control.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from config import Settings
from controllers.switch_controller import SwitchController
from controllers.train_controller import TrainController
from dispatcher.block_manager import BlockManager
from dispatcher.mqtt_bridge import MqttBridge, TagEvent
from dispatcher.track_model import TrackEdge, TrackModel
from utils.logging_config import get_logger

logger = get_logger(__name__)


class Dispatcher:
    """
    Event-driven orchestrator: on each tag read it updates the train's
    position, releases the block it just left (advancing any queued train),
    and either advances the reporting train onto its next block or stops it
    if that block isn't free. A watchdog stops every train if one goes too
    long without an expected tag, and auto-clears once that train's tag
    reappears.
    """

    def __init__(
        self,
        track_model: TrackModel,
        block_manager: BlockManager,
        bridge: MqttBridge,
        train_controller: TrainController,
        switch_controller: SwitchController,
        settings: Settings,
    ) -> None:
        """Wire together the components; does not start anything yet."""
        self._track_model = track_model
        self._block_manager = block_manager
        self._bridge = bridge
        self._train_controller = train_controller
        self._switch_controller = switch_controller
        self._settings = settings

        self.running = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._emergency = False
        self._emergency_train_id: Optional[str] = None
        self._pending_edge: Dict[str, TrackEdge] = {}

    async def run(self) -> None:
        """Start the MQTT bridge and process tag events until `stop()`."""
        await self._bridge.start()
        self.running = True
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self._consumer_task = asyncio.create_task(self._consume_events())

        logger.info("Dispatcher started")
        await self._consumer_task

    async def _consume_events(self) -> None:
        """Process tag events forever; cancelled directly by `stop()`."""
        async for event in self._bridge.events():
            await self._handle_tag_event(event)

    async def stop(self) -> None:
        """Stop watchdog monitoring, event consumption, and the MQTT bridge."""
        self.running = False
        for task in (self._consumer_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._bridge.stop()
        logger.info("Dispatcher stopped")

    async def _handle_tag_event(self, event: TagEvent) -> None:
        """Process a single RFID tag read from a train."""
        if event.train_id not in self._track_model.trains:
            logger.warning(
                f"Ignoring tag event from unregistered train: {event.train_id}"
            )
            return

        tag_id = self._track_model.tag_id_for_uid(event.tag_uid)
        if tag_id is None:
            logger.warning(
                f"Ignoring unknown RFID UID {event.tag_uid} from train {event.train_id}"
            )
            return

        was_emergency_train = event.train_id == self._emergency_train_id

        result = self._track_model.record_tag_event(
            event.train_id, tag_id, event.timestamp
        )

        if was_emergency_train and self._emergency:
            logger.warning(
                f"Train {event.train_id} reappeared after emergency stop; "
                "clearing failsafe and resuming all trains"
            )
            self._emergency = False
            self._emergency_train_id = None
            await self._resume_all_after_emergency()

        if result.edge_completed is not None:
            retry_train = await self._block_manager.release(
                event.train_id, result.edge_completed
            )
            if retry_train is not None:
                retry_edge = self._track_model.next_edge_for_train(retry_train)
                await self._attempt_advance(retry_train, retry_edge)

        if self._emergency:
            return

        next_edge = self._track_model.next_edge_for_train(event.train_id)
        await self._attempt_advance(event.train_id, next_edge)

    async def _attempt_advance(self, train_id: str, edge: Optional[TrackEdge]) -> None:
        """Try to move `train_id` onto `edge`; stop it if that isn't possible."""
        if edge is None:
            return

        self._pending_edge[train_id] = edge

        granted = await self._block_manager.request_entry(train_id, edge)
        if not granted:
            await self._stop_train(train_id)
            return

        switches_ok = await self._block_manager.set_switches_for_edge(
            edge, self._switch_controller
        )
        if not switches_ok:
            # Unsafe to proceed with a partially-set switch: give back the
            # block we just reserved and hold the train.
            await self._block_manager.release(train_id, edge)
            await self._stop_train(train_id)
            return

        self._pending_edge.pop(train_id, None)
        await self._resume_train(train_id)

    async def _stop_train(self, train_id: str) -> None:
        """Stop a train and mark it as intentionally stopped."""
        train = self._track_model.trains.get(train_id)
        if train is None:
            return
        self._track_model.mark_stopped(train_id, True)
        try:
            await self._train_controller.handle_command(train.hub_id, 0)
        except ValueError as e:
            logger.warning(f"Could not stop train {train_id}: {e}")

    async def _resume_train(self, train_id: str) -> None:
        """Resume a train at the configured cruise power."""
        train = self._track_model.trains.get(train_id)
        if train is None:
            return
        self._track_model.mark_stopped(train_id, False)
        try:
            await self._train_controller.handle_command(
                train.hub_id, self._settings.dispatcher_cruise_power
            )
        except ValueError as e:
            logger.warning(f"Could not resume train {train_id}: {e}")

    async def _resume_all_after_emergency(self) -> None:
        """Re-evaluate every train's next edge after an e-stop auto-clears."""
        for train_id in list(self._track_model.trains.keys()):
            edge = self._pending_edge.get(
                train_id
            ) or self._track_model.next_edge_for_train(train_id)
            await self._attempt_advance(train_id, edge)

    async def _watchdog_loop(self) -> None:
        """Periodically check for trains that have missed an expected tag."""
        while self.running:
            await asyncio.sleep(self._settings.dispatcher_watchdog_check_interval)
            if self._emergency:
                continue
            for train_id in list(self._track_model.trains.keys()):
                if not self._track_model.is_moving(train_id):
                    continue
                elapsed = self._track_model.seconds_since_last_tag(train_id)
                if elapsed > self._settings.dispatcher_watchdog_timeout:
                    await self._emergency_stop_all(train_id)
                    break

    async def _emergency_stop_all(self, stalled_train_id: str) -> None:
        """Stop every train; latches until the stalled train's tag reappears."""
        logger.critical(
            f"Watchdog timeout: train {stalled_train_id} missed its expected "
            "tag. Stopping all trains."
        )
        self._emergency = True
        self._emergency_train_id = stalled_train_id
        for train_id in list(self._track_model.trains.keys()):
            await self._stop_train(train_id)
