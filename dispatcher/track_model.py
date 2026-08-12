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
    """A train's identity and its fixed, pre-assigned cyclic route through switches."""

    id: str
    hub_id: str  # BLE address of the train hub, e.g. "90:84:2B:18:28:36"
    route: list[str]  # cyclic list of switch ids


@dataclass
class TagEventResult:
    """Outcome of recording a single RFID sensor read."""

    train_id: str
    previous_position: Optional[str]
    current_position: Optional[str]
    edges_completed: list[Edge]


# ---------------------------------------------------------------------------
# Track model
# ---------------------------------------------------------------------------


class TrackModel:
    """
    Full graph of the LEGO train layout.

    Nodes = switches (10 total: 7 motorized, 3 manual)
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
        self._train_self_drive: dict[str, bool] = {}
        # Edges granted to a train but not yet confirmed cleared by a tag read,
        # in route order. See TrackModel.next_block_chain_for_train.
        self._pending_edges: dict[str, list[Edge]] = {}

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
        # are A-I plus K.
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
        ]
        for sid, desc in defs:
            self.sensors[sid] = Sensor(id=sid, description=desc)

    def _build_edges(self) -> None:
        E = Edge
        P = SwitchPort
        edges = [
            E("AH", "A", P.TRUNK, "H", P.TRUNK, [6], "BLK_AH"),
            E("AK", "A", P.STRAIGHT, "K", P.STRAIGHT, [], "BLK_AK"),
            E("AC", "A", P.DIVERGE, "C", P.STRAIGHT, [], "BLK_AC"),
            E("BD", "B", P.TRUNK, "D", P.STRAIGHT, [1], "BLK_BD"),
            E("BK", "B", P.STRAIGHT, "K", P.DIVERGE, [2, 5], "BLK_BK"),
            E("BG", "B", P.DIVERGE, "G", P.DIVERGE, [8], "BLK_BG"),
            E("CF", "C", P.TRUNK, "F", P.DIVERGE, [], "BLK_CF"),
            E("CH", "C", P.DIVERGE, "H", P.DIVERGE, [9], "BLK_CH"),
            E("DE_S", "D", P.TRUNK, "E", P.STRAIGHT, [3], "BLK_DE_S"),
            E("DE_D", "D", P.DIVERGE, "E", P.DIVERGE, [7], "BLK_DE_D"),
            E("EI", "E", P.TRUNK, "I", P.DIVERGE, [], "BLK_EI"),
            E("FG", "F", P.TRUNK, "G", P.STRAIGHT, [4], "BLK_FG"),
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
            ("BLK_AC", ["AC"], "A(div)-C(str)"),
            ("BLK_BD", ["BD"], "B(trunk)-TAG_1-D(str)"),
            ("BLK_BK", ["BK"], "B(str)-TAG_2-TAG_5-K(div)"),
            ("BLK_BG", ["BG"], "Crossover: B(div)-TAG_8-G(div)"),
            ("BLK_CF", ["CF"], "C(trunk)-F(div)"),
            ("BLK_CH", ["CH"], "Crossover: C(div)-TAG_9-H(div)"),
            ("BLK_DE_S", ["DE_S"], "D(trunk)-TAG_3-E(str)"),
            ("BLK_DE_D", ["DE_D"], "Crossover: D(div)-TAG_7-E(div)"),
            ("BLK_EI", ["EI"], "E(trunk)-I(div)"),
            ("BLK_FG", ["FG"], "F(trunk)-TAG_4-G(str)"),
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

    def register_train(self, train_id: str, hub_id: str, route: list[str]) -> None:
        """Register a train with its fixed, pre-assigned cyclic route of switch ids."""
        if not route:
            raise ValueError(f"Train {train_id} needs a non-empty route")
        for switch_id in route:
            if switch_id not in self.switches:
                raise ValueError(
                    f"Train {train_id} route references unknown switch {switch_id}"
                )
        self.trains[train_id] = Train(id=train_id, hub_id=hub_id, route=list(route))
        self.train_position[train_id] = route[0]
        self._train_route_index[train_id] = 0
        self._pending_edges[train_id] = []
        self._train_stopped[train_id] = True
        self._train_self_drive[train_id] = False

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
        if train is None or not train.route:
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
        if train is None or not train.route:
            return None
        route_len = len(train.route)
        idx = self._train_route_index.get(train_id, 0)
        chain: list[Edge] = []
        for step in range(route_len):
            current_switch = train.route[(idx + step) % route_len]
            next_switch = train.route[(idx + step + 1) % route_len]
            candidates = self.edge_between(current_switch, next_switch)
            if not candidates:
                break
            edge = candidates[0]
            chain.append(edge)
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
        self.mark_tag_seen(train_id, timestamp)
        previous_position = self.train_position.get(train_id)
        train = self.trains.get(train_id)
        if train is None:
            return TagEventResult(train_id, previous_position, previous_position, [])

        edge = self.sensor_on_edge(sensor_id)
        pending = self._pending_edges.get(train_id, [])
        if edge is None or edge not in pending:
            return TagEventResult(train_id, previous_position, previous_position, [])

        i = pending.index(edge)
        completed = pending[: i + 1]
        self._pending_edges[train_id] = pending[i + 1 :]
        self._train_route_index[train_id] = self._train_route_index.get(
            train_id, 0
        ) + len(completed)
        new_position = train.route[self._train_route_index[train_id] % len(train.route)]
        self.train_position[train_id] = new_position
        return TagEventResult(train_id, previous_position, new_position, completed)

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
