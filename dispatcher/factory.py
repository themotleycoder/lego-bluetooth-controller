"""Factory for wiring up a Dispatcher from application settings."""

from __future__ import annotations

from typing import Optional

from config import Settings, get_settings
from controllers.switch_controller import SwitchController
from controllers.train_controller import TrainController
from dispatcher.block_manager import BlockManager
from dispatcher.dispatcher import Dispatcher
from dispatcher.mqtt_bridge import MqttBridge
from dispatcher.track_model import TrackModel
from utils.logging_config import get_logger

logger = get_logger(__name__)


def build_dispatcher(
    train_controller: TrainController,
    switch_controller: SwitchController,
    settings: Optional[Settings] = None,
) -> Dispatcher:
    """Build a Dispatcher wired to the given BLE controllers via MQTT."""
    settings = settings or get_settings()
    track_model = TrackModel()

    for switch_id, (hub_id, port_name) in settings.switch_wiring_dict.items():
        track_model.configure_switch_wiring(switch_id, hub_id, port_name)
    for sensor_id, uid in settings.sensor_uid_mapping_dict.items():
        track_model.configure_sensor_uid(sensor_id, uid)

    routes = settings.train_routes_dict
    for train_id, hub_id in settings.train_hub_mapping_dict.items():
        route = routes.get(train_id)
        if route:
            track_model.register_train(train_id, hub_id, route)
        else:
            logger.warning(
                f"Train {train_id} has a hub mapping but no configured route "
                "(train_routes); it will never be dispatched"
            )

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
