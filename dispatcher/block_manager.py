"""
Block protection, route/switch locking, and contention resolution for the
RFID dispatcher.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from controllers.switch_controller import SwitchController
from dispatcher.track_model import Edge, SwitchType, TrackModel
from utils.logging_config import get_logger

logger = get_logger(__name__)


class BlockManager:
    """
    Enforces block protection and switch/route locking across the track.

    A block is either FREE or held by exactly one train. Blocks are granted
    and released in chains (see TrackModel.next_block_chain_for_train) since
    most blocks carry no RFID sensor of their own and can't be confirmed
    individually -- a chain is all-or-nothing, just like switch-setting. A
    train that requests an already-held block in a chain is queued (FIFO
    order of arrival) and surfaced again via `release()` once the block
    frees. When more than one train is queued for the same block,
    `resolve_contention` breaks the tie.
    """

    def __init__(self, track_model: TrackModel) -> None:
        """Initialize with no blocks held and no trains queued."""
        self._track_model = track_model
        self._reserved_by: Dict[str, str] = {}
        self._pending: Dict[str, List[str]] = {}

    async def request_entry(self, train_id: str, chain: List[Edge]) -> bool:
        """
        Request permission for `train_id` to enter every block in `chain`.

        Returns True only if every block in the chain is free (or already
        held by `train_id`), granting them all together. If any block in the
        chain is held by another train, `train_id` is queued on that block
        and this returns False -- no partial grants.
        """
        if not chain:
            return True

        for edge in chain:
            if not edge.block:
                continue
            owner = self._reserved_by.get(edge.block)
            if owner is not None and owner != train_id:
                self._queue(edge.block, train_id)
                return False

        for edge in chain:
            if edge.block:
                self._grant(edge.block, train_id)
        return True

    def _grant(self, block_id: str, train_id: str) -> None:
        self._reserved_by[block_id] = train_id
        self._track_model.occupy_block(block_id, train_id)
        pending = self._pending.get(block_id)
        if pending and train_id in pending:
            pending.remove(train_id)

    def _queue(self, block_id: str, train_id: str) -> None:
        pending = self._pending.setdefault(block_id, [])
        if train_id not in pending:
            pending.append(train_id)

    def resolve_contention(self, train_a: str, train_b: str, edge: Edge) -> str:
        """
        Decide which of two trains contending for the same block should be
        HELD (wait), given switch-granularity positioning.

        "Farther away" is defined as the number of route-hops remaining from
        each train's current position to the contested edge's start switch.
        The train with the larger hop count is farther away and is held; the
        closer train proceeds. Equal hop counts break by lexical train_id
        order, a deterministic tiebreak that avoids livelock.
        """
        hops_a = self._track_model.hops_to_switch(train_a, edge.from_switch)
        hops_b = self._track_model.hops_to_switch(train_b, edge.from_switch)

        if hops_a == hops_b:
            held = max(train_a, train_b)
        else:
            held = train_a if hops_a > hops_b else train_b

        logger.info(
            f"Block contention on {edge.block}: {train_a} ({hops_a} hops) vs "
            f"{train_b} ({hops_b} hops) -> holding {held}"
        )
        return held

    def _select_next_pending(self, pending: List[str], edge: Edge) -> str:
        """Pick which queued train is granted the block next (pairwise reduction
        over `resolve_contention` so N-way queues resolve consistently)."""
        winner = pending[0]
        for candidate in pending[1:]:
            held = self.resolve_contention(winner, candidate, edge)
            if held == winner:
                winner = candidate
        return winner

    def _release_block(
        self, block_id: str, train_id: str, edge: Optional[Edge] = None
    ) -> Optional[str]:
        """
        Free `block_id` if held by `train_id`, returning the next queued
        train_id to retry entry, if any (removed from the pending queue).

        `edge` is used for contention resolution among queued trains; if not
        given (e.g. releasing by block_id alone), an arbitrary edge sharing
        the block is used instead -- any edge on a block is an equally valid
        reference point for "how far away is this train from this block".
        """
        if self._reserved_by.get(block_id) == train_id:
            del self._reserved_by[block_id]
        self._track_model.free_block(block_id)

        pending = self._pending.get(block_id)
        if not pending:
            return None

        if edge is None:
            block = self._track_model.blocks.get(block_id)
            if not block or not block.edge_ids:
                return None
            edge = self._track_model.edges[block.edge_ids[0]]

        winner = self._select_next_pending(pending, edge)
        pending.remove(winner)
        return winner

    async def release(self, train_id: str, chain: List[Edge]) -> List[str]:
        """
        Release every block in `chain` previously held by `train_id`.

        Returns the (deduplicated) list of queued train_ids, if any, that
        should retry entry now that their blocks are free.
        """
        retries: List[str] = []
        for edge in chain:
            if not edge.block:
                continue
            winner = self._release_block(edge.block, train_id, edge)
            if winner and winner not in retries:
                retries.append(winner)
        return retries

    async def release_all(self, train_id: str) -> List[str]:
        """
        Release every block currently held by `train_id`, regardless of
        which chain granted it -- used when a train is pulled off self-drive
        control and may be holding blocks from several past chain-grants.

        Returns the (deduplicated) list of queued train_ids, if any, that
        should retry entry now that their blocks are free.
        """
        retries: List[str] = []
        held_blocks = [
            block_id
            for block_id, owner in self._reserved_by.items()
            if owner == train_id
        ]
        for block_id in held_blocks:
            winner = self._release_block(block_id, train_id)
            if winner and winner not in retries:
                retries.append(winner)
        return retries

    async def set_switches_for_chain(
        self, chain: List[Edge], switch_controller: SwitchController
    ) -> bool:
        """
        Set every switch a chain of edges requires, all-or-nothing.

        A partial switch failure is a derailment risk, so a single failed
        switch denies the whole chain rather than leaving a mixed state.
        Manual switches are skipped (the dispatcher can't actuate them) but
        still logged, so an operator can verify they're hand-set correctly.
        """
        for switch_id, diverge in self._track_model.route_switch_settings(chain):
            switch = self._track_model.switches.get(switch_id)
            if switch is None:
                logger.error(f"Unknown switch_id {switch_id} in requested chain")
                return False

            if switch.switch_type == SwitchType.MANUAL:
                logger.info(
                    f"Manual switch {switch_id} must be hand-set to "
                    f"{'DIVERGE' if diverge else 'STRAIGHT'}"
                )
                continue

            if switch.hub_id is None or switch.port_name is None:
                logger.error(f"Switch {switch_id} has no configured BLE wiring")
                return False

            success = await switch_controller.send_command_with_retry(
                switch.hub_id, switch.port_name, int(diverge)
            )
            if not success:
                logger.error(
                    f"Failed to set switch {switch_id} to "
                    f"{'DIVERGE' if diverge else 'STRAIGHT'}"
                )
                return False

        return True
