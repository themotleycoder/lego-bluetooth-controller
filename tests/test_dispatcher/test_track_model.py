"""Tests for dispatcher.track_model."""

import pytest

from dispatcher.track_model import SwitchPort, SwitchType, TrackModel


class TestTopologyShape:
    def test_topology_shape(self):
        model = TrackModel()
        assert len(model.switches) == 10
        assert len(model.sensors) == 9
        assert len(model.edges) == 15
        assert len(model.blocks) == 15

    def test_motorized_and_manual_switch_counts(self):
        model = TrackModel()
        motorized = [
            s for s in model.switches.values() if s.switch_type == SwitchType.MOTORIZED
        ]
        manual = [
            s for s in model.switches.values() if s.switch_type == SwitchType.MANUAL
        ]
        assert len(motorized) == 7
        assert len(manual) == 3

    def test_find_route_between_switches(self):
        model = TrackModel()
        route = model.find_route("A", "H")
        assert [e.id for e in route] == ["AH"]

    def test_find_route_same_switch_is_empty(self):
        model = TrackModel()
        assert model.find_route("A", "A") == []

    def test_find_route_unknown_switch_is_none(self):
        model = TrackModel()
        assert model.find_route("A", "NOPE") is None

    def test_sensor_on_edge(self):
        model = TrackModel()
        edge = model.sensor_on_edge(4)
        assert edge.id == "FG"

    def test_sensor_on_edge_unknown_sensor_is_none(self):
        model = TrackModel()
        assert model.sensor_on_edge(999) is None

    def test_switch_required_position(self):
        model = TrackModel()
        assert model.switch_required_position("A", SwitchPort.TRUNK) is None
        assert model.switch_required_position("A", SwitchPort.STRAIGHT) is False
        assert model.switch_required_position("A", SwitchPort.DIVERGE) is True

    def test_route_switch_settings_dedupes_and_skips_trunk(self):
        model = TrackModel()
        route = model.find_route("C", "G")  # CF (C.trunk-F.div), FG (F.trunk-G.str)
        settings = model.route_switch_settings(route)
        assert ("F", True) in settings
        assert ("G", False) in settings
        assert not any(sw == "C" for sw, _ in settings)

    def test_route_blocks_and_sensors(self):
        model = TrackModel()
        route = model.find_route("C", "G")  # CF (no sensor), FG (sensor 4)
        assert model.route_blocks(route) == ["BLK_CF", "BLK_FG"]
        assert model.route_sensors(route) == [4]


class TestSwitchAndSensorWiring:
    def test_configure_switch_wiring(self):
        model = TrackModel()
        model.configure_switch_wiring("F", hub_id=11, port_name="SWITCH_A")
        assert model.switches["F"].hub_id == 11
        assert model.switches["F"].port_name == "SWITCH_A"

    def test_configure_switch_wiring_unknown_switch_raises(self):
        model = TrackModel()
        with pytest.raises(KeyError):
            model.configure_switch_wiring("Z", hub_id=1, port_name="SWITCH_A")

    def test_configure_sensor_uid(self):
        model = TrackModel()
        model.configure_sensor_uid(4, "04AABBCC")
        assert model.sensors[4].tag_uid == "04AABBCC"

    def test_configure_sensor_uid_unknown_sensor_raises(self):
        model = TrackModel()
        with pytest.raises(KeyError):
            model.configure_sensor_uid(999, "04AABBCC")

    def test_sensor_id_for_uid_defaults_to_identity(self):
        model = TrackModel()
        assert model.sensor_id_for_uid("4") == 4
        assert model.sensor_id_for_uid("nonexistent") is None

    def test_sensor_id_for_uid_with_explicit_physical_uid(self):
        model = TrackModel()
        model.configure_sensor_uid(4, "04AABBCC")
        assert model.sensor_id_for_uid("04AABBCC") == 4
        assert model.sensor_id_for_uid("4") is None


class TestTrainRegistration:
    def _build(self) -> TrackModel:
        model = TrackModel()
        model.register_train(
            "T1", hub_id="90:84:2B:18:28:36", route=["A", "C", "F", "G", "H"]
        )
        return model

    def test_register_train_sets_initial_state(self):
        model = self._build()
        assert model.train_position["T1"] == "A"
        assert model.is_moving("T1") is False

    def test_register_train_empty_route_raises(self):
        model = TrackModel()
        with pytest.raises(ValueError):
            model.register_train("T1", hub_id="90:84:2B:18:28:36", route=[])

    def test_register_train_unknown_switch_raises(self):
        model = TrackModel()
        with pytest.raises(ValueError):
            model.register_train("T1", hub_id="90:84:2B:18:28:36", route=["A", "NOPE"])

    def test_mark_stopped_and_is_moving(self):
        model = self._build()
        model.mark_stopped("T1", False)
        assert model.is_moving("T1") is True
        model.mark_stopped("T1", True)
        assert model.is_moving("T1") is False

    def test_self_drive_defaults_off_and_is_toggleable(self):
        model = self._build()
        assert model.is_self_drive("T1") is False
        model.set_self_drive("T1", True)
        assert model.is_self_drive("T1") is True
        model.set_self_drive("T1", False)
        assert model.is_self_drive("T1") is False

    def test_train_id_for_hub_id(self):
        model = self._build()
        assert model.train_id_for_hub_id("90:84:2B:18:28:36") == "T1"
        assert model.train_id_for_hub_id("nonexistent") is None


class TestChainAdvancement:
    def _build(self) -> TrackModel:
        model = TrackModel()
        model.register_train(
            "T1", hub_id="90:84:2B:18:28:36", route=["A", "C", "F", "G", "H"]
        )
        return model

    def test_next_block_chain_stops_at_first_sensored_edge(self):
        model = self._build()
        chain = model.next_block_chain_for_train("T1")
        assert [e.id for e in chain] == ["AC", "CF", "FG"]

    def test_next_block_chain_spans_multiple_sensorless_edges(self):
        model = self._build()
        model.grant_pending_chain("T1", model.next_block_chain_for_train("T1"))
        model.record_tag_event("T1", 4, timestamp=1.0)  # completes AC,CF,FG -> pos G
        chain = model.next_block_chain_for_train("T1")
        assert [e.id for e in chain] == ["GH", "AH"]

    def test_next_block_chain_for_unknown_train_is_none(self):
        model = self._build()
        assert model.next_block_chain_for_train("GHOST") is None

    def test_record_tag_event_confirms_pending_chain(self):
        model = self._build()
        model.grant_pending_chain("T1", model.next_block_chain_for_train("T1"))
        result = model.record_tag_event("T1", 4, timestamp=1.0)
        assert result.previous_position == "A"
        assert result.current_position == "G"
        assert [e.id for e in result.edges_completed] == ["AC", "CF", "FG"]
        assert model.train_position["T1"] == "G"

    def test_record_tag_event_for_sensor_not_pending_is_ignored(self):
        model = self._build()
        model.grant_pending_chain("T1", model.next_block_chain_for_train("T1"))
        result = model.record_tag_event(
            "T1", 6, timestamp=1.0
        )  # AH's sensor, not pending yet
        assert result.edges_completed == []
        assert model.train_position["T1"] == "A"

    def test_record_tag_event_for_unregistered_train_is_ignored(self):
        model = TrackModel()
        result = model.record_tag_event("GHOST", 4, timestamp=1.0)
        assert result.edges_completed == []
        assert result.current_position is None

    def test_record_tag_event_updates_last_tag_time_even_when_ignored(self):
        model = self._build()
        model.record_tag_event("T1", 999, timestamp=5.0)
        assert model.seconds_since_last_tag("T1", now=10.0) == 5.0


class TestHopsToSwitch:
    def test_hops_to_switch(self):
        model = TrackModel()
        model.register_train(
            "T1", hub_id="90:84:2B:18:28:36", route=["A", "C", "F", "G", "H"]
        )
        assert model.hops_to_switch("T1", "A") == 0
        assert model.hops_to_switch("T1", "C") == 1
        assert model.hops_to_switch("T1", "H") == 4

    def test_hops_to_switch_unregistered_train_is_zero(self):
        model = TrackModel()
        assert model.hops_to_switch("GHOST", "A") == 0


class TestBlockOccupancy:
    def test_block_starts_free(self):
        model = TrackModel()
        assert model.is_block_free("BLK_AH") is True

    def test_occupy_and_free_block(self):
        model = TrackModel()
        model.occupy_block("BLK_AH", "T1")
        assert model.is_block_free("BLK_AH") is False
        assert model.blocks["BLK_AH"].occupied_by == "T1"
        model.free_block("BLK_AH")
        assert model.is_block_free("BLK_AH") is True


class TestSecondsSinceLastTag:
    def test_seconds_since_last_tag(self):
        model = TrackModel()
        model.mark_tag_seen("T1", timestamp=100.0)
        assert model.seconds_since_last_tag("T1", now=105.0) == 5.0

    def test_seconds_since_last_tag_unknown_train_is_zero(self):
        model = TrackModel()
        assert model.seconds_since_last_tag("GHOST", now=100.0) == 0.0
