"""Tests for the virtual-time dispatcher simulator (dispatcher/simulate.py)."""

import asyncio

import pytest

from dispatcher.simulate import (
    Simulation,
    find_period,
    run_seeds,
    window_variety,
)


@pytest.fixture(autouse=True)
def restore_event_loop():
    """run_seeds() uses asyncio.run(), which leaves the main thread with no
    current event loop; later tests calling asyncio.get_event_loop() would fail."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


class TestPathMetrics:
    def test_find_period_detects_repeating_loop(self):
        sequence = ["a"] * 5 + ["x", "y", "z"] * 20
        assert find_period(sequence) == 3

    def test_find_period_none_for_varying_path(self):
        sequence = [str(i) for i in range(100)]
        assert find_period(sequence) is None

    def test_window_variety_low_for_loop_high_for_novel(self):
        assert window_variety(["x", "y"] * 50) < 0.1
        assert window_variety([str(i) for i in range(100)]) == 1.0

    def test_empty_sequences_are_safe(self):
        assert find_period([]) is None
        assert window_variety([]) == 0.0


class TestSimulation:
    def test_single_train_runs_clean_and_covers_layout(self):
        (result,) = run_seeds(1, [0], 600.0)
        assert result.outcome == "ok"
        stats = result.trains["TRN-1"]
        assert stats.edges_entered > 50
        assert stats.distinct_edges >= 8

    def test_runs_are_deterministic_per_seed(self):
        first = run_seeds(1, [3], 300.0)[0]
        second = run_seeds(1, [3], 300.0)[0]
        assert first.trains == second.trains
        assert first.outcome == second.outcome

    def test_too_many_trains_rejected(self):
        with pytest.raises(ValueError):
            Simulation(n_trains=11, seed=0, duration=10.0)

    def test_overlap_is_reported_as_collision(self):
        sim = Simulation(n_trains=2, seed=0, duration=10.0)
        edge = sim.model.edges["BD"]
        for train in sim.trains.values():
            train.edge = edge
        sim._check_places()
        assert sim.outcome == "collision"

    def test_two_trains_run_clean(self):
        results = run_seeds(2, list(range(10)), 600.0)
        assert all(r.outcome == "ok" for r in results), [
            (r.seed, r.outcome, r.detail) for r in results
        ]
