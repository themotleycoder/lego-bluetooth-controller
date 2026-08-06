"""Tests for dispatcher.dispatcher."""

import asyncio
import time
from typing import AsyncIterator, Optional
from unittest.mock import AsyncMock

from config import Settings
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import TagEvent
from dispatcher.track_model import TrackModel


class FakeBridge:
    """Minimal MqttBridge stand-in driven directly by tests."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[TagEvent]" = asyncio.Queue()
        self.published = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def push(self, event: TagEvent) -> None:
        await self._queue.put(event)

    async def events(self) -> AsyncIterator[TagEvent]:
        while True:
            yield await self._queue.get()

    def publish_command(
        self, train_id: str, action: str, value: Optional[int] = None
    ) -> None:
        self.published.append((train_id, action, value))


def build_two_train_model() -> TrackModel:
    """
    TRN-A and TRN-B both start needing the real A->H block (BLK_AH, requires
    switch "A" STRAIGHT, sensor 4) -- a shared single-track segment two
    trains contend for, exercising block protection end-to-end.
    """
    model = TrackModel()
    model.configure_switch_wiring("A", hub_id=1, port_name="SWITCH_A")
    model.register_train("TRN-A", hub_id=12, route=["A", "H"])
    model.register_train("TRN-B", hub_id=22, route=["A", "H"])
    return model


def build_settings(**overrides) -> Settings:
    defaults = dict(
        dispatcher_watchdog_timeout=0.1,
        dispatcher_watchdog_check_interval=0.02,
        dispatcher_cruise_power=40,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_train_controller() -> AsyncMock:
    tc = AsyncMock()
    tc.handle_command = AsyncMock()
    return tc


def make_switch_controller() -> AsyncMock:
    sc = AsyncMock()
    sc.send_command_with_retry = AsyncMock(return_value=True)
    return sc


def build_dispatcher(model=None, settings=None):
    model = model or build_two_train_model()
    settings = settings or build_settings()
    bm = BlockManager(model)
    bridge = FakeBridge()
    train_controller = make_train_controller()
    switch_controller = make_switch_controller()
    dispatcher = Dispatcher(
        model, bm, bridge, train_controller, switch_controller, settings
    )
    return dispatcher, model, bridge, train_controller, switch_controller


class TestTagEventHandling:
    async def test_resuming_a_train_uses_cruise_power(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        # Tag value is arbitrary here -- nothing is pending yet, so it's
        # ignored for position purposes, but the dispatcher still grants the
        # train's first chain (A->H) unconditionally afterward.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))

        train_controller.handle_command.assert_awaited_with(
            12, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_switches_are_set_before_the_train_is_allowed_through(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # grants A->H
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "4", 2.0))  # confirms it

        switch_controller.send_command_with_retry.assert_awaited_with(1, "SWITCH_A", 0)

    async def test_second_train_is_stopped_when_shared_block_is_occupied(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # grants A->H
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # denied

        train_controller.handle_command.assert_awaited_with(22, 0)

    async def test_queued_train_resumes_once_block_is_released(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # queued
        train_controller.handle_command.reset_mock()

        await dispatcher._handle_tag_event(
            TagEvent("TRN-A", "4", 2.0)
        )  # releases BLK_AH

        train_controller.handle_command.assert_any_await(
            22, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_unregistered_train_is_ignored(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("GHOST", "1", 1.0))

        train_controller.handle_command.assert_not_awaited()

    async def test_unknown_tag_uid_is_ignored(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "NOPE", 1.0))

        train_controller.handle_command.assert_not_awaited()


class TestWatchdog:
    async def test_moving_train_missing_a_tag_stops_every_train(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()
        for train_id in model.trains:
            model.mark_tag_seen(train_id, timestamp=time.time())
            model.mark_stopped(train_id, False)

        watchdog_task = asyncio.create_task(dispatcher._watchdog_loop())
        dispatcher.running = True
        try:
            await asyncio.sleep(0.3)
            assert dispatcher._emergency is True
            stopped_hub_ids = {
                call.args[0]
                for call in train_controller.handle_command.await_args_list
                if call.args[1] == 0
            }
            assert stopped_hub_ids == {12, 22}
        finally:
            dispatcher.running = False
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

    async def test_stalled_trains_tag_clears_emergency_and_resumes_all(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()
        for train_id in model.trains:
            model.mark_tag_seen(train_id, timestamp=time.time())
            model.mark_stopped(train_id, False)

        watchdog_task = asyncio.create_task(dispatcher._watchdog_loop())
        dispatcher.running = True
        try:
            await asyncio.sleep(0.3)
            assert dispatcher._emergency is True
            stalled_train_id = dispatcher._emergency_train_id
            assert stalled_train_id is not None

            train_controller.handle_command.reset_mock()
            await dispatcher._handle_tag_event(TagEvent(stalled_train_id, "1", 1000.0))

            assert dispatcher._emergency is False
            assert dispatcher._emergency_train_id is None
        finally:
            dispatcher.running = False
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass


class TestRunAndStop:
    async def test_stop_terminates_run_promptly(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        run_task = asyncio.create_task(dispatcher.run())
        await asyncio.sleep(0.05)  # let run() start consuming events

        await asyncio.wait_for(dispatcher.stop(), timeout=2)

        # stop() cancels the consumer task run() is awaiting, so run_task
        # itself ends up cancelled too -- expected and harmless, since
        # production never awaits the fire-and-forget task it's wrapped in.
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except asyncio.CancelledError:
            pass

        assert dispatcher.running is False
        assert run_task.cancelled()
