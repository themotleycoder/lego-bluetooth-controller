"""
Configuration management for LEGO Train Controller service.

Uses Pydantic Settings for type-safe environment variable loading.
All configuration can be overridden via environment variables or .env file.
"""

import os
from typing import Dict, List, Optional, Tuple
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server Configuration
    host: str = Field(default="0.0.0.0", description="Server host address")
    port: int = Field(default=8000, description="Server port")

    # Security Configuration
    api_keys: str = Field(
        default="", description="Comma-separated list of valid API keys"
    )
    allowed_origins: str = Field(
        default="*", description="Comma-separated list of allowed CORS origins"
    )

    @property
    def api_keys_list(self) -> List[str]:
        """Parse API keys as list."""
        if not self.api_keys:
            return []
        return [key.strip() for key in self.api_keys.split(",") if key.strip()]

    @property
    def allowed_origins_list(self) -> List[str]:
        """Parse allowed origins as list."""
        if not self.allowed_origins:
            return ["*"]
        return [
            origin.strip()
            for origin in self.allowed_origins.split(",")
            if origin.strip()
        ]

    require_auth: bool = Field(
        default=True, description="Require API key authentication for all endpoints"
    )

    # Logging Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    log_format: str = Field(default="json", description="Log format (json or text)")
    log_file: Optional[str] = Field(
        default=None, description="Log file path (None for stdout only)"
    )

    # Bluetooth Configuration
    bluetooth_reset_on_startup: bool = Field(
        default=True, description="Reset Bluetooth adapter on service startup"
    )
    max_train_connections: int = Field(
        default=10, description="Maximum number of simultaneous train connections"
    )
    max_switch_connections: int = Field(
        default=10, description="Maximum number of simultaneous switch connections"
    )

    # LEGO Hub Constants
    lego_service_uuid: str = Field(
        default="00001623-1212-efde-1623-785feabcd123",
        description="LEGO Hub BLE service UUID",
    )
    lego_char_uuid: str = Field(
        default="00001624-1212-efde-1623-785feabcd123",
        description="LEGO Hub BLE characteristic UUID",
    )
    lego_manufacturer_id: int = Field(
        default=919, description="LEGO manufacturer ID for BLE advertising"
    )

    # Timing Configuration
    status_update_interval: float = Field(
        default=0.1, description="Status polling interval in seconds for active devices"
    )
    inactive_device_threshold: float = Field(
        default=5.0, description="Time in seconds before marking device as inactive"
    )
    command_retry_delay: float = Field(
        default=0.5, description="Base delay in seconds between command retries"
    )
    max_command_retries: int = Field(
        default=3, description="Maximum number of command retry attempts"
    )

    # Validation Ranges
    power_min: int = Field(default=-100, description="Minimum train power value")
    power_max: int = Field(default=100, description="Maximum train power value")
    valid_switch_names: str = Field(
        default="A,B,C,D", description="Valid switch name letters (comma-separated)"
    )
    valid_switch_positions: str = Field(
        default="STRAIGHT,DIVERGING",
        description="Valid switch positions (comma-separated)",
    )

    @property
    def valid_switch_names_list(self) -> List[str]:
        """Parse valid switch names as list."""
        return [
            name.strip() for name in self.valid_switch_names.split(",") if name.strip()
        ]

    @property
    def valid_switch_positions_list(self) -> List[str]:
        """Parse valid switch positions as list."""
        return [
            pos.strip() for pos in self.valid_switch_positions.split(",") if pos.strip()
        ]

    # MQTT Configuration (RFID dispatcher)
    mqtt_broker_host: str = Field(
        default="localhost", description="MQTT broker hostname or IP address"
    )
    mqtt_broker_port: int = Field(default=1883, description="MQTT broker port")
    mqtt_keepalive: int = Field(
        default=60, description="MQTT connection keepalive interval in seconds"
    )
    mqtt_client_id: str = Field(
        default="lego-dispatcher", description="MQTT client ID for the dispatcher"
    )
    mqtt_username: Optional[str] = Field(
        default=None, description="MQTT broker username (omit to disable MQTT auth)"
    )
    mqtt_password: Optional[str] = Field(
        default=None, description="MQTT broker password (omit to disable MQTT auth)"
    )
    mqtt_tag_topic_template: str = Field(
        default="train/{train_id}/tag",
        description="MQTT topic template trains publish RFID tag reads to",
    )
    mqtt_command_topic_template: str = Field(
        default="train/{train_id}/command",
        description="MQTT topic template the dispatcher publishes commands to",
    )

    # Dispatcher Configuration
    dispatcher_enabled: bool = Field(
        default=False,
        description="Enable the RFID/MQTT dispatcher background task on startup",
    )
    dispatcher_watchdog_timeout: float = Field(
        default=10.0,
        description="Seconds a moving train may go without a tag read before failsafe triggers",
    )
    dispatcher_watchdog_check_interval: float = Field(
        default=1.0,
        description="How often the dispatcher watchdog checks train timeouts",
    )
    dispatcher_cruise_power: int = Field(
        default=40, description="Default motor power the dispatcher resumes trains at"
    )
    train_hub_mapping: str = Field(
        default="",
        description=(
            "Comma-separated train_id=hub_address pairs (hub_address is the "
            "train hub's BLE address, since colons in the address rule out "
            "':' as the pair delimiter), e.g. "
            "'TRN-A=90:84:2B:18:28:36,TRN-B=F3:33:66:0C:3A:6A'"
        ),
    )

    @property
    def train_hub_mapping_dict(self) -> Dict[str, str]:
        """Parse train_hub_mapping into a dict of train_id -> BLE hub address."""
        mapping: Dict[str, str] = {}
        for pair in self.train_hub_mapping.split(","):
            pair = pair.strip()
            if not pair:
                continue
            train_id, _, hub_address = pair.partition("=")
            mapping[train_id.strip()] = hub_address.strip()
        return mapping

    train_routes: str = Field(
        default="",
        description=(
            "Semicolon-separated train_id:route pairs, route is a "
            "'-'-joined cyclic list of switch ids, e.g. 'TRN-A:C-A-H-F-E-D;TRN-B:B-J-I-F-E'"
        ),
    )

    @property
    def train_routes_dict(self) -> Dict[str, List[str]]:
        """Parse train_routes into a dict of train_id -> ordered list of switch ids."""
        routes: Dict[str, List[str]] = {}
        for pair in self.train_routes.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            train_id, _, route = pair.partition(":")
            switch_ids = [s.strip() for s in route.split("-") if s.strip()]
            if switch_ids:
                routes[train_id.strip()] = switch_ids
        return routes

    switch_wiring: str = Field(
        default="",
        description=(
            "Comma-separated switch_id:hub_id:port_name triples for motorized "
            "switches, e.g. 'A:11:SWITCH_A,B:11:SWITCH_B'"
        ),
    )

    @property
    def switch_wiring_dict(self) -> Dict[str, Tuple[int, str]]:
        """Parse switch_wiring into a dict of switch_id -> (hub_id, port_name)."""
        wiring: Dict[str, Tuple[int, str]] = {}
        for triple in self.switch_wiring.split(","):
            triple = triple.strip()
            if not triple:
                continue
            switch_id, _, rest = triple.partition(":")
            hub_id, _, port_name = rest.partition(":")
            wiring[switch_id.strip()] = (int(hub_id.strip()), port_name.strip())
        return wiring

    sensor_uid_mapping: str = Field(
        default="",
        description=(
            "Comma-separated sensor_id:uid pairs mapping logical sensor ids to "
            "physical RFID tag UIDs, e.g. '1:04AABBCC,2:04CCDDEE'"
        ),
    )

    @property
    def sensor_uid_mapping_dict(self) -> Dict[int, str]:
        """Parse sensor_uid_mapping into a dict of sensor_id -> physical UID."""
        mapping: Dict[int, str] = {}
        for pair in self.sensor_uid_mapping.split(","):
            pair = pair.strip()
            if not pair:
                continue
            sensor_id, _, uid = pair.partition(":")
            mapping[int(sensor_id.strip())] = uid.strip()
        return mapping

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """
    Get the global settings instance.

    Returns:
        Settings: The application settings
    """
    return settings
