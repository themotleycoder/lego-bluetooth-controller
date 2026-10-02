"""
Track topology model for the LEGO train dispatcher.

Directed graph of the physical track layout. Switches are nodes,
track segments are edges. RFID sensors sit on edges for position detection.

Usage:
    from track_model import TrackModel
    model = TrackModel()
    route = model.find_route("SW_A", "SW_K")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SwitchPort(Enum):
    """Physical ports on a LEGO switch piece."""

    TRUNK = "trunk"
    STRAIGHT = "straight"
    DIVERGE = "diverge"


class SwitchType(Enum):
    MOTORIZED = "motorized"  # dispatcher-controlled via BLE
    MANUAL = "manual"  # hand-set, fixed position


class Direction(Enum):
    """Which way a train is moving through a bidirectional edge."""

    FORWARD = "forward"
    REVERSE = "reverse"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Switch:
    id: str
    switch_type: SwitchType
    # Current state: True = diverge, False = straight (trunk always connected)
    # For manual switches the dispatcher reads but doesn't write this.

    # BLE wiring, populated at runtime from config (see TrackModel.configure_switch_wiring) --
    # unset for manual switches, which the dispatcher never actuates.
    hub_id: Optional[int] = None
    port_name: Optional[
        str
    ] = None  # e.g. "SWITCH_A", matches SwitchController's convention

    def __repr__(self) -> str:
        return f"SW_{self.id}({self.switch_type.value})"


@dataclass(frozen=True)
class Sensor:
    """An RFID tag embedded under the track."""

    id: int
    tag_uid: Optional[str] = None  # populated at runtime from config
    description: str = ""

    def __repr__(self) -> str:
        return f"TAG_{self.id}"


@dataclass(frozen=True)
class Edge:
    """
    A directed track segment between two switch ports.

    Trains can traverse in both directions; the 'forward' direction
    is from_switch:from_port -> to_switch:to_port. A reverse edge
    is generated automatically by TrackModel.
    """

    id: str
    from_switch: str  # switch id
    from_port: SwitchPort
    to_switch: str  # switch id
    to_port: SwitchPort
    sensors: list[int] = field(default_factory=list)  # sensor ids in order
    block: str = ""  # block id for occupancy

    def __repr__(self) -> str:
        sensors_str = (
            f" [{', '.join(f'T{s}' for s in self.sensors)}]" if self.sensors else ""
        )
        return f"{self.from_switch}.{self.from_port.value} -> {self.to_switch}.{self.to_port.value}{sensors_str}"


@dataclass
class Block:
    """
    A track section protected by occupancy locking.
    Only one train may hold a block at a time.
    """

    id: str
    edge_ids: list[str]  # edges that share this block
    description: str = ""
    occupied_by: Optional[str] = None  # train_id or None


@dataclass
class Train:
    """
    A train's identity and how it decides where to go next.

    `route` is a fixed, pre-assigned cyclic list of switch ids -- used unless
    the train is in smart-drive mode, in which case it's None and the train
    instead picks its next edge dynamically (see TrackModel._choose_next_edge)
    to cover the whole layout over time instead of looping a fixed path.
    """

    id: str
    hub_id: str  # BLE address of the train hub, e.g. "90:84:2B:18:28:36"
    route: Optional[
        list[str]
    ] = None  # cyclic list of switch ids, or None if smart-drive


@dataclass
class TagEventResult:
    """Outcome of recording a single RFID sensor read."""

    train_id: str
    previous_position: Optional[str]
    current_position: Optional[str]
    edges_completed: list[Edge]
    # True only if the read confirmed one of the train's pending edges.
    matched: bool = False
    # True if the read is on the edge the train last confirmed: the reader can
    # report the same tag repeatedly, and an edge with two sensors (e.g. BK)
    # is confirmed by whichever is read first while the train is still on it.
    duplicate: bool = False


# ---------------------------------------------------------------------------
# Track model
# ---------------------------------------------------------------------------


class TrackModel:
    """
    Full graph of the LEGO train layout.

    Nodes = switches (10 total: 8 motorized, 2 manual)
    Edges = track segments between switch ports
    Sensors = 9 RFID tags on edges
    Blocks = 15 occupancy zones
    """

    def __init__(self) -> None:
        self.switches: dict[str, Switch] = {}
        self.sensors: dict[int, Sensor] = {}
        self.edges: dict[str, Edge] = {}
        self.blocks: dict[str, Block] = {}

        # adjacency: switch_id -> list of edge_ids leaving that switch
        self._adj: dict[str, list[str]] = {}

        # Runtime state: train registry + live position/movement tracking.
        # Unlike switches/sensors/edges/blocks (fixed topology), this is
        # populated at runtime via register_train() from config.
        self.trains: dict[str, Train] = {}
        self.train_position: dict[str, str] = {}
        self.train_battery: dict[str, float] = {}  # train_id -> last known VSYS volts
        self._train_route_index: dict[str, int] = {}
        self._train_stopped: dict[str, bool] = {}
        self._train_last_tag_time: dict[str, float] = {}
        self._train_last_confirmed_edge: dict[str, str] = {}
        self._train_self_drive: dict[str, bool] = {}
        # Edges granted to a train but not yet confirmed cleared by a tag read,
        # in route order. See TrackModel.next_block_chain_for_train.
        self._pending_edges: dict[str, list[Edge]] = {}
        # Smart-drive (dynamic coverage routing) state -- see register_train
        # and _choose_next_edge. Coverage is driven by least-recently-used
        # edge selection: _train_edge_tick records the tick each edge was
        # last confirmed at (missing = never visited, treated as oldest),
        # and _train_tick is each train's monotonic visit counter.
        self._train_smart_drive: dict[str, bool] = {}
        self._train_edge_tick: dict[str, dict[str, int]] = {}
        self._train_tick: dict[str, int] = {}
        self._train_last_edge: dict[str, Optional[str]] = {}
        self._train_start_switch: dict[str, Optional[str]] = {}

        self._build()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._build_switches()
        self._build_sensors()
        self._build_edges()
        self._build_blocks()
        self._build_adjacency()

    def _build_switches(self) -> None:
        # Switch ids and topology below come from the track designer export
        # (track-topology.json) -- note there's no switch "J": the ten ids
        # are A-I plus K. The export flagged A/B/C as manual. B had a motor
        # wired on (hub 5), but hub 5's BLE connection has been unreliable
        # (stale/garbled status notifications, commands failing verification)
        # since at least 2026-08-13, so B is reverted to manual for now --
        # hand-set it for TRN-A's route until hub 5 is fixed. A and C are
        # getting motors too but aren't wired yet, so they also stay manual.
        motorized = ["D", "E", "F", "G", "H", "I", "K"]
        manual = ["A", "B", "C"]
        for sid in motorized:
            self.switches[sid] = Switch(id=sid, switch_type=SwitchType.MOTORIZED)
        for sid in manual:
            self.switches[sid] = Switch(id=sid, switch_type=SwitchType.MANUAL)

    def _build_sensors(self) -> None:
        defs = [
            (1, "Upper right, B-D segment"),
            (2, "Upper left, B-K segment (near B)"),
            (3, "Right, D-E straight segment"),
            (4, "Upper middle, F-G segment"),
            (5, "Lower left, B-K segment (near K)"),
            (6, "Left, A-H segment"),
            (7, "Right diagonal, D-E crossover"),
            (8, "Upper middle, B-G crossover"),
            (9, "Left middle, C-H crossover"),
            (10, "Lower right, E-I segment"),
            (11, "Lower middle, C-F segment"),
            (12, "Lower middle, A-C segment"),
            (13, "Right, F-G segment (right end of inner oval)"),
            (14, "Far left, B-K segment (outer left curve)"),
        ]
        for sid, desc in defs:
            self.sensors[sid] = Sensor(id=sid, description=desc)

    def _build_edges(self) -> None:
        E = Edge
        P = SwitchPort
        edges = [
            E("AH", "A", P.TRUNK, "H", P.TRUNK, [6], "BLK_AH"),
            E("AK", "A", P.STRAIGHT, "K", P.STRAIGHT, [], "BLK_AK"),
            E("AC", "A", P.DIVERGE, "C", P.STRAIGHT, [12], "BLK_AC"),
            E("BD", "B", P.TRUNK, "D", P.STRAIGHT, [1], "BLK_BD"),
            E("BK", "B", P.STRAIGHT, "K", P.DIVERGE, [2, 14, 5], "BLK_BK"),
            E("BG", "B", P.DIVERGE, "G", P.DIVERGE, [8], "BLK_BG"),
            E("CF", "C", P.TRUNK, "F", P.DIVERGE, [11], "BLK_CF"),
            E("CH", "C", P.DIVERGE, "H", P.DIVERGE, [9], "BLK_CH"),
            E("DE_S", "D", P.TRUNK, "E", P.STRAIGHT, [3], "BLK_DE_S"),
            E("DE_D", "D", P.DIVERGE, "E", P.DIVERGE, [7], "BLK_DE_D"),
            E("EI", "E", P.TRUNK, "I", P.DIVERGE, [10], "BLK_EI"),
            E("FG", "F", P.TRUNK, "G", P.STRAIGHT, [13, 4], "BLK_FG"),
            E("FI", "F", P.STRAIGHT, "I", P.STRAIGHT, [], "BLK_FI"),
            E("GH", "G", P.TRUNK, "H", P.STRAIGHT, [], "BLK_GH"),
            E("IK", "I", P.TRUNK, "K", P.TRUNK, [], "BLK_IK"),
        ]
        for e in edges:
            self.edges[e.id] = e

    def _build_blocks(self) -> None:
        blk_defs = [
            ("BLK_AH", ["AH"], "A(trunk)-TAG_6-H(trunk)"),
            ("BLK_AK", ["AK"], "A(str)-K(str)"),
            ("BLK_AC", ["AC"], "A(div)-TAG_12-C(str)"),
            ("BLK_BD", ["BD"], "B(trunk)-TAG_1-D(str)"),
            ("BLK_BK", ["BK"], "B(str)-TAG_2-TAG_14-TAG_5-K(div)"),
            ("BLK_BG", ["BG"], "Crossover: B(div)-TAG_8-G(div)"),
            ("BLK_CF", ["CF"], "C(trunk)-TAG_11-F(div)"),
            ("BLK_CH", ["CH"], "Crossover: C(div)-TAG_9-H(div)"),
            ("BLK_DE_S", ["DE_S"], "D(trunk)-TAG_3-E(str)"),
            ("BLK_DE_D", ["DE_D"], "Crossover: D(div)-TAG_7-E(div)"),
            ("BLK_EI", ["EI"], "E(trunk)-TAG_10-I(div)"),
            ("BLK_FG", ["FG"], "F(trunk)-TAG_13-TAG_4-G(str)"),
            ("BLK_FI", ["FI"], "F(str)-I(str)"),
            ("BLK_GH", ["GH"], "G(trunk)-H(str)"),
            ("BLK_IK", ["IK"], "I(trunk)-K(trunk)"),
        ]
        for bid, eids, desc in blk_defs:
            self.blocks[bid] = Block(id=bid, edge_ids=eids, description=desc)

    def _build_adjacency(self) -> None:
        """Build forward + reverse adjacency from edges."""
        for sid in self.switches:
            self._adj[sid] = []
        for eid, edge in self.edges.items():
            self._adj[edge.from_switch].append(eid)
            # Reverse traversal is also valid (bidirectional track)
            self._adj[edge.to_switch].append(eid)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def edges_from(
        self, switch_id: str, port: Optional[SwitchPort] = None
    ) -> list[Edge]:
        """Edges leaving a switch, optionally filtered by port."""
        result = []
        for eid in self._adj.get(switch_id, []):
            e = self.edges[eid]
            if e.from_switch == switch_id:
                if port is None or e.from_port == port:
                    result.append(e)
            elif e.to_switch == switch_id:
                # reverse direction: the "from_port" in reverse is to_port
                if port is None or e.to_port == port:
                    result.append(e)
        return result

    def edge_between(self, sw_a: str, sw_b: str) -> list[Edge]:
        """All edges directly connecting two switches (either direction)."""
        result = []
        for eid, e in self.edges.items():
            if (e.from_switch == sw_a and e.to_switch == sw_b) or (
                e.from_switch == sw_b and e.to_switch == sw_a
            ):
                result.append(e)
        return result

    def sensor_on_edge(self, sensor_id: int) -> Optional[Edge]:
        """Find which edge a sensor sits on."""
        for e in self.edges.values():
            if sensor_id in e.sensors:
                return e
        return None

    def switch_required_position(
        self, switch_id: str, port: SwitchPort
    ) -> Optional[bool]:
        """
        What position must a switch be in to route through the given port?

        Returns:
            None  - trunk is always connected regardless of position
            False - switch must be set to STRAIGHT
            True  - switch must be set to DIVERGE
        """
        if port == SwitchPort.TRUNK:
            return None  # trunk is common to both positions
        elif port == SwitchPort.STRAIGHT:
            return False
        else:
            return True

    def neighbors(self, switch_id: str) -> set[str]:
        """All switches directly reachable from this one."""
        result = set()
        for eid in self._adj.get(switch_id, []):
            e = self.edges[eid]
            if e.from_switch == switch_id:
                result.add(e.to_switch)
            else:
                result.add(e.from_switch)
        return result

    def find_route(
        self,
        from_switch: str,
        to_switch: str,
    ) -> Optional[list[Edge]]:
        """
        BFS shortest path (fewest edges) between two switches.

        Returns a list of edges forming the route, or None if unreachable.
        The route ignores current switch positions and block occupancy;
        the dispatcher is responsible for setting switches and acquiring
        blocks before moving a train.
        """
        if from_switch == to_switch:
            return []
        if from_switch not in self.switches or to_switch not in self.switches:
            return None

        from collections import deque

        queue: deque[tuple[str, list[Edge]]] = deque([(from_switch, [])])
        visited: set[str] = {from_switch}

        while queue:
            current, path = queue.popleft()
            for eid in self._adj.get(current, []):
                edge = self.edges[eid]
                if edge.from_switch == current:
                    next_sw = edge.to_switch
                else:
                    next_sw = edge.from_switch

                if next_sw in visited:
                    continue
                visited.add(next_sw)
                new_path = path + [edge]
                if next_sw == to_switch:
                    return new_path
                queue.append((next_sw, new_path))

        return None

    def route_switch_settings(self, route: list[Edge]) -> list[tuple[str, bool]]:
        """
        Given a route (list of edges), return the switch settings needed.

        Returns list of (switch_id, diverge: bool) for motorized switches
        that need to be set. Manual switches are included but flagged
        via switch_type so the dispatcher can warn if misaligned.
        """
        settings: list[tuple[str, bool]] = []
        for i, edge in enumerate(route):
            # Entry switch: which port are we leaving from?
            pos = self.switch_required_position(edge.from_switch, edge.from_port)
            if pos is not None:
                settings.append((edge.from_switch, pos))

            # Exit switch: which port are we arriving at?
            pos = self.switch_required_position(edge.to_switch, edge.to_port)
            if pos is not None:
                settings.append((edge.to_switch, pos))

        # Deduplicate (a switch may appear in consecutive edges)
        seen: set[str] = set()
        unique: list[tuple[str, bool]] = []
        for sw_id, diverge in settings:
            if sw_id not in seen:
                seen.add(sw_id)
                unique.append((sw_id, diverge))
        return unique

    def route_blocks(self, route: list[Edge]) -> list[str]:
        """Block IDs that must be acquired for a route, in order."""
        blocks: list[str] = []
        for edge in route:
            if edge.block and (not blocks or blocks[-1] != edge.block):
                blocks.append(edge.block)
        return blocks

    def route_sensors(self, route: list[Edge]) -> list[int]:
        """Sensor IDs a train will encounter along a route, in order."""
        sensors: list[int] = []
        for edge in route:
            sensors.extend(edge.sensors)
        return sensors

    # ------------------------------------------------------------------
    # Runtime configuration (wiring populated from config, not hardcoded)
    # ------------------------------------------------------------------

    def configure_switch_wiring(
        self, switch_id: str, hub_id: int, port_name: str
    ) -> None:
        """Attach BLE addressing to a motorized switch (from dispatcher config)."""
        switch = self.switches.get(switch_id)
        if switch is None:
            raise KeyError(f"Unknown switch id: {switch_id}")
        self.switches[switch_id] = replace(switch, hub_id=hub_id, port_name=port_name)

    def configure_sensor_uid(self, sensor_id: int, uid: str) -> None:
        """Attach a physical RFID UID to a sensor (from dispatcher config)."""
        sensor = self.sensors.get(sensor_id)
        if sensor is None:
            raise KeyError(f"Unknown sensor id: {sensor_id}")
        self.sensors[sensor_id] = replace(sensor, tag_uid=uid)

    def sensor_id_for_uid(self, uid: str) -> Optional[int]:
        """
        Map a physical RFID UID to a logical sensor id.

        Falls back to treating the sensor id itself (as a string) as the UID
        when no explicit tag_uid has been configured -- handy for tests and
        mock runs that don't need real hardware UIDs.
        """
        for sid, sensor in self.sensors.items():
            if sensor.tag_uid is not None:
                if sensor.tag_uid == uid:
                    return sid
            elif str(sid) == uid:
                return sid
        return None

    # ------------------------------------------------------------------
    # Train registry and live position/movement tracking
    # ------------------------------------------------------------------

    def register_train(
        self,
        train_id: str,
        hub_id: str,
        route: Optional[list[str]] = None,
        smart_drive: bool = False,
        start_switch: Optional[str] = None,
    ) -> None:
        """
        Register a train, either with a fixed cyclic route or in smart-drive mode.

        `route` is required unless `smart_drive=True`, in which case the train
        instead picks its next edge dynamically (see _choose_next_edge) --
        `start_switch` sets its initial position (defaults to an arbitrary
        motorized switch if omitted).
        """
        if not route and not smart_drive:
            raise ValueError(
                f"Train {train_id} needs a non-empty route, or smart_drive=True"
            )
        if route:
            for switch_id in route:
                if switch_id not in self.switches:
                    raise ValueError(
                        f"Train {train_id} route references unknown switch {switch_id}"
                    )
        self.trains[train_id] = Train(
            id=train_id, hub_id=hub_id, route=list(route) if route else None
        )
        if route:
            initial_position = route[0]
        elif start_switch is not None:
            if start_switch not in self.switches:
                raise ValueError(f"Unknown start_switch {start_switch}")
            initial_position = start_switch
        else:
            initial_position = next(iter(self.switches))
        self._train_start_switch[train_id] = start_switch
        self.train_position[train_id] = initial_position
        self._train_route_index[train_id] = 0
        self._pending_edges[train_id] = []
        self._train_stopped[train_id] = True
        self._train_self_drive[train_id] = False
        self._train_smart_drive[train_id] = smart_drive
        self._train_edge_tick[train_id] = {}
        self._train_tick[train_id] = 0
        self._train_last_edge[train_id] = None
        self._train_last_confirmed_edge.pop(train_id, None)

    def reset_train(self, train_id: str) -> None:
        """
        Put a train back to its just-registered state: start position,
        route index, no pending/confirmed edges, stopped, self-drive off.

        Used to recover after a manual repositioning without restarting the
        service. Blocks the train occupies are freed too.
        """
        train = self.trains[train_id]
        for block in self.blocks.values():
            if block.occupied_by == train_id:
                block.occupied_by = None
        self._train_last_tag_time.pop(train_id, None)
        self.register_train(
            train_id,
            train.hub_id,
            route=train.route,
            smart_drive=self._train_smart_drive.get(train_id, False),
            start_switch=self._train_start_switch.get(train_id),
        )

    def is_smart_drive(self, train_id: str) -> bool:
        """True if this train picks its next edge dynamically instead of a fixed route."""
        return self._train_smart_drive.get(train_id, False)

    def mark_tag_seen(self, train_id: str, timestamp: float) -> None:
        """Record that a train reported a tag at `timestamp`, for watchdog timing."""
        self._train_last_tag_time[train_id] = timestamp

    def seconds_since_last_tag(
        self, train_id: str, now: Optional[float] = None
    ) -> float:
        """Seconds since a train's last recorded tag; 0 if it's never reported one."""
        last = self._train_last_tag_time.get(train_id)
        if last is None:
            return 0.0
        return (now if now is not None else time.time()) - last

    def update_battery(self, train_id: str, voltage: float) -> None:
        """Record a battery voltage reading for a train."""
        self.train_battery[train_id] = voltage

    def get_battery(self, train_id: str) -> Optional[float]:
        """Return the last known battery voltage for a train, or None."""
        return self.train_battery.get(train_id)

    def mark_stopped(self, train_id: str, stopped: bool) -> None:
        """Record whether a train is intentionally stopped (vs. cruising)."""
        self._train_stopped[train_id] = stopped

    def has_pending_edges(self, train_id: str) -> bool:
        """True if the train has been granted edges it hasn't yet confirmed."""
        return bool(self._pending_edges.get(train_id))

    def is_moving(self, train_id: str) -> bool:
        """True once a train has been explicitly marked as not stopped."""
        return not self._train_stopped.get(train_id, True)

    def set_self_drive(self, train_id: str, enabled: bool) -> None:
        """Record whether the dispatcher may automatically advance this train."""
        self._train_self_drive[train_id] = enabled

    def is_self_drive(self, train_id: str) -> bool:
        """True only once self-drive has been explicitly enabled for this train."""
        return self._train_self_drive.get(train_id, False)

    def train_id_for_hub_id(self, hub_id: str) -> Optional[str]:
        """Reverse-lookup a train_id from its BLE hub address, or None if unregistered."""
        for train in self.trains.values():
            if train.hub_id == hub_id:
                return train.id
        return None

    def hops_to_switch(self, train_id: str, target_switch: str) -> int:
        """Route-hops from a train's current position to target_switch (for contention)."""
        train = self.trains.get(train_id)
        if train is None:
            return 0
        if self.is_smart_drive(train_id):
            current = self.train_position.get(train_id)
            if current is None:
                return 0
            path = self.find_route(current, target_switch)
            # Unreachable -- treat as maximally far so it never wins a tiebreak.
            return len(path) if path is not None else len(self.edges) + 1
        if not train.route:
            return 0
        route_len = len(train.route)
        idx = self._train_route_index.get(train_id, 0)
        for offset in range(route_len):
            if train.route[(idx + offset) % route_len] == target_switch:
                return offset
        return route_len

    def next_block_chain_for_train(self, train_id: str) -> Optional[list[Edge]]:
        """
        The next chain of edges a train should be granted, in route order.

        Most blocks carry no sensor of their own, so a single tag read can't
        confirm each one individually: the chain extends through consecutive
        sensorless edges and stops after the first edge that does carry a
        sensor (inclusive), since that's the next point positioning can be
        confirmed. The whole chain is granted/released together
        (see BlockManager) -- all or nothing, like switch-setting already is.
        """
        train = self.trains.get(train_id)
        if train is None:
            return None
        if self.is_smart_drive(train_id):
            return self._next_chain_smart_drive(train_id)
        if not train.route:
            return None
        return self._fixed_route_chain(train, self._train_route_index.get(train_id, 0))

    def _fixed_route_chain(self, train: Train, start_idx: int) -> Optional[list[Edge]]:
        """The chain of a fixed-route train's edges starting at route index `start_idx`."""
        route = train.route or []
        route_len = len(route)
        chain: list[Edge] = []
        for step in range(route_len):
            current_switch = route[(start_idx + step) % route_len]
            next_switch = route[(start_idx + step + 1) % route_len]
            candidates = self.edge_between(current_switch, next_switch)
            if not candidates:
                break
            edge = candidates[0]
            chain.append(edge)
            if edge.sensors:
                break
        return chain or None

    def lookahead_chain(self, train_id: str) -> Optional[list[Edge]]:
        """
        The chain a fixed-route train would be granted *after* its current
        pending one, or None for smart-drive trains (whose path is chosen
        dynamically) and trains without a route.
        """
        train = self.trains.get(train_id)
        if train is None or self.is_smart_drive(train_id) or not train.route:
            return None
        pending = self._pending_edges.get(train_id, [])
        start_idx = self._train_route_index.get(train_id, 0) + len(pending)
        return self._fixed_route_chain(train, start_idx)

    def lookahead_chain_for_sensor(
        self, train_id: str, sensor_id: int
    ) -> Optional[list[Edge]]:
        """
        The lookahead chain, if `sensor_id` sits on it -- i.e. the sensor is
        the very next one the train should have read had it not missed one.

        Used to recognize a single missed read: a train that skipped a tag and
        then read exactly the following one is where its route says it should
        be. Smart-drive trains and any other sensor return None.
        """
        chain = self.lookahead_chain(train_id)
        if not chain:
            return None
        target = self.sensor_on_edge(sensor_id)
        return chain if target is not None and target in chain else None

    def switches_reserved_from(self, train_id: str) -> set[str]:
        """
        Switches `train_id` must not flip ahead of time: any switch needed by
        its own granted chain or by another train's granted chain, plus any
        switch another train is currently positioned at.
        """
        reserved: set[str] = set()
        for other_id in self.trains:
            pending = self._pending_edges.get(other_id, [])
            reserved.update(sw for sw, _ in self.route_switch_settings(pending))
            if other_id != train_id:
                position = self.train_position.get(other_id)
                if position is not None:
                    reserved.add(position)
        return reserved

    def extend_pending_chain(self, train_id: str, chain: list[Edge]) -> None:
        """Append edges to a train's granted-but-unconfirmed chain."""
        self._pending_edges[train_id] = self._pending_edges.get(train_id, []) + list(
            chain
        )

    def _edge_requires_manual_position(self, switch_id: str, port: SwitchPort) -> bool:
        """
        True if departing `switch_id` via `port` requires a manual switch to
        already be in a specific, dispatcher-unverifiable position.

        TRUNK is always connected regardless of position, so it's always safe;
        STRAIGHT/DIVERGE on a MANUAL switch is not, since the dispatcher can
        neither read nor set that switch's physical state.
        """
        switch = self.switches.get(switch_id)
        return (
            switch is not None
            and switch.switch_type == SwitchType.MANUAL
            and port != SwitchPort.TRUNK
        )

    def _departure_port(self, edge: Edge, from_switch: str) -> SwitchPort:
        """The port on `from_switch` a train uses to depart along `edge`."""
        return edge.from_port if edge.from_switch == from_switch else edge.to_port

    def _other_switch(self, edge: Edge, switch_id: str) -> str:
        """The switch at the far end of `edge` from `switch_id`."""
        return edge.to_switch if edge.from_switch == switch_id else edge.from_switch

    def _choose_next_edge(
        self,
        train_id: str,
        current_switch: str,
        last_edge_id: Optional[str] = None,
        edge_tick: Optional[dict[str, int]] = None,
    ) -> Optional[Edge]:
        """
        Pick the next edge for a smart-drive train, favoring full coverage.

        Prefers candidates no other train holds, then the
        least-recently-visited of those (never-visited
        edges rank above any visited one), avoids immediately reversing back
        along the edge just traversed when an alternative exists, and never
        departs via a manual switch's straight/diverge port (see
        _edge_requires_manual_position). Ties break lexically by edge id for
        determinism.

        `last_edge_id`/`edge_tick` default to the train's last *confirmed*
        state, but a caller building a multi-hop chain in one call (see
        _next_chain_smart_drive) must pass its own evolving copies -- without
        that, reversal-avoidance and recency both only know about edges from
        *previous* chains, so a chain being built in a single call could
        bounce back and forth on one just-chosen edge forever.
        """
        candidates = [
            e
            for e in self.edges_from(current_switch)
            if not self._edge_requires_manual_position(
                current_switch, self._departure_port(e, current_switch)
            )
        ]
        if not candidates:
            return None

        if last_edge_id is None:
            last_edge_id = self._train_last_edge.get(train_id)
        if edge_tick is None:
            edge_tick = self._train_edge_tick.get(train_id, {})

        non_reversing = [e for e in candidates if e.id != last_edge_id]
        pool = non_reversing or candidates

        # Prefer an exit nobody else is in: a train that insists on the
        # least-recently-visited edge even when another train holds it just
        # waits there, and two trains each wanting the other's edge deadlock.
        # If every exit is taken the train still picks one and queues.
        free = [e for e in pool if self._block_free_for(e, train_id)]
        pool = free or pool

        return min(pool, key=lambda e: (edge_tick.get(e.id, -1), e.id))

    def _block_free_for(self, edge: Edge, train_id: str) -> bool:
        """True if the edge's block is unoccupied or already held by `train_id`."""
        block = self.blocks.get(edge.block)
        return block is None or block.occupied_by in (None, train_id)

    def _next_chain_smart_drive(self, train_id: str) -> Optional[list[Edge]]:
        """Dynamic analog of the fixed-route chain-building loop above."""
        current_switch = self.train_position.get(train_id)
        if current_switch is None:
            return None
        chain: list[Edge] = []
        last_edge_id = self._train_last_edge.get(train_id)
        edge_tick = dict(self._train_edge_tick.get(train_id, {}))
        tick = self._train_tick.get(train_id, 0)
        # Safety bound: no chain should ever need more hops than there are edges.
        for _ in range(len(self.edges) + 1):
            edge = self._choose_next_edge(
                train_id, current_switch, last_edge_id, edge_tick
            )
            if edge is None:
                break
            chain.append(edge)
            tick += 1
            edge_tick[edge.id] = tick
            last_edge_id = edge.id
            current_switch = self._other_switch(edge, current_switch)
            if edge.sensors:
                break
        return chain or None

    def grant_pending_chain(self, train_id: str, chain: list[Edge]) -> None:
        """Mark a chain of edges as granted-but-not-yet-confirmed for a train."""
        self._pending_edges[train_id] = list(chain)

    def record_tag_event(
        self, train_id: str, sensor_id: int, timestamp: float
    ) -> TagEventResult:
        """
        Record a sensor read for a train and confirm any pending edges it clears.

        A sensor read only confirms edges up to and including the edge it
        sits on; since chains are built to end at the first sensored edge,
        that's normally the entire pending chain. Reads for a sensor whose
        edge isn't currently pending for this train (unregistered train,
        stray/foreign read) are ignored -- no position update, no completion.
        """
        previous_position = self.train_position.get(train_id)
        train = self.trains.get(train_id)
        if train is None:
            return TagEventResult(train_id, previous_position, previous_position, [])

        edge = self.sensor_on_edge(sensor_id)
        pending = self._pending_edges.get(train_id, [])
        if edge is None or edge not in pending:
            # Deliberately does not refresh the watchdog: a stray read means
            # the train is somewhere the dispatcher didn't expect, not that
            # it is making progress along its granted chain.
            return TagEventResult(
                train_id,
                previous_position,
                previous_position,
                [],
                matched=False,
                duplicate=edge is not None
                and edge.id == self._train_last_confirmed_edge.get(train_id),
            )

        self.mark_tag_seen(train_id, timestamp)
        self._train_last_confirmed_edge[train_id] = edge.id

        i = pending.index(edge)
        completed = pending[: i + 1]
        self._pending_edges[train_id] = pending[i + 1 :]

        smart = self.is_smart_drive(train_id)
        if not smart:
            self._train_route_index[train_id] = self._train_route_index.get(
                train_id, 0
            ) + len(completed)

        # Derive the new position by walking the completed edges from the
        # train's last known position -- works the same way regardless of
        # whether the route is fixed or dynamically chosen.
        position = previous_position
        for completed_edge in completed:
            if position is None:
                break
            position = self._other_switch(completed_edge, position)
            if smart:
                tick = self._train_tick.get(train_id, 0) + 1
                self._train_tick[train_id] = tick
                self._train_edge_tick.setdefault(train_id, {})[completed_edge.id] = tick
        if smart and completed:
            self._train_last_edge[train_id] = completed[-1].id

        new_position = position if position is not None else previous_position
        self.train_position[train_id] = new_position
        return TagEventResult(
            train_id, previous_position, new_position, completed, matched=True
        )

    # ------------------------------------------------------------------
    # Block occupancy
    # ------------------------------------------------------------------

    def is_block_free(self, block_id: str) -> bool:
        block = self.blocks.get(block_id)
        return block is None or block.occupied_by is None

    def occupy_block(self, block_id: str, train_id: str) -> None:
        block = self.blocks.get(block_id)
        if block is not None:
            block.occupied_by = train_id

    def free_block(self, block_id: str) -> None:
        block = self.blocks.get(block_id)
        if block is not None:
            block.occupied_by = None

    # ------------------------------------------------------------------
    # Debug / display
    # ------------------------------------------------------------------

    def summary(self) -> str:
        lines = [
            f"TrackModel: {len(self.switches)} switches, "
            f"{len(self.edges)} edges, "
            f"{len(self.sensors)} sensors, "
            f"{len(self.blocks)} blocks",
            "",
            "Switches:",
        ]
        for s in self.switches.values():
            lines.append(f"  {s}")
        lines.append("")
        lines.append("Edges:")
        for e in self.edges.values():
            lines.append(f"  {e.id}: {e}")
        lines.append("")
        lines.append("Blocks:")
        for b in self.blocks.values():
            lines.append(f"  {b.id}: {b.description}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    model = TrackModel()
    print(model.summary())
    print()

    # Example: route from A to J
    route = model.find_route("A", "J")
    if route:
        print(f"Route A → J ({len(route)} edges):")
        for e in route:
            print(f"  {e}")
        print(f"Switch settings: {model.route_switch_settings(route)}")
        print(f"Blocks to acquire: {model.route_blocks(route)}")
        print(f"Sensors on route: {model.route_sensors(route)}")

    # Example: route from C to I
    route = model.find_route("C", "I")
    if route:
        print(f"\nRoute C → I ({len(route)} edges):")
        for e in route:
            print(f"  {e}")
        print(f"Switch settings: {model.route_switch_settings(route)}")
        print(f"Blocks to acquire: {model.route_blocks(route)}")
        print(f"Sensors on route: {model.route_sensors(route)}")
