"""Tests for battery voltage monitoring (Pico VSYS -> MQTT -> dispatcher)."""

import asyncio
import json
from typing import AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import MqttBridge, TagEvent
from dispatcher.track_model import TrackModel


class TestTagEventBatteryField:
    def test_accepts_no_battery_reading(self):
        event = TagEvent(train_id="TRN-A", tag_uid="T1", timestamp=1.0)
        assert event.battery_v is None

    def test_accepts_a_battery_reading(self):
        event = TagEvent(train_id="TRN-A", tag_uid="T1", timestamp=1.0, battery_v=3.85)
        assert event.battery_v == 3.85


class TestTrackModelBattery:
    def test_update_and_get_battery_round_trips(self):
        model = TrackModel()
        model.update_battery("TRN-A", 3.85)
        assert model.get_battery("TRN-A") == 3.85

    def test_get_battery_returns_none_for_unknown_train(self):
        model = TrackModel()
        assert model.get_battery("GHOST") is None

    def test_update_battery_overwrites_previous_reading(self):
        model = TrackModel()
        model.update_battery("TRN-A", 3.9)
        model.update_battery("TRN-A", 3.6)
        assert model.get_battery("TRN-A") == 3.6


# ---------------------------------------------------------------------------
# Dispatcher._handle_tag_event battery update
# ---------------------------------------------------------------------------


class FakeBridge:
    """Minimal MqttBridge stand-in driven directly by tests."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[TagEvent]" = asyncio.Queue()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def events(self) -> AsyncIterator[TagEvent]:
        while True:
            yield await self._queue.get()

    def publish_command(
        self, train_id: str, action: str, value: Optional[int] = None
    ) -> None:
        pass


TRN_A_HUB = "90:84:2B:18:28:36"


def build_model() -> TrackModel:
    model = TrackModel()
    model.configure_switch_wiring("A", hub_id=1, port_name="SWITCH_A")
    model.register_train("TRN-A", hub_id=TRN_A_HUB, route=["A", "H"])
    return model


def build_dispatcher() -> Dispatcher:
    model = build_model()
    block_manager = BlockManager(model)
    bridge = FakeBridge()
    train_controller = AsyncMock()
    switch_controller = AsyncMock()
    switch_controller.send_command_with_retry = AsyncMock(return_value=True)
    settings = Settings(
        dispatcher_watchdog_timeout=0.1,
        dispatcher_watchdog_check_interval=0.02,
        dispatcher_cruise_power=40,
    )
    return Dispatcher(
        model, block_manager, bridge, train_controller, switch_controller, settings
    )


class TestDispatcherBatteryUpdate:
    async def test_battery_is_recorded_when_present_on_the_event(self):
        dispatcher = build_dispatcher()

        await dispatcher._handle_tag_event(
            TagEvent(train_id="TRN-A", tag_uid="1", timestamp=1.0, battery_v=3.85)
        )

        assert dispatcher.track_model.get_battery("TRN-A") == 3.85

    async def test_battery_is_untouched_when_absent_from_the_event(self):
        dispatcher = build_dispatcher()

        await dispatcher._handle_tag_event(
            TagEvent(train_id="TRN-A", tag_uid="1", timestamp=1.0)
        )

        assert dispatcher.track_model.get_battery("TRN-A") is None

    async def test_later_reading_overwrites_earlier_one(self):
        dispatcher = build_dispatcher()

        await dispatcher._handle_tag_event(
            TagEvent(train_id="TRN-A", tag_uid="1", timestamp=1.0, battery_v=3.9)
        )
        await dispatcher._handle_tag_event(
            TagEvent(train_id="TRN-A", tag_uid="4", timestamp=2.0, battery_v=3.7)
        )

        assert dispatcher.track_model.get_battery("TRN-A") == 3.7


# ---------------------------------------------------------------------------
# MqttBridge._on_message battery_v parsing
# ---------------------------------------------------------------------------


def make_settings(**overrides) -> Settings:
    defaults = dict(
        mqtt_broker_host="localhost",
        mqtt_broker_port=1883,
        mqtt_keepalive=60,
        mqtt_client_id="test-dispatcher",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_message(payload: bytes, topic: str = "train/TRN-A/tag") -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = payload
    return msg


class TestMqttBridgeBatteryParsing:
    async def test_battery_v_is_parsed_when_present(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._loop = asyncio.get_running_loop()

            payload = json.dumps(
                {
                    "train_id": "TRN-A",
                    "tag_uid": "T1",
                    "timestamp": 123.0,
                    "battery_v": 3.72,
                }
            ).encode("utf-8")
            bridge._on_message(None, None, make_message(payload))
            await asyncio.sleep(0)

            event = await asyncio.wait_for(bridge._queue.get(), timeout=1)
            assert event.battery_v == 3.72

    async def test_battery_v_defaults_to_none_when_omitted(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._loop = asyncio.get_running_loop()

            payload = json.dumps(
                {"train_id": "TRN-A", "tag_uid": "T1", "timestamp": 123.0}
            ).encode("utf-8")
            bridge._on_message(None, None, make_message(payload))
            await asyncio.sleep(0)

            event = await asyncio.wait_for(bridge._queue.get(), timeout=1)
            assert event.battery_v is None
