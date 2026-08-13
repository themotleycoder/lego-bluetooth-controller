"""
MQTT bridge between the RFID-equipped trains and the dispatcher.

Subscribes to tag-read events published by each train's Pico 2 W firmware and
publishes command messages back. paho-mqtt's client runs its network I/O on
its own background thread (`loop_start()`), while the rest of the dispatcher
is asyncio-based; incoming messages are handed from that thread to the
dispatcher's event loop via `call_soon_threadsafe` into an `asyncio.Queue`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import paho.mqtt.client as mqtt

from config import Settings
from utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TagEvent:
    """A single RFID tag read reported by a train."""

    train_id: str
    tag_uid: str
    timestamp: float
    # LiPo voltage from the Pico's VSYS ADC, piggybacked on the tag payload.
    # Optional/defaults to None for backward compat with older firmware
    # that doesn't report it yet.
    battery_v: Optional[float] = None


class MqttBridge:
    """Async-friendly wrapper around a paho-mqtt client."""

    def __init__(self, settings: Settings) -> None:
        """Configure (but do not connect) the underlying MQTT client."""
        self._settings = settings
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[TagEvent]" = asyncio.Queue(maxsize=1000)

        self._client = mqtt.Client(client_id=settings.mqtt_client_id)
        if settings.mqtt_username:
            self._client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    async def start(self) -> None:
        """Connect to the broker and start listening for tag events."""
        self._loop = asyncio.get_running_loop()
        self._client.connect(
            self._settings.mqtt_broker_host,
            self._settings.mqtt_broker_port,
            self._settings.mqtt_keepalive,
        )
        self._client.loop_start()

        tag_wildcard = self._settings.mqtt_tag_topic_template.format(train_id="+")
        status_wildcard = self._settings.mqtt_status_topic_template.format(train_id="+")
        self._client.subscribe(tag_wildcard)
        self._client.subscribe(status_wildcard)
        logger.info(
            f"MQTT bridge connected to {self._settings.mqtt_broker_host}:"
            f"{self._settings.mqtt_broker_port}, subscribed to {tag_wildcard} "
            f"and {status_wildcard}"
        )

    async def stop(self) -> None:
        """Disconnect from the broker and stop the network thread."""
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT bridge disconnected")

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        if rc == 0:
            logger.info("MQTT client connected successfully")
        else:
            logger.error(f"MQTT client failed to connect, rc={rc}")

    def _on_disconnect(self, client: Any, userdata: Any, rc: int) -> None:
        if rc != 0:
            logger.warning(f"MQTT client disconnected unexpectedly, rc={rc}")

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """
        Runs on paho's network thread. Parses and hands off to asyncio.

        Handles both tag-read messages (train/<id>/tag, carry tag_uid) and
        status pings (train/<id>/status, e.g. "ready"/"reconnected", no
        tag_uid) with the same TagEvent shape -- tag_uid defaults to "" for
        a status-only message, which Dispatcher._handle_tag_event treats as
        "no sensor to resolve, but still record battery_v if present".
        """
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            event = TagEvent(
                train_id=str(payload["train_id"]),
                tag_uid=str(payload.get("tag_uid", "")),
                timestamp=float(payload["timestamp"]),
                battery_v=payload.get("battery_v"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Dropping malformed message on {msg.topic}: {e}")
            return

        if self._loop is None:
            logger.warning("MQTT message received before bridge start(); dropping")
            return

        self._loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: TagEvent) -> None:
        """Runs on the asyncio loop, scheduled via call_soon_threadsafe."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                f"Tag event queue full, dropping event for train {event.train_id}"
            )

    async def events(self) -> AsyncIterator[TagEvent]:
        """Yield tag events as they arrive, forever."""
        while True:
            yield await self._queue.get()

    def publish_command(
        self, train_id: str, action: str, value: Optional[int] = None
    ) -> None:
        """
        Publish a command message to a train.

        Reserved for future direct Pico-side motor control; the Pico
        firmware currently only logs received commands, so the dispatcher
        does not call this yet — real actuation goes through the existing
        BLE-based TrainController/SwitchController instead.
        """
        topic = self._settings.mqtt_command_topic_template.format(train_id=train_id)
        payload: dict = {"action": action}
        if value is not None:
            payload["value"] = value
        self._client.publish(topic, json.dumps(payload))
