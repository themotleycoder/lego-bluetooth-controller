"""Tests for dispatcher.block_manager."""

from unittest.mock import AsyncMock

from dispatcher.block_manager import BlockManager
from dispatcher.track_model import TrackModel


def build_model() -> TrackModel:
    """
    Real track topology with two trains registered:
      T1: D -> E -> I -> F -> G -> H  (starts at the DE_S block)
      T2: B -> D -> E -> I            (starts one hop from D, via BD)
    Both routes cross the shared DE_S block, giving a real bottleneck to
    contend over. Switch "F" (used by the CF edge) is wired for BLE.
    """
    model = TrackModel()
    model.configure_switch_wiring("F", hub_id=1, port_name="SWITCH_A")
    model.register_train("T1", hub_id=1, route=["D", "E", "I", "F", "G", "H"])
    model.register_train("T2", hub_id=2, route=["B", "D", "E", "I"])
    return model


class TestRequestEntry:
    async def test_grants_free_chain(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        assert await bm.request_entry("T1", chain) is True
        assert model.is_block_free("BLK_DE_S") is False

    async def test_denies_occupied_block_and_queues(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        assert await bm.request_entry("T2", chain) is False

    async def test_same_train_re_requesting_held_chain_returns_true(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        assert await bm.request_entry("T1", chain) is True

    async def test_empty_chain_is_always_granted(self):
        model = build_model()
        bm = BlockManager(model)
        assert await bm.request_entry("T1", []) is True

    async def test_multi_edge_chain_is_all_or_nothing(self):
        model = build_model()
        bm = BlockManager(model)
        # T2 already holds DE_S; a chain that includes it must be denied whole.
        await bm.request_entry("T2", [model.edges["DE_S"]])

        chain = [model.edges["BD"], model.edges["DE_S"]]
        assert await bm.request_entry("T1", chain) is False
        # BD must not have been partially granted.
        assert model.is_block_free("BLK_BD") is True


class TestResolveContention:
    def test_closer_train_wins_farther_train_held(self):
        model = build_model()
        bm = BlockManager(model)
        edge = model.edges["DE_S"]  # from_switch == "D"

        # T1 starts at D (0 hops); T2 starts at B, one hop from D via BD.
        assert bm.resolve_contention("T1", "T2", edge) == "T2"

    def test_equal_hops_break_lexically(self):
        model = build_model()
        model.register_train("T3", hub_id=3, route=["D", "E", "I"])
        bm = BlockManager(model)
        edge = model.edges["DE_S"]  # both T1 and T3 start at D -> 0 hops each

        assert bm.resolve_contention("T1", "T3", edge) == "T3"


class TestReleaseAndRequeue:
    async def test_release_frees_the_blocks(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        await bm.release("T1", chain)

        assert model.is_block_free("BLK_DE_S") is True

    async def test_release_returns_the_only_queued_train(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        await bm.request_entry("T2", chain)

        assert await bm.release("T1", chain) == ["T2"]

    async def test_release_with_no_queue_returns_empty(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        assert await bm.release("T1", chain) == []

    async def test_release_picks_closer_train_among_multiple_queued(self):
        model = build_model()
        model.register_train("T3", hub_id=3, route=["K", "B", "D", "E"])  # 2 hops to D
        bm = BlockManager(model)
        chain = [model.edges["DE_S"]]  # from_switch == "D"

        await bm.request_entry("T1", chain)  # holder (irrelevant once holding)
        await bm.request_entry("T2", chain)  # queued, 1 hop from D -> closer
        await bm.request_entry("T3", chain)  # queued, 2 hops from D -> farther

        assert await bm.release("T1", chain) == ["T2"]

    async def test_release_multi_edge_chain_returns_all_retries(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["BD"], model.edges["DE_S"]]

        await bm.request_entry("T1", chain)
        await bm.request_entry("T2", [model.edges["DE_S"]])  # queued on DE_S only

        retries = await bm.release("T1", chain)
        assert retries == ["T2"]
        assert model.is_block_free("BLK_BD") is True
        assert model.is_block_free("BLK_DE_S") is True


class TestSetSwitchesForChain:
    async def test_all_switches_succeed(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["CF"]]  # requires switch F -> DIVERGE
        switch_controller = AsyncMock()
        switch_controller.send_command_with_retry = AsyncMock(return_value=True)

        result = await bm.set_switches_for_chain(chain, switch_controller)

        assert result is True
        switch_controller.send_command_with_retry.assert_awaited_once_with(
            1, "SWITCH_A", 1
        )

    async def test_switch_failure_denies_chain(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["CF"]]
        switch_controller = AsyncMock()
        switch_controller.send_command_with_retry = AsyncMock(return_value=False)

        result = await bm.set_switches_for_chain(chain, switch_controller)

        assert result is False

    async def test_unwired_motorized_switch_denies_chain(self):
        model = TrackModel()  # switch "F" never wired
        model.register_train("T1", hub_id=1, route=["C", "F", "G"])
        bm = BlockManager(model)
        chain = [model.edges["CF"]]
        switch_controller = AsyncMock()

        result = await bm.set_switches_for_chain(chain, switch_controller)

        assert result is False
        switch_controller.send_command_with_retry.assert_not_awaited()

    async def test_manual_switches_are_not_actuated(self):
        model = build_model()
        bm = BlockManager(model)
        chain = [model.edges["AC"]]  # A and C are both manual switches
        switch_controller = AsyncMock()

        result = await bm.set_switches_for_chain(chain, switch_controller)

        assert result is True
        switch_controller.send_command_with_retry.assert_not_awaited()
