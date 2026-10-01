"""
Virtual-time simulator for smart-drive dispatching.

Drives the *real* Dispatcher, BlockManager and TrackModel with simulated
trains that physically follow whatever chains the dispatcher grants them,
then reports safety violations (collisions, trains moving without a granted
chain, deadlocks, emergency stops) and how varied the trains' paths are.
No MQTT broker, BLE hardware or wall-clock waiting is involved: time is a
float advanced from event to event.

Usage:
    python -m dispatcher.simulate --trains 2 --duration 1800 --seeds 20
    python -m dispatcher.simulate --trains 2 --seeds 5 -v

What is and isn't modelled: trains take a seeded random time to traverse each
edge, read each sensor at a fixed fraction along it, and keep coasting briefly
after a stop command. MQTT and BLE latency delay tag delivery and command
effect. Dispatcher switch commands always succeed instantly, physical
switches aren't modelled (a train is assumed to follow its granted chain),
and the watchdog isn't run -- only tag-driven behaviour is exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from config import get_settings
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import TagEvent
from dispatcher.track_model import Edge, SwitchType, TrackModel

Place = Tuple[str, str]


class SimTrackModel(TrackModel):
    """TrackModel that reports every chain grant to the simulator."""

    def __init__(self) -> None:
        """Build the real track topology with no grant listener attached."""
        super().__init__()
        self.on_grant: Optional[Callable[[str, List[Edge], bool], None]] = None

    def grant_pending_chain(self, train_id: str, chain: List[Edge]) -> None:
        """Record the grant, flagging a repeat of a chain not yet confirmed."""
        duplicate = self._pending_edges.get(train_id) == chain
        super().grant_pending_chain(train_id, chain)
        if self.on_grant is not None:
            self.on_grant(train_id, list(chain), duplicate)


@dataclass
class SimTrain:
    """Ground-truth physical state of one simulated train."""

    train_id: str
    node: Optional[str]
    edge: Optional[Edge] = None
    exit_node: Optional[str] = None
    elapsed: float = 0.0
    length: float = 0.0
    tags_left: List[Tuple[float, int]] = field(default_factory=list)
    todo: List[Edge] = field(default_factory=list)
    power_on: bool = False
    coast_left: Optional[float] = None
    stopped_s: float = 0.0
    edges_entered: List[str] = field(default_factory=list)
    # Edge the tail is still in after the nose has left it, and how many more
    # seconds of motion until the tail clears it (the reader is on the nose).
    trail: Optional[Edge] = None
    trail_left: float = 0.0

    @property
    def moving(self) -> bool:
        """True while on an edge with power applied or still coasting."""
        return self.edge is not None and (self.power_on or self.coast_left is not None)

    def places(self) -> set[Place]:
        """Track pieces this train physically occupies."""
        places: set[Place] = (
            {("edge", self.edge.id)}
            if self.edge is not None
            else {("node", self.node or "?")}
        )
        if self.trail is not None:
            places.add(("edge", self.trail.id))
        return places


@dataclass
class TrainStats:
    """Per-train outcome metrics for one run."""

    edges_entered: int
    distinct_edges: int
    stopped_fraction: float
    period: Optional[int]
    variety: float


@dataclass
class SimResult:
    """Outcome of one simulated run."""

    seed: int
    outcome: str  # ok | collision | unprotected | inconsistent | deadlock | estop
    end_time: float
    detail: str
    trains: dict[str, TrainStats]


def find_period(sequence: List[str], max_period: int = 60) -> Optional[int]:
    """
    Smallest period the second half of `sequence` repeats exactly, or None.

    A train locked into a fixed loop shows up as a short period; a train
    whose path keeps varying has none.
    """
    tail = sequence[len(sequence) // 2 :]
    for period in range(1, min(max_period, len(tail) // 2) + 1):
        if all(tail[i] == tail[i - period] for i in range(period, len(tail))):
            return period
    return None


def window_variety(sequence: List[str], width: int = 8) -> float:
    """Share of distinct `width`-edge windows in the second half (1.0 = all new)."""
    tail = sequence[len(sequence) // 2 :]
    windows = [tuple(tail[i : i + width]) for i in range(len(tail) - width + 1)]
    return len(set(windows)) / len(windows) if windows else 0.0


class _TrainStub:
    """TrainController stand-in: forwards power commands to the simulator."""

    def __init__(self, sim: "Simulation") -> None:
        self._sim = sim

    async def handle_command(self, hub_id: str, power: int) -> None:
        self._sim.issue_power(hub_id, power)


class _SwitchStub:
    """SwitchController stand-in: every command succeeds instantly."""

    def __init__(self) -> None:
        self.commands = 0

    async def send_command_with_retry(
        self, hub_id: int, switch_name: str, position: int, max_retries: int = 3
    ) -> bool:
        self.commands += 1
        return True


class Simulation:
    """One seeded run of N smart-drive trains on the real layout."""

    def __init__(
        self,
        n_trains: int,
        seed: int,
        duration: float,
        tag_fraction: float = 0.5,
        coast_s: float = 0.3,
        tag_latency: float = 0.2,
        command_latency: float = 0.3,
        train_length_cm: float = 26.0,
        speed_cm_s: float = 25.0,
    ) -> None:
        """Build the track, register trains at random distinct switches."""
        self.seed = seed
        self.duration = duration
        self.tag_fraction = tag_fraction
        self.coast_s = coast_s
        self.tag_latency = tag_latency
        self.command_latency = command_latency
        self.train_len_s = train_length_cm / speed_cm_s
        self.now = 0.0
        self.outcome: Optional[str] = None
        self.detail = ""
        self._seq = 0
        self._inbox: List[Tuple[float, int, str, int]] = []
        self._outbox: List[Tuple[float, int, str, int]] = []

        rng = random.Random(seed)
        self.model = SimTrackModel()
        self.model.on_grant = self._on_grant
        self._edge_len = {
            eid: rng.uniform(3.0, 6.0) for eid in sorted(self.model.edges)
        }
        self._hub_to_train: dict[str, str] = {}
        self._wire_switches()

        starts = self._pick_starts(rng, n_trains)
        self.trains: dict[str, SimTrain] = {}
        for i, start in enumerate(starts, start=1):
            train_id, hub_id = f"TRN-{i}", f"hub-{i}"
            self.model.register_train(
                train_id, hub_id, smart_drive=True, start_switch=start
            )
            self._hub_to_train[hub_id] = train_id
            self.trains[train_id] = SimTrain(train_id, node=start)

        self.switch_stub = _SwitchStub()
        self.dispatcher = Dispatcher(
            track_model=self.model,
            block_manager=BlockManager(self.model),
            bridge=None,  # type: ignore[arg-type]
            train_controller=_TrainStub(self),  # type: ignore[arg-type]
            switch_controller=self.switch_stub,  # type: ignore[arg-type]
            settings=get_settings(),
        )

    def _wire_switches(self) -> None:
        for i, (sid, switch) in enumerate(sorted(self.model.switches.items())):
            if switch.switch_type == SwitchType.MOTORIZED:
                self.model.configure_switch_wiring(sid, 100 + i, "SWITCH_A")

    def _pick_starts(self, rng: random.Random, n_trains: int) -> List[str]:
        candidates = sorted(self.model.switches)
        if n_trains > len(candidates):
            raise ValueError(f"at most {len(candidates)} trains fit on the layout")
        while True:
            starts = rng.sample(candidates, n_trains)
            probe = SimTrackModel()
            for i, start in enumerate(starts):
                probe.register_train(
                    f"P{i}", f"p{i}", smart_drive=True, start_switch=start
                )
            if not all(
                probe.next_block_chain_for_train(f"P{i}") for i in range(n_trains)
            ):
                continue
            # Trains on neighbouring switches share an edge, which the
            # dispatcher can't tell apart at startup (see _claim_start_blocks).
            touching = [{e.id for e in probe.edges_from(start)} for start in starts]
            if sum(len(t) for t in touching) == len(set().union(*touching)):
                return starts

    # ------------------------------------------------------------------
    # Dispatcher-facing hooks
    # ------------------------------------------------------------------

    def _on_grant(self, train_id: str, chain: List[Edge], duplicate: bool) -> None:
        """A newly granted chain is what the train will physically follow."""
        if not duplicate:
            self.trains[train_id].todo = chain

    def issue_power(self, hub_id: str, power: int) -> None:
        """Queue a power command; it reaches the train after BLE latency."""
        train_id = self._hub_to_train[hub_id]
        self._seq += 1
        self._outbox.append(
            (self.now + self.command_latency, self._seq, train_id, power)
        )

    # ------------------------------------------------------------------
    # Physical model
    # ------------------------------------------------------------------

    def _fractions(self, count: int) -> List[float]:
        if count == 1:
            return [self.tag_fraction]
        return [0.1 + 0.8 * i / (count - 1) for i in range(count)]

    def _violation(self, kind: str, detail: str) -> None:
        if self.outcome is None:
            self.outcome = kind
            self.detail = f"t={self.now:.1f}s: {detail}"

    def _enter_next_edge(self, train: SimTrain) -> None:
        edge = train.todo.pop(0)
        node = train.node
        if node not in (edge.from_switch, edge.to_switch):
            self._violation(
                "inconsistent",
                f"{train.train_id} at {node} was granted edge {edge.id} "
                f"({edge.from_switch}-{edge.to_switch}), which isn't connected to it",
            )
            return
        forward = node == edge.from_switch
        train.exit_node = edge.to_switch if forward else edge.from_switch
        train.length = self._edge_len[edge.id]
        fractions = self._fractions(len(edge.sensors))
        tags = [(f * train.length, s) for f, s in zip(fractions, edge.sensors)]
        if not forward:
            tags = [(train.length - t, s) for t, s in reversed(tags)]
        train.tags_left = tags
        train.elapsed = 0.0
        train.edge = edge
        train.node = None
        train.edges_entered.append(edge.id)
        for other in self.trains.values():
            if other is not train and ("edge", edge.id) in other.places():
                self._violation(
                    "collision",
                    f"{train.train_id} entered {edge.id} while {other.train_id} "
                    "was still on it",
                )

    def _check_places(self) -> None:
        seen: dict[Place, str] = {}
        for train in self.trains.values():
            for place in train.places():
                if place in seen:
                    self._violation(
                        "collision",
                        f"{train.train_id} and {seen[place]} both at {place[0]} "
                        f"{place[1]}",
                    )
                seen[place] = train.train_id

    def _advance(self, dt: float) -> None:
        for train in self.trains.values():
            if train.moving:
                train.elapsed += dt
                if train.coast_left is not None:
                    train.coast_left -= dt
                if train.trail is not None:
                    train.trail_left -= dt
            else:
                train.stopped_s += dt

    def _next_milestone(self) -> Optional[Tuple[float, int, object, str, object]]:
        candidates: List[Tuple[float, int, object, str, object]] = []
        for item in self._outbox:
            candidates.append((item[0], 0, item[1], "cmd", item))
        for item in self._inbox:
            candidates.append((item[0], 1, item[1], "deliver", item))
        for train in self.trains.values():
            if not train.moving:
                continue
            if train.tags_left:
                at = self.now + max(0.0, train.tags_left[0][0] - train.elapsed)
                candidates.append((at, 2, train.train_id, "tag", train))
            if train.trail is not None:
                at = self.now + max(0.0, train.trail_left)
                candidates.append((at, 3, train.train_id, "clear", train))
            at = self.now + max(0.0, train.length - train.elapsed)
            candidates.append((at, 4, train.train_id, "arrive", train))
            if train.coast_left is not None:
                at = self.now + max(0.0, train.coast_left)
                candidates.append((at, 5, train.train_id, "coast", train))
        if not candidates:
            return None
        return min(candidates, key=lambda c: c[:3])

    async def _process(self, kind: str, ref: object) -> None:
        if kind == "cmd":
            self._outbox.remove(ref)  # type: ignore[arg-type]
            _, _, train_id, power = ref  # type: ignore[misc]
            self._apply_power(self.trains[train_id], power)
        elif kind == "deliver":
            self._inbox.remove(ref)  # type: ignore[arg-type]
            _, _, train_id, sensor = ref  # type: ignore[misc]
            event = TagEvent(train_id=train_id, tag_uid=str(sensor), timestamp=self.now)
            await self.dispatcher._handle_tag_event(event)
            if self.dispatcher._emergency:
                self._violation(
                    "estop",
                    f"dispatcher emergency-stopped all trains "
                    f"(triggered by {self.dispatcher._emergency_train_id})",
                )
        elif kind == "tag":
            train: SimTrain = ref  # type: ignore[assignment]
            _, sensor = train.tags_left.pop(0)
            self._seq += 1
            self._inbox.append(
                (self.now + self.tag_latency, self._seq, train.train_id, sensor)
            )
        elif kind == "arrive":
            self._arrive(ref)  # type: ignore[arg-type]
        elif kind == "clear":
            train = ref  # type: ignore[assignment]
            train.trail = None
            train.trail_left = 0.0
        elif kind == "coast":
            train = ref  # type: ignore[assignment]
            train.coast_left = None

    def _apply_power(self, train: SimTrain, power: int) -> None:
        if power > 0:
            train.power_on = True
            train.coast_left = None
            if train.edge is None and train.todo:
                self._enter_next_edge(train)
            elif train.edge is None:
                self._violation(
                    "unprotected", f"{train.train_id} powered with no granted chain"
                )
        elif train.power_on:
            train.power_on = False
            train.coast_left = self.coast_s if train.edge is not None else None

    def _arrive(self, train: SimTrain) -> None:
        was_powered = train.power_on
        train.node = train.exit_node
        train.trail, train.trail_left = train.edge, self.train_len_s
        train.edge = None
        train.elapsed = 0.0
        train.tags_left = []
        for other in self.trains.values():
            if other is not train and ("node", train.node or "?") in other.places():
                self._violation(
                    "collision",
                    f"{train.train_id} reached switch {train.node} where "
                    f"{other.train_id} was stopped",
                )
        if train.todo:
            self._enter_next_edge(train)
        elif was_powered:
            train.power_on = False
            self._violation(
                "unprotected",
                f"{train.train_id} reached switch {train.node} still powered with "
                "no granted chain",
            )
        train.coast_left = None if train.edge is None else train.coast_left

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def _stall_detail(self) -> str:
        manager = self.dispatcher._block_manager
        waits = []
        for block_id, queue in sorted(manager._pending.items()):
            for train_id in queue:
                holder = manager._reserved_by.get(block_id, "nobody")
                waits.append(f"{train_id} waits on {block_id} (held by {holder})")
        return "; ".join(waits) or "no queued block requests"

    async def run(self) -> SimResult:
        """Simulate until the duration elapses, a violation occurs, or a stall."""
        # All trains are placed before any is enabled, so none is granted a
        # chain through another's starting switch.
        for train_id in self.trains:
            await self.dispatcher._claim_start_blocks(train_id)
        for train_id in self.trains:
            await self.dispatcher.set_self_drive(train_id, True)
        self._check_places()

        while self.now < self.duration and self.outcome is None:
            milestone = self._next_milestone()
            if milestone is None:
                self._violation("deadlock", self._stall_detail())
                break
            at, _, _, kind, ref = milestone
            at = min(at, self.duration)
            self._advance(at - self.now)
            self.now = at
            if at >= self.duration:
                break
            await self._process(kind, ref)
            self._check_places()

        return self._result()

    def _result(self) -> SimResult:
        stats = {}
        for train in self.trains.values():
            seq = train.edges_entered
            stats[train.train_id] = TrainStats(
                edges_entered=len(seq),
                distinct_edges=len(set(seq)),
                stopped_fraction=train.stopped_s / self.now if self.now else 0.0,
                period=find_period(seq),
                variety=window_variety(seq),
            )
        return SimResult(
            seed=self.seed,
            outcome=self.outcome or "ok",
            end_time=self.now,
            detail=self.detail,
            trains=stats,
        )


def run_seeds(
    n_trains: int, seeds: List[int], duration: float, **kwargs: float
) -> List[SimResult]:
    """Run one simulation per seed and collect the results."""
    return [
        asyncio.run(Simulation(n_trains, seed, duration, **kwargs).run())
        for seed in seeds
    ]


def format_report(results: List[SimResult], n_trains: int, duration: float) -> str:
    """Summarize many runs: outcomes, throughput, and path repetitiveness."""
    lines = [
        f"{len(results)} runs, {n_trains} trains, {duration:.0f}s simulated each",
        "",
    ]
    for outcome, count in Counter(r.outcome for r in results).most_common():
        lines.append(f"  {outcome:<12} {count}")

    finished = [r for r in results if r.outcome == "ok"]
    train_stats = [s for r in finished for s in r.trains.values()]
    if train_stats:
        stopped = statistics.mean(s.stopped_fraction for s in train_stats)
        rate = statistics.mean(s.edges_entered for s in train_stats) / duration * 60
        looped = [s for s in train_stats if s.period is not None]
        lines += [
            "",
            f"Clean runs: trains are stopped {stopped:.0%} of the time and "
            f"enter {rate:.1f} edges/min on average",
            f"  {len(looped)}/{len(train_stats)} trains settle into an exactly "
            "repeating loop"
            + (
                f" (median {statistics.median(s.period for s in looped):.0f} edges)"
                if looped
                else ""
            ),
            f"  path variety (distinct 8-edge windows): "
            f"{statistics.mean(s.variety for s in train_stats):.0%}",
        ]

    failures = [r for r in results if r.outcome != "ok"]
    if failures:
        lines += ["", "First failures:"]
        for r in failures[:5]:
            lines.append(f"  seed {r.seed}: {r.outcome} at {r.detail}")
    return "\n".join(lines)


def main() -> None:
    """Parse CLI args, run the simulations, print a report."""
    parser = argparse.ArgumentParser(description="Simulate smart-drive dispatching")
    parser.add_argument("--trains", type=int, default=2)
    parser.add_argument("--duration", type=float, default=1800.0, help="sim seconds")
    parser.add_argument("--seeds", type=int, default=20, help="number of seeds")
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument(
        "--tag-fraction",
        type=float,
        default=0.5,
        help="where along an edge its sensor sits (0-1) for single-sensor edges",
    )
    parser.add_argument("--train-length-cm", type=float, default=26.0)
    parser.add_argument("--speed-cm-s", type=float, default=25.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    results = run_seeds(
        args.trains,
        seeds,
        args.duration,
        tag_fraction=args.tag_fraction,
        train_length_cm=args.train_length_cm,
        speed_cm_s=args.speed_cm_s,
    )
    if args.verbose:
        for r in results:
            print(f"seed {r.seed}: {r.outcome} {r.detail}")
        print()
    print(format_report(results, args.trains, args.duration))
    raise SystemExit(0 if all(r.outcome == "ok" for r in results) else 1)


if __name__ == "__main__":
    main()
