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


TRN_A_HUB = "90:84:2B:18:28:36"
TRN_B_HUB = "F3:33:66:0C:3A:6A"


def build_two_train_model() -> TrackModel:
    """
    TRN-A and TRN-B both start needing the real B->D block (BLK_BD, requires
    switch "D" STRAIGHT, sensor 1) -- a shared single-track segment two
    trains contend for, exercising block protection end-to-end.
    """
    model = TrackModel()
    model.configure_switch_wiring("D", hub_id=1, port_name="SWITCH_A")
    model.register_train("TRN-A", hub_id=TRN_A_HUB, route=["B", "D"])
    model.register_train("TRN-B", hub_id=TRN_B_HUB, route=["B", "D"])
    # Self-drive defaults to off; these existing tests exercise automatic
    # dispatcher-driven movement, so opt both trains in explicitly (mirrors
    # what checking "Self Drive" in the UI does for a real train).
    model.set_self_drive("TRN-A", True)
    model.set_self_drive("TRN-B", True)
    return model


def build_two_smart_drive_train_model() -> TrackModel:
    """
    Same BLK_BD bottleneck as build_two_train_model, but both trains are
    smart-drive: starting at B, its only safe (non-manual) departure is the
    BD trunk edge, so the first chain each requests is deterministically the
    same as the fixed-route case -- letting the existing contention
    scenario be replayed under dynamic routing.
    """
    model = TrackModel()
    model.configure_switch_wiring("D", hub_id=1, port_name="SWITCH_A")
    model.register_train("TRN-A", hub_id=TRN_A_HUB, smart_drive=True, start_switch="B")
    model.register_train("TRN-B", hub_id=TRN_B_HUB, smart_drive=True, start_switch="B")
    model.set_self_drive("TRN-A", True)
    model.set_self_drive("TRN-B", True)
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
        # train's first chain (B->D) unconditionally afterward.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))

        train_controller.handle_command.assert_awaited_with(
            TRN_A_HUB, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_switches_are_set_before_the_train_is_allowed_through(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # grants B->D
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 2.0))  # confirms it

        switch_controller.send_command_with_retry.assert_awaited_with(1, "SWITCH_A", 0)

    async def test_second_train_is_stopped_when_shared_block_is_occupied(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # grants B->D
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # denied

        train_controller.handle_command.assert_awaited_with(TRN_B_HUB, 0)

    async def test_queued_train_resumes_once_block_is_released(self):
        # TRN-A runs B->D->E; TRN-B only wants B->D, so it queues on BLK_BD.
        model = build_two_train_model()
        model.configure_switch_wiring("E", hub_id=2, port_name="SWITCH_A")
        model.register_train("TRN-A", hub_id=TRN_A_HUB, route=["B", "D", "E"])
        model.set_self_drive("TRN-A", True)
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher(model=model)

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # queued
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 2.0))  # nose on BD
        train_controller.handle_command.reset_mock()

        # TRN-A's tail may still be in BLK_BD, so it stays held through the
        # next chain's confirmation and only frees once the nose is further on.
        assert not model.is_block_free("BLK_BD")
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", 3.0))

        train_controller.handle_command.assert_any_await(
            TRN_B_HUB, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_confirmed_chain_is_held_until_the_next_one_confirms(self):
        dispatcher, model, *_ = build_dispatcher()
        bd, de_s = model.edges["BD"], model.edges["DE_S"]

        assert dispatcher._edges_safe_to_release("TRN-A", [bd]) == []
        assert dispatcher._edges_safe_to_release("TRN-A", [de_s]) == [bd]

    async def test_edge_reused_by_the_next_chain_is_not_released(self):
        dispatcher, model, *_ = build_dispatcher()
        bd = model.edges["BD"]

        dispatcher._edges_safe_to_release("TRN-A", [bd])
        assert dispatcher._edges_safe_to_release("TRN-A", [bd]) == []

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


class TestSmartDriveDispatch:
    async def test_second_smart_drive_train_is_stopped_when_shared_block_is_occupied(
        self,
    ):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher(model=build_two_smart_drive_train_model())

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # grants B->D
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # denied

        train_controller.handle_command.assert_awaited_with(TRN_B_HUB, 0)

    async def test_queued_smart_drive_train_resumes_once_block_is_released(self):
        model = build_two_smart_drive_train_model()
        model.configure_switch_wiring("E", hub_id=2, port_name="SWITCH_A")
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher(model=model)

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # queued
        # Nose reaches D; TRN-A takes the D->E crossover (sensor 7) next.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 2.0))
        train_controller.handle_command.reset_mock()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "7", 3.0))

        train_controller.handle_command.assert_any_await(
            TRN_B_HUB, dispatcher._settings.dispatcher_cruise_power
        )


class TestStartBlocks:
    async def test_enabling_self_drive_holds_every_block_at_the_start_switch(self):
        dispatcher, model, *_ = build_dispatcher(
            model=build_two_smart_drive_train_model()
        )

        await dispatcher.set_self_drive("TRN-A", True)

        for block in ("BLK_BD", "BLK_BK", "BLK_BG"):
            assert model.blocks[block].occupied_by == "TRN-A"

    async def test_start_blocks_are_freed_once_the_first_chain_clears_them(self):
        dispatcher, model, *_ = build_dispatcher(
            model=build_two_smart_drive_train_model()
        )
        await dispatcher.set_self_drive("TRN-A", True)
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # nose on BD

        # BD is TRN-A's own first chain, held until the next confirmation;
        # the other two were only start holds.
        assert model.blocks["BLK_BD"].occupied_by == "TRN-A"
        assert model.is_block_free("BLK_BK")
        assert model.is_block_free("BLK_BG")

    async def test_disabling_self_drive_releases_the_start_blocks(self):
        dispatcher, model, *_ = build_dispatcher(
            model=build_two_smart_drive_train_model()
        )
        await dispatcher.set_self_drive("TRN-A", True)

        await dispatcher.set_self_drive("TRN-A", False)

        assert all(block.occupied_by is None for block in model.blocks.values())

    async def test_overlapping_start_positions_warn_and_leave_blocks_alone(
        self, caplog
    ):
        dispatcher, model, *_ = build_dispatcher(
            model=build_two_smart_drive_train_model()
        )
        await dispatcher.set_self_drive("TRN-A", True)

        await dispatcher.set_self_drive("TRN-B", True)  # also starts at B

        assert "starts at switch B" in caplog.text
        assert model.blocks["BLK_BK"].occupied_by == "TRN-A"


class TestSelfDrive:
    async def test_train_with_self_drive_off_does_not_advance_on_tag_event(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()
        model.set_self_drive("TRN-A", False)

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))

        train_controller.handle_command.assert_not_awaited()

    async def test_enabling_self_drive_immediately_attempts_to_advance(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()
        model.set_self_drive("TRN-A", False)
        train_controller.handle_command.reset_mock()

        result = await dispatcher.set_self_drive("TRN-A", True)

        assert result is True
        assert model.is_self_drive("TRN-A") is True
        train_controller.handle_command.assert_awaited_with(
            TRN_A_HUB, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_disabling_self_drive_stops_the_train_and_releases_its_blocks(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1.0))  # holds BLK_BD
        await dispatcher._handle_tag_event(TagEvent("TRN-B", "1", 1.0))  # queued
        train_controller.handle_command.reset_mock()

        result = await dispatcher.set_self_drive("TRN-A", False)

        assert result is True
        assert model.is_self_drive("TRN-A") is False
        # TRN-A stopped, and TRN-B (queued behind it) got the block instead.
        train_controller.handle_command.assert_any_await(TRN_A_HUB, 0)
        train_controller.handle_command.assert_any_await(
            TRN_B_HUB, dispatcher._settings.dispatcher_cruise_power
        )

    async def test_set_self_drive_for_unknown_train_returns_false(self):
        (
            dispatcher,
            model,
            bridge,
            train_controller,
            switch_controller,
        ) = build_dispatcher()

        result = await dispatcher.set_self_drive("GHOST", True)

        assert result is False


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
            assert stopped_hub_ids == {TRN_A_HUB, TRN_B_HUB}
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
            # Only a read that confirms one of the train's pending edges
            # clears the failsafe -- give it a granted chain to confirm.
            model.grant_pending_chain(
                stalled_train_id, model.next_block_chain_for_train(stalled_train_id)
            )
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


class TestUnexpectedTag:
    async def _start_trn_a(self):
        built = build_dispatcher()
        dispatcher, model, bridge, train_controller, switch_controller = built
        # First read kicks off TRN-A: it is granted BD and starts moving.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", time.time()))
        assert model.is_moving("TRN-A")
        assert model.has_pending_edges("TRN-A")
        train_controller.handle_command.reset_mock()
        return built

    async def test_unexpected_sensor_from_moving_train_stops_every_train(self):
        dispatcher, model, bridge, train_controller, _ = await self._start_trn_a()

        # TAG_3 is on D->E, not the B->D block TRN-A was granted.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        assert dispatcher._emergency is True
        assert dispatcher._emergency_train_id == "TRN-A"
        stopped = {
            call.args[0]
            for call in train_controller.handle_command.await_args_list
            if call.args[1] == 0
        }
        assert stopped == {TRN_A_HUB, TRN_B_HUB}

    async def test_unexpected_sensor_never_resumes_the_train(self):
        dispatcher, model, bridge, train_controller, _ = await self._start_trn_a()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        powers = [c.args[1] for c in train_controller.handle_command.await_args_list]
        assert all(power == 0 for power in powers)

    async def test_further_unexpected_reads_do_not_clear_the_emergency(self):
        dispatcher, model, bridge, train_controller, _ = await self._start_trn_a()
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))
        train_controller.handle_command.reset_mock()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "5", time.time()))

        assert dispatcher._emergency is True
        train_controller.handle_command.assert_not_awaited()

    async def test_unexpected_sensor_from_stationary_train_is_ignored(self):
        dispatcher, model, bridge, train_controller, _ = build_dispatcher()
        model.mark_stopped("TRN-A", True)

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        assert dispatcher._emergency is False


class TestMissedReadRecovery:
    def _model(self) -> TrackModel:
        model = TrackModel()
        model.configure_switch_wiring("D", hub_id=1, port_name="SWITCH_A")
        model.configure_switch_wiring("E", hub_id=2, port_name="SWITCH_A")
        model.register_train("TRN-A", hub_id=TRN_A_HUB, route=["B", "D", "E"])
        model.register_train("TRN-B", hub_id=TRN_B_HUB, route=["B", "D", "E"])
        model.set_self_drive("TRN-A", True)
        model.set_self_drive("TRN-B", False)
        return model

    async def _kicked_off(self):
        built = build_dispatcher(model=self._model())
        dispatcher, model, *_ = built
        # First read starts TRN-A on B->D (sensor 1); it is now moving.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", time.time()))
        assert model.is_moving("TRN-A") and model.has_pending_edges("TRN-A")
        return built

    async def test_missing_one_sensor_is_tolerated_when_the_next_one_is_read(self):
        dispatcher, model, bridge, train_controller, _ = await self._kicked_off()
        train_controller.handle_command.reset_mock()

        # TRN-A never reports sensor 1, then reads sensor 3 (D->E).
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        assert dispatcher._emergency is False
        assert model.train_position["TRN-A"] == "E"
        assert all(
            call.args[1] != 0
            for call in train_controller.handle_command.await_args_list
        )

    async def test_skipped_blocks_stay_held_after_recovery(self):
        dispatcher, model, *_ = await self._kicked_off()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        # The nose has only just reached sensor 3, so the tail may still be
        # in either block; both are freed once the next chain confirms.
        assert not model.is_block_free("BLK_BD")
        assert not model.is_block_free("BLK_DE_S")

    async def test_skipping_into_a_block_held_by_another_train_stops_all(self):
        dispatcher, model, bridge, train_controller, _ = await self._kicked_off()
        de_s = model.edges["DE_S"]
        assert await dispatcher._block_manager.request_entry("TRN-B", [de_s])
        train_controller.handle_command.reset_mock()

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "3", time.time()))

        assert dispatcher._emergency is True
        stopped = {
            call.args[0]
            for call in train_controller.handle_command.await_args_list
            if call.args[1] == 0
        }
        assert stopped == {TRN_A_HUB, TRN_B_HUB}

    async def test_skipping_two_sensors_is_not_tolerated(self):
        dispatcher, model, *_ = await self._kicked_off()

        # Sensor 5 (B-K) is two sensors beyond what TRN-A last confirmed.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "5", time.time()))

        assert dispatcher._emergency is True


class TestSwitchPreset:
    def _model(self) -> TrackModel:
        model = TrackModel()
        model.configure_switch_wiring("D", hub_id=1, port_name="SWITCH_A")
        model.configure_switch_wiring("E", hub_id=2, port_name="SWITCH_A")
        model.register_train("TRN-A", hub_id=TRN_A_HUB, route=["B", "D", "E"])
        model.register_train("TRN-B", hub_id=TRN_B_HUB, route=["B", "D", "E"])
        model.set_self_drive("TRN-A", True)
        model.set_self_drive("TRN-B", False)
        return model

    @staticmethod
    def _commanded_hubs(switch_controller) -> set:
        return {
            call.args[0]
            for call in switch_controller.send_command_with_retry.await_args_list
        }

    async def _kick_off(self, model):
        built = build_dispatcher(model=model)
        dispatcher, _, _, train_controller, switch_controller = built
        # First read starts TRN-A on B->D (sensor 1).
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", time.time()))
        return dispatcher, train_controller, switch_controller

    async def test_next_chains_switches_are_set_when_a_chain_is_granted(self):
        _, _, switch_controller = await self._kick_off(self._model())

        # B->D needs switch D; the following D->E chain needs switch E, which
        # is set now instead of waiting for sensor 1 to be read.
        assert self._commanded_hubs(switch_controller) == {1, 2}

    async def test_switches_used_by_another_trains_granted_chain_are_left_alone(
        self,
    ):
        model = self._model()
        model.grant_pending_chain("TRN-B", [model.edges["DE_S"]])

        _, _, switch_controller = await self._kick_off(model)

        assert 2 not in self._commanded_hubs(switch_controller)

    async def test_switch_another_train_is_positioned_at_is_left_alone(self):
        model = self._model()
        model.train_position["TRN-B"] = "E"

        _, _, switch_controller = await self._kick_off(model)

        assert 2 not in self._commanded_hubs(switch_controller)

    async def test_preset_failure_does_not_stop_the_train(self):
        model = self._model()
        built = build_dispatcher(model=model)
        dispatcher, _, _, train_controller, switch_controller = built

        async def fail_for_switch_e(hub_id, port_name, position):
            return hub_id != 2

        switch_controller.send_command_with_retry.side_effect = fail_for_switch_e

        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", time.time()))

        assert model.is_moving("TRN-A")
        assert all(
            call.args[1] != 0
            for call in train_controller.handle_command.await_args_list
        )


class TestDeviceClockIsNotTrusted:
    async def test_tag_with_a_wildly_wrong_pico_timestamp_does_not_trip_the_watchdog(
        self,
    ):
        dispatcher, model, *_ = build_dispatcher()
        model.set_self_drive("TRN-A", True)
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", time.time()))
        assert model.is_moving("TRN-A")

        # An unsynced Pico stamps events near the 2021 epoch.
        await dispatcher._handle_tag_event(TagEvent("TRN-A", "1", 1609459660.0))

        assert model.seconds_since_last_tag("TRN-A") < 5.0


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
