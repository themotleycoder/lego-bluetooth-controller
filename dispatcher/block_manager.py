"""
Block protection, route/switch locking, and contention resolution for the
RFID dispatcher.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from controllers.switch_controller import SwitchController
from dispatcher.track_model import BlockState, TrackEdge, TrackModel
from utils.logging_config import get_logger

logger = get_logger(__name__)


class BlockManager:
    """
    Enforces block protection and switch/route locking across the track.

    A block (edge) is either FREE or held by exactly one train. A train that
    requests an already-held block is queued (FIFO order of arrival) and
    surfaced again via `release()` once the block frees. When more than one
    train is queued for the same block, `resolve_contention` breaks the tie.
    """

    def __init__(self, track_model: TrackModel) -> None:
        """Initialize with no blocks held and no trains queued."""
        self._track_model = track_model
        self._reserved_by: Dict[str, str] = {}
        self._pending: Dict[str, List[str]] = {}

    async def request_entry(self, train_id: str, edge: TrackEdge) -> bool:
        """
        Request permission for `train_id` to enter `edge`.

        Returns True if the block is granted immediately (it was free), or
        if `train_id` already holds it. Otherwise the train is queued and
        this returns False.
        """
        if self._reserved_by.get(edge.edge_id) == train_id:
            return True

        if self._track_model.get_block_state(edge.edge_id) == BlockState.FREE:
            self._grant(edge.edge_id, train_id)
            return True

        self._queue(edge.edge_id, train_id)
        return False

    def _grant(self, edge_id: str, train_id: str) -> None:
        self._reserved_by[edge_id] = train_id
        self._track_model.set_block_state(edge_id, BlockState.OCCUPIED)
        pending = self._pending.get(edge_id)
        if pending and train_id in pending:
            pending.remove(train_id)

    def _queue(self, edge_id: str, train_id: str) -> None:
        pending = self._pending.setdefault(edge_id, [])
        if train_id not in pending:
            pending.append(train_id)

    def resolve_contention(self, train_a: str, train_b: str, edge: TrackEdge) -> str:
        """
        Decide which of two trains contending for the same block should be
        HELD (wait), given tag-granularity positioning.

        "Farther away" is defined as the number of route-hops remaining from
        each train's current position to the contested edge's start tag. The
        train with the larger hop count is farther away and is held; the
        closer train proceeds. Equal hop counts break by lexical train_id
        order, a deterministic tiebreak that avoids livelock.
        """
        hops_a = self._hops_to_tag(train_a, edge.from_tag)
        hops_b = self._hops_to_tag(train_b, edge.from_tag)

        if hops_a == hops_b:
            held = max(train_a, train_b)
        else:
            held = train_a if hops_a > hops_b else train_b

        logger.info(
            f"Block contention on {edge.edge_id}: {train_a} ({hops_a} hops) vs "
            f"{train_b} ({hops_b} hops) -> holding {held}"
        )
        return held

    def _hops_to_tag(self, train_id: str, target_tag: str) -> int:
        """Count route-hops from a train's current position to target_tag."""
        train = self._track_model.trains.get(train_id)
        if train is None or not train.route:
            return 0

        current_tag = self._track_model.train_position.get(train_id, train.route[0])
        if current_tag not in train.route or target_tag not in train.route:
            return len(train.route)

        start_idx = train.route.index(current_tag)
        target_idx = train.route.index(target_tag)
        return (target_idx - start_idx) % len(train.route)

    def _select_next_pending(self, pending: List[str], edge: TrackEdge) -> str:
        """Pick which queued train is granted the block next (pairwise reduction
        over `resolve_contention` so N-way queues resolve consistently)."""
        winner = pending[0]
        for candidate in pending[1:]:
            held = self.resolve_contention(winner, candidate, edge)
            if held == winner:
                winner = candidate
        return winner

    async def release(self, train_id: str, edge: TrackEdge) -> Optional[str]:
        """
        Release a block previously held by `train_id`.

        Returns the next queued train_id (if any) that should retry entry
        now that the block is free.
        """
        if self._reserved_by.get(edge.edge_id) == train_id:
            del self._reserved_by[edge.edge_id]
        self._track_model.set_block_state(edge.edge_id, BlockState.FREE)

        pending = self._pending.get(edge.edge_id)
        if not pending:
            return None

        winner = self._select_next_pending(pending, edge)
        pending.remove(winner)
        return winner

    async def set_switches_for_edge(
        self, edge: TrackEdge, switch_controller: SwitchController
    ) -> bool:
        """
        Set every switch an edge requires, all-or-nothing.

        A partial switch failure is a derailment risk, so a single failed
        switch denies the whole edge rather than leaving a mixed state.
        """
        for requirement in edge.switch_requirements:
            descriptor = self._track_model.switches.get(requirement.switch_id)
            if descriptor is None:
                logger.error(
                    f"Unknown switch_id {requirement.switch_id} on edge {edge.edge_id}"
                )
                return False

            success = await switch_controller.send_command_with_retry(
                descriptor.hub_id,
                descriptor.switch_name,
                int(requirement.required_position),
            )
            if not success:
                logger.error(
                    f"Failed to set switch {descriptor.switch_id} to "
                    f"{requirement.required_position.name} for edge {edge.edge_id}"
                )
                return False

            self._track_model.switch_positions[
                requirement.switch_id
            ] = requirement.required_position

        return True
