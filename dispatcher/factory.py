"""Factory for wiring up a Dispatcher from application settings."""

from __future__ import annotations

from typing import Optional

from config import Settings, get_settings
from controllers.switch_controller import SwitchController
from controllers.train_controller import TrainController
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import MqttBridge
from dispatcher.track_model import build_sample_topology


def build_dispatcher(
    train_controller: TrainController,
    switch_controller: SwitchController,
    settings: Optional[Settings] = None,
) -> Dispatcher:
    """Build a Dispatcher wired to the given BLE controllers via MQTT."""
    settings = settings or get_settings()
    track_model = build_sample_topology()
    block_manager = BlockManager(track_model)
    bridge = MqttBridge(settings)
    return Dispatcher(
        track_model=track_model,
        block_manager=block_manager,
        bridge=bridge,
        train_controller=train_controller,
        switch_controller=switch_controller,
        settings=settings,
    )
