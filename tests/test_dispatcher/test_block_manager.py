"""Tests for dispatcher.block_manager."""

from unittest.mock import AsyncMock

from dispatcher.block_manager import BlockManager
from dispatcher.track_model import (
    BlockState,
    SwitchDescriptor,
    SwitchPosition,
    SwitchRequirement,
    TagNode,
    TrackEdge,
    TrackModel,
    TrainDescriptor,
)


def build_model() -> TrackModel:
    tags = [TagNode("A"), TagNode("B"), TagNode("C")]
    switches = [SwitchDescriptor("SW1", hub_id=1, switch_name="SWITCH_A")]
    edges = [
        TrackEdge(
            "E_AB",
            "A",
            "B",
            switch_requirements=[SwitchRequirement("SW1", SwitchPosition.STRAIGHT)],
        ),
        TrackEdge("E_BC", "B", "C"),
    ]
    trains = [
        TrainDescriptor("T1", hub_id=1, route=["A", "B", "C"]),
        TrainDescriptor("T2", hub_id=2, route=["A", "B", "C"]),
    ]
    return TrackModel(tags=tags, edges=edges, switches=switches, trains=trains)


class TestRequestEntry:
    async def test_grants_free_block(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        assert await bm.request_entry("T1", edge) is True
        assert model.get_block_state("E_BC") == BlockState.OCCUPIED

    async def test_denies_occupied_block_and_queues(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        await bm.request_entry("T1", edge)
        assert await bm.request_entry("T2", edge) is False

    async def test_same_train_re_requesting_held_block_returns_true(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        await bm.request_entry("T1", edge)
        assert await bm.request_entry("T1", edge) is True


class TestResolveContention:
    def test_closer_train_wins_farther_train_held(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]  # from_tag == "B"

        model.record_tag_event("T1", "A", timestamp=1.0)  # 1 hop to B
        model.record_tag_event("T2", "C", timestamp=1.0)  # 2 hops to B (C->A->B)

        assert bm.resolve_contention("T1", "T2", edge) == "T2"

    def test_equal_hops_break_lexically(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        model.record_tag_event("T1", "A", timestamp=1.0)
        model.record_tag_event("T2", "A", timestamp=1.0)

        assert bm.resolve_contention("T1", "T2", edge) == "T2"


class TestReleaseAndRequeue:
    async def test_release_frees_the_block(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        await bm.request_entry("T1", edge)
        await bm.release("T1", edge)

        assert model.get_block_state("E_BC") == BlockState.FREE

    async def test_release_returns_the_only_queued_train(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        await bm.request_entry("T1", edge)
        await bm.request_entry("T2", edge)

        assert await bm.release("T1", edge) == "T2"

    async def test_release_with_no_queue_returns_none(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]

        await bm.request_entry("T1", edge)
        assert await bm.release("T1", edge) is None

    async def test_release_picks_closer_train_among_multiple_queued(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_BC"]  # from_tag == "B"
        model.trains["T3"] = TrainDescriptor("T3", hub_id=3, route=["A", "B", "C"])

        model.record_tag_event("T1", "C", timestamp=1.0)  # holder position irrelevant
        model.record_tag_event("T2", "A", timestamp=1.0)  # 1 hop to B -> closer
        model.record_tag_event("T3", "C", timestamp=1.0)  # 2 hops to B -> farther

        await bm.request_entry("T1", edge)  # holder
        await bm.request_entry("T2", edge)  # queued
        await bm.request_entry("T3", edge)  # queued

        assert await bm.release("T1", edge) == "T2"


class TestSetSwitchesForEdge:
    async def test_all_switches_succeed(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_AB"]
        switch_controller = AsyncMock()
        switch_controller.send_command_with_retry = AsyncMock(return_value=True)

        result = await bm.set_switches_for_edge(edge, switch_controller)

        assert result is True
        switch_controller.send_command_with_retry.assert_awaited_once_with(
            1, "SWITCH_A", int(SwitchPosition.STRAIGHT)
        )
        assert model.switch_positions["SW1"] == SwitchPosition.STRAIGHT

    async def test_switch_failure_denies_edge(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["E_AB"]
        switch_controller = AsyncMock()
        switch_controller.send_command_with_retry = AsyncMock(return_value=False)

        result = await bm.set_switches_for_edge(edge, switch_controller)

        assert result is False
        assert "SW1" not in model.switch_positions

    async def test_unknown_switch_id_denies_edge(self):
        model = build_model()
        bm = BlockManager(model)
        bad_edge = TrackEdge(
            "E_bad",
            "A",
            "B",
            switch_requirements=[
                SwitchRequirement("SW_UNKNOWN", SwitchPosition.STRAIGHT)
            ],
        )
        switch_controller = AsyncMock()

        result = await bm.set_switches_for_edge(bad_edge, switch_controller)

        assert result is False
        switch_controller.send_command_with_retry.assert_not_awaited()
