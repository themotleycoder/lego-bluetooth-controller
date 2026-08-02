"""
Track topology and live position/occupancy state for the RFID dispatcher.

Models the track as a directed graph: nodes are RFID tag positions, edges are
track segments between two tags. Positioning is tag-granularity only (no
continuous odometry), so each train follows a fixed, pre-assigned cyclic route
rather than being dynamically routed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Dict, List, Optional

from utils.logging_config import get_logger

logger = get_logger(__name__)


class SwitchPosition(IntEnum):
    """Physical switch position, matching SwitchController's numeric convention."""

    STRAIGHT = 0
    DIVERGING = 1


class BlockState(Enum):
    """Occupancy state of a track block (edge)."""

    FREE = "free"
    OCCUPIED = "occupied"


@dataclass(frozen=True)
class TagNode:
    """
    An RFID tag position on the track.

    `tag_id` is the logical name used throughout the topology (routes,
    edges). `uid` is the physical RFID tag's scanned UID hex string; when
    omitted, `tag_id` itself is treated as the UID (handy for the sample
    topology and tests, where "T1" can be published directly as the UID).
    """

    tag_id: str
    uid: Optional[str] = None
    label: Optional[str] = None


@dataclass(frozen=True)
class SwitchRequirement:
    """A switch position an edge requires in order to be traversable."""

    switch_id: str
    required_position: SwitchPosition


@dataclass(frozen=True)
class TrackEdge:
    """
    A directed track segment between two RFID tags.

    Directed because a facing switch may require a different position
    depending on the direction of travel; a bidirectional segment is
    represented as two TrackEdge instances.
    """

    edge_id: str
    from_tag: str
    to_tag: str
    length_m: Optional[float] = None
    switch_requirements: List[SwitchRequirement] = field(default_factory=list)


@dataclass(frozen=True)
class SwitchDescriptor:
    """Maps an abstract topology switch to its physical hub/port."""

    switch_id: str
    hub_id: int
    switch_name: str  # e.g. "SWITCH_A".."SWITCH_D"


@dataclass
class TrainDescriptor:
    """A train's identity and its fixed, pre-assigned cyclic route."""

    train_id: str
    hub_id: int
    route: List[str]  # cyclic list of tag_ids


@dataclass
class TagEventResult:
    """Outcome of recording a single RFID tag read."""

    train_id: str
    previous_tag: Optional[str]
    current_tag: str
    edge_completed: Optional[TrackEdge]


class TrackModel:
    """Directed-graph model of the track plus live train/switch/block state."""

    def __init__(
        self,
        tags: List[TagNode],
        edges: List[TrackEdge],
        switches: List[SwitchDescriptor],
        trains: List[TrainDescriptor],
    ) -> None:
        """Build the graph and initialize all blocks to FREE."""
        self.tags: Dict[str, TagNode] = {tag.tag_id: tag for tag in tags}
        self.edges: Dict[str, TrackEdge] = {edge.edge_id: edge for edge in edges}
        self.switches: Dict[str, SwitchDescriptor] = {
            switch.switch_id: switch for switch in switches
        }
        self.trains: Dict[str, TrainDescriptor] = {
            train.train_id: train for train in trains
        }

        self._edges_from: Dict[str, List[TrackEdge]] = {}
        for edge in edges:
            self._edges_from.setdefault(edge.from_tag, []).append(edge)

        self._tag_id_by_uid: Dict[str, str] = {
            (tag.uid or tag.tag_id): tag.tag_id for tag in tags
        }

        self.train_position: Dict[str, str] = {}
        self.train_last_update: Dict[str, float] = {}
        self.train_stopped: Dict[str, bool] = {}
        self.block_state: Dict[str, BlockState] = {
            edge.edge_id: BlockState.FREE for edge in edges
        }
        self.switch_positions: Dict[str, SwitchPosition] = {}

    def edges_from(self, tag_id: str) -> List[TrackEdge]:
        """Return all edges departing the given tag."""
        return list(self._edges_from.get(tag_id, []))

    def tag_id_for_uid(self, uid: str) -> Optional[str]:
        """Resolve a physical RFID tag UID to its logical tag_id, if known."""
        return self._tag_id_by_uid.get(uid)

    def record_tag_event(
        self, train_id: str, tag_id: str, timestamp: float
    ) -> TagEventResult:
        """
        Update a train's position from a tag read.

        Unknown train/tag IDs are logged and ignored rather than raised, since
        a misread tag must not crash the dispatcher.
        """
        if train_id not in self.trains:
            logger.warning(f"Tag event from unregistered train: {train_id}")
            return TagEventResult(train_id, None, tag_id, None)
        if tag_id not in self.tags:
            logger.warning(f"Unknown tag_id {tag_id} reported by train {train_id}")
            return TagEventResult(
                train_id, self.train_position.get(train_id), tag_id, None
            )

        previous_tag = self.train_position.get(train_id)
        edge_completed: Optional[TrackEdge] = None
        if previous_tag is not None:
            for edge in self.edges_from(previous_tag):
                if edge.to_tag == tag_id:
                    edge_completed = edge
                    break

        self.train_position[train_id] = tag_id
        self.train_last_update[train_id] = timestamp

        return TagEventResult(train_id, previous_tag, tag_id, edge_completed)

    def next_edge_for_train(self, train_id: str) -> Optional[TrackEdge]:
        """Return the edge a train should take next along its fixed route."""
        train = self.trains.get(train_id)
        if train is None or not train.route:
            return None

        current_tag = self.train_position.get(train_id, train.route[0])
        if current_tag not in train.route:
            logger.warning(
                f"Train {train_id} at tag {current_tag} is off its configured route"
            )
            return None

        idx = train.route.index(current_tag)
        next_tag = train.route[(idx + 1) % len(train.route)]

        for edge in self.edges_from(current_tag):
            if edge.to_tag == next_tag:
                return edge

        logger.warning(
            f"No edge found from {current_tag} to {next_tag} for train {train_id}"
        )
        return None

    def get_block_state(self, edge_id: str) -> BlockState:
        """Return the current occupancy state of a block (edge)."""
        return self.block_state.get(edge_id, BlockState.FREE)

    def set_block_state(self, edge_id: str, state: BlockState) -> None:
        """Set the occupancy state of a block (edge)."""
        self.block_state[edge_id] = state

    def mark_stopped(self, train_id: str, stopped: bool) -> None:
        """Mark whether a train is intentionally stopped (vs. moving)."""
        self.train_stopped[train_id] = stopped

    def is_moving(self, train_id: str) -> bool:
        """Whether a train should currently be advancing (used by the watchdog)."""
        return train_id in self.train_position and not self.train_stopped.get(
            train_id, True
        )

    def seconds_since_last_tag(
        self, train_id: str, now: Optional[float] = None
    ) -> float:
        """Seconds elapsed since the train's last recorded tag read."""
        now = now if now is not None else time.time()
        last = self.train_last_update.get(train_id)
        if last is None:
            return 0.0
        return now - last


def build_sample_topology() -> TrackModel:
    """
    Build the sample topology matching the reference layout: 14 tags, 7
    switches, an inner loop and an outer loop sharing one junction.

    Outer loop: T1..T7 (7 tags). Inner loop: T8..T14 (7 tags). Switches SW1
    (hub 1, SWITCH_A) and SW5 (hub 2, SWITCH_A) sit at the shared junction:
    STRAIGHT keeps a train on its own loop, DIVERGING crosses it to the other
    loop via the T1<->T8 crossover edges. Two Technic Hubs are used for
    switches since each hub only exposes ports A-D (4 switches per hub).
    """
    tags = [TagNode(f"T{i}") for i in range(1, 15)]

    switches = [
        SwitchDescriptor("SW1", hub_id=1, switch_name="SWITCH_A"),
        SwitchDescriptor("SW2", hub_id=1, switch_name="SWITCH_B"),
        SwitchDescriptor("SW3", hub_id=1, switch_name="SWITCH_C"),
        SwitchDescriptor("SW4", hub_id=1, switch_name="SWITCH_D"),
        SwitchDescriptor("SW5", hub_id=2, switch_name="SWITCH_A"),
        SwitchDescriptor("SW6", hub_id=2, switch_name="SWITCH_B"),
        SwitchDescriptor("SW7", hub_id=2, switch_name="SWITCH_C"),
    ]

    def req(switch_id: str, position: SwitchPosition) -> List[SwitchRequirement]:
        return [SwitchRequirement(switch_id, position)]

    outer_tags = [f"T{i}" for i in range(1, 8)]
    inner_tags = [f"T{i}" for i in range(8, 15)]

    edges: List[TrackEdge] = []

    # Outer loop, cyclic T1 -> T2 -> ... -> T7 -> T1.
    outer_switch_map = {"T1": "SW1", "T3": "SW2", "T5": "SW3", "T7": "SW4"}
    for i, from_tag in enumerate(outer_tags):
        to_tag = outer_tags[(i + 1) % len(outer_tags)]
        switch_id = outer_switch_map.get(from_tag)
        edges.append(
            TrackEdge(
                edge_id=f"E_{from_tag}_{to_tag}",
                from_tag=from_tag,
                to_tag=to_tag,
                switch_requirements=(
                    req(switch_id, SwitchPosition.STRAIGHT) if switch_id else []
                ),
            )
        )

    # Inner loop, cyclic T8 -> T9 -> ... -> T14 -> T8.
    inner_switch_map = {"T8": "SW5", "T10": "SW6", "T12": "SW7"}
    for i, from_tag in enumerate(inner_tags):
        to_tag = inner_tags[(i + 1) % len(inner_tags)]
        switch_id = inner_switch_map.get(from_tag)
        edges.append(
            TrackEdge(
                edge_id=f"E_{from_tag}_{to_tag}",
                from_tag=from_tag,
                to_tag=to_tag,
                switch_requirements=(
                    req(switch_id, SwitchPosition.STRAIGHT) if switch_id else []
                ),
            )
        )

    # Shared junction crossover between the two loops.
    edges.append(
        TrackEdge(
            edge_id="E_T1_T8_crossover",
            from_tag="T1",
            to_tag="T8",
            switch_requirements=req("SW1", SwitchPosition.DIVERGING),
        )
    )
    edges.append(
        TrackEdge(
            edge_id="E_T8_T1_crossover",
            from_tag="T8",
            to_tag="T1",
            switch_requirements=req("SW5", SwitchPosition.DIVERGING),
        )
    )

    trains = [
        TrainDescriptor(train_id="TRN-A", hub_id=12, route=inner_tags),
        TrainDescriptor(train_id="TRN-B", hub_id=22, route=outer_tags),
    ]

    return TrackModel(tags=tags, edges=edges, switches=switches, trains=trains)
