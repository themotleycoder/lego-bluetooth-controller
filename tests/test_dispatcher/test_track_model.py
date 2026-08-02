"""Tests for dispatcher.track_model."""

from dispatcher.track_model import (
    BlockState,
    TagNode,
    TrackEdge,
    TrackModel,
    TrainDescriptor,
    build_sample_topology,
)


class TestSampleTopology:
    def test_sample_topology_shape(self):
        model = build_sample_topology()
        assert len(model.tags) == 14
        assert len(model.switches) == 7
        assert len(model.trains) == 2

    def test_sample_topology_trains_have_full_loop_routes(self):
        model = build_sample_topology()
        for train in model.trains.values():
            assert len(train.route) == 7

    def test_sample_topology_junction_switches_double_as_crossover(self):
        model = build_sample_topology()
        crossover_switch_ids = {
            req.switch_id
            for edge in model.edges.values()
            if "crossover" in edge.edge_id
            for req in edge.switch_requirements
        }
        assert crossover_switch_ids == {"SW1", "SW5"}


class TestSimpleLoop:
    """A minimal 3-tag loop for exercising the graph logic in isolation."""

    def _build(self) -> TrackModel:
        tags = [TagNode("A"), TagNode("B"), TagNode("C")]
        edges = [
            TrackEdge("E_AB", "A", "B"),
            TrackEdge("E_BC", "B", "C"),
            TrackEdge("E_CA", "C", "A"),
        ]
        trains = [TrainDescriptor("T1", hub_id=1, route=["A", "B", "C"])]
        return TrackModel(tags=tags, edges=edges, switches=[], trains=trains)

    def test_record_tag_event_sets_initial_position(self):
        model = self._build()
        result = model.record_tag_event("T1", "A", timestamp=1.0)
        assert result.current_tag == "A"
        assert result.previous_tag is None
        assert result.edge_completed is None
        assert model.train_position["T1"] == "A"

    def test_record_tag_event_detects_completed_edge(self):
        model = self._build()
        model.record_tag_event("T1", "A", timestamp=1.0)
        result = model.record_tag_event("T1", "B", timestamp=2.0)
        assert result.previous_tag == "A"
        assert result.edge_completed.edge_id == "E_AB"

    def test_record_tag_event_unknown_train_is_ignored(self):
        model = self._build()
        result = model.record_tag_event("GHOST", "A", timestamp=1.0)
        assert result.edge_completed is None
        assert "GHOST" not in model.train_position

    def test_record_tag_event_unknown_tag_is_ignored(self):
        model = self._build()
        result = model.record_tag_event("T1", "NOPE", timestamp=1.0)
        assert result.edge_completed is None
        assert "T1" not in model.train_position

    def test_next_edge_defaults_to_route_start_when_unpositioned(self):
        model = self._build()
        edge = model.next_edge_for_train("T1")
        assert edge.edge_id == "E_AB"

    def test_next_edge_wraps_cyclically_at_end_of_route(self):
        model = self._build()
        model.record_tag_event("T1", "C", timestamp=1.0)
        edge = model.next_edge_for_train("T1")
        assert edge.edge_id == "E_CA"

    def test_next_edge_for_unknown_train_is_none(self):
        model = self._build()
        assert model.next_edge_for_train("GHOST") is None

    def test_block_state_defaults_to_free(self):
        model = self._build()
        assert model.get_block_state("E_AB") == BlockState.FREE

    def test_set_and_get_block_state(self):
        model = self._build()
        model.set_block_state("E_AB", BlockState.OCCUPIED)
        assert model.get_block_state("E_AB") == BlockState.OCCUPIED

    def test_is_moving_false_until_explicitly_marked(self):
        model = self._build()
        model.record_tag_event("T1", "A", timestamp=1.0)
        assert model.is_moving("T1") is False
        model.mark_stopped("T1", False)
        assert model.is_moving("T1") is True

    def test_tag_id_for_uid_defaults_to_tag_id(self):
        model = self._build()
        assert model.tag_id_for_uid("A") == "A"
        assert model.tag_id_for_uid("nonexistent") is None

    def test_tag_id_for_uid_with_explicit_physical_uid(self):
        tags = [TagNode("A", uid="04AABBCC")]
        model = TrackModel(tags=tags, edges=[], switches=[], trains=[])
        assert model.tag_id_for_uid("04AABBCC") == "A"
        assert model.tag_id_for_uid("A") is None

    def test_seconds_since_last_tag(self):
        model = self._build()
        model.record_tag_event("T1", "A", timestamp=100.0)
        assert model.seconds_since_last_tag("T1", now=105.0) == 5.0

    def test_seconds_since_last_tag_unknown_train_is_zero(self):
        model = self._build()
        assert model.seconds_since_last_tag("GHOST", now=100.0) == 0.0
