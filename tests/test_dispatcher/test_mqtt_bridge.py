"""Tests for dispatcher.mqtt_bridge."""

import asyncio
import json
from unittest.mock import MagicMock, patch

from config import Settings
from dispatcher.mqtt_bridge import MqttBridge, TagEvent


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


class TestOnMessage:
    async def test_valid_message_is_delivered_via_events(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._loop = asyncio.get_running_loop()

            payload = json.dumps(
                {"train_id": "TRN-A", "tag_uid": "T1", "timestamp": 123.0}
            ).encode("utf-8")
            bridge._on_message(None, None, make_message(payload))
            await asyncio.sleep(0)  # let call_soon_threadsafe run

            event = await asyncio.wait_for(bridge._queue.get(), timeout=1)
            assert event == TagEvent(train_id="TRN-A", tag_uid="T1", timestamp=123.0)

    async def test_malformed_json_is_dropped(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._loop = asyncio.get_running_loop()

            bridge._on_message(None, None, make_message(b"not json"))
            await asyncio.sleep(0)

            assert bridge._queue.empty()

    async def test_missing_required_field_is_dropped(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._loop = asyncio.get_running_loop()

            payload = json.dumps({"train_id": "TRN-A"}).encode("utf-8")
            bridge._on_message(None, None, make_message(payload))
            await asyncio.sleep(0)

            assert bridge._queue.empty()

    def test_message_before_start_is_dropped(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())  # start() never called; _loop is None

            payload = json.dumps(
                {"train_id": "TRN-A", "tag_uid": "T1", "timestamp": 1.0}
            ).encode("utf-8")
            bridge._on_message(None, None, make_message(payload))

            assert bridge._queue.empty()


class TestQueueBackpressure:
    async def test_queue_full_drops_newest_without_raising(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            bridge = MqttBridge(make_settings())
            bridge._queue = asyncio.Queue(maxsize=1)

            event1 = TagEvent("TRN-A", "T1", 1.0)
            event2 = TagEvent("TRN-A", "T2", 2.0)

            bridge._enqueue(event1)
            bridge._enqueue(event2)  # queue full; must not raise

            assert bridge._queue.qsize() == 1
            assert await bridge._queue.get() == event1


class TestPublishCommand:
    def test_builds_correct_topic_and_payload(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance
            bridge = MqttBridge(make_settings())

            bridge.publish_command("TRN-A", "stop")

            topic, payload = mock_instance.publish.call_args[0]
            assert topic == "train/TRN-A/command"
            assert json.loads(payload) == {"action": "stop"}

    def test_includes_value_when_given(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance
            bridge = MqttBridge(make_settings())

            bridge.publish_command("TRN-A", "set_speed", value=50)

            _, payload = mock_instance.publish.call_args[0]
            assert json.loads(payload) == {"action": "set_speed", "value": 50}


class TestUsernameAuth:
    def test_username_pw_set_called_when_configured(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            MqttBridge(make_settings(mqtt_username="user", mqtt_password="pass"))

            mock_instance.username_pw_set.assert_called_once_with("user", "pass")

    def test_username_pw_set_not_called_when_absent(self):
        with patch("dispatcher.mqtt_bridge.mqtt.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            MqttBridge(make_settings())

            mock_instance.username_pw_set.assert_not_called()
