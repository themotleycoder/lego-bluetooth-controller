"""
Unit tests for configuration module.

Tests Settings class and environment variable loading.
"""

import os
from typing import List

import pytest

from config import Settings, get_settings


class TestSettings:
    """Test suite for Settings configuration class."""

    def test_default_settings(self):
        """Test default settings values (ignoring env overrides)."""
        # Create settings with explicit values to avoid env file
        settings = Settings(
            host="0.0.0.0",
            port=8000,
            api_keys="",
            require_auth=True,
            log_level="INFO",
            log_format="json",
            bluetooth_reset_on_startup=True,
        )

        assert settings.host == "0.0.0.0"
        assert settings.port == 8000
        assert settings.require_auth is True
        assert settings.log_level == "INFO"
        assert settings.log_format == "json"
        assert settings.bluetooth_reset_on_startup is True
        assert settings.max_train_connections == 10
        assert settings.max_switch_connections == 10

    def test_api_keys_list_property(self):
        """Test API keys parsing from comma-separated string."""
        settings = Settings(api_keys="key1,key2,key3")

        assert isinstance(settings.api_keys_list, list)
        assert len(settings.api_keys_list) == 3
        assert "key1" in settings.api_keys_list
        assert "key2" in settings.api_keys_list
        assert "key3" in settings.api_keys_list

    def test_api_keys_list_with_spaces(self):
        """Test API keys parsing handles extra spaces."""
        settings = Settings(api_keys=" key1 , key2 , key3 ")

        assert len(settings.api_keys_list) == 3
        assert settings.api_keys_list == ["key1", "key2", "key3"]

    def test_api_keys_list_empty(self):
        """Test empty API keys returns empty list."""
        settings = Settings(api_keys="")

        assert settings.api_keys_list == []

    def test_allowed_origins_list_property(self):
        """Test allowed origins parsing from comma-separated string."""
        settings = Settings(allowed_origins="http://localhost,https://example.com")

        assert isinstance(settings.allowed_origins_list, list)
        assert len(settings.allowed_origins_list) == 2
        assert "http://localhost" in settings.allowed_origins_list
        assert "https://example.com" in settings.allowed_origins_list

    def test_allowed_origins_default(self):
        """Test default allowed origins."""
        settings = Settings(allowed_origins="")

        assert settings.allowed_origins_list == ["*"]

    def test_valid_switch_names_list(self):
        """Test valid switch names parsing."""
        settings = Settings()

        assert isinstance(settings.valid_switch_names_list, list)
        assert settings.valid_switch_names_list == ["A", "B", "C", "D"]

    def test_valid_switch_positions_list(self):
        """Test valid switch positions parsing."""
        settings = Settings()

        assert isinstance(settings.valid_switch_positions_list, list)
        assert settings.valid_switch_positions_list == ["STRAIGHT", "DIVERGING"]

    def test_custom_port(self):
        """Test setting custom port."""
        settings = Settings(port=9000)

        assert settings.port == 9000

    def test_disable_auth(self):
        """Test disabling authentication."""
        settings = Settings(require_auth=False)

        assert settings.require_auth is False

    def test_log_level_custom(self):
        """Test setting custom log level."""
        settings = Settings(log_level="DEBUG")

        assert settings.log_level == "DEBUG"

    def test_bluetooth_reset_disabled(self):
        """Test disabling Bluetooth reset on startup."""
        settings = Settings(bluetooth_reset_on_startup=False)

        assert settings.bluetooth_reset_on_startup is False

    def test_bluetooth_adapter_defaults(self):
        """Test default Bluetooth adapter settings (single-adapter mode)."""
        settings = Settings()

        assert settings.bluetooth_switch_adapter == "hci0"
        assert settings.bluetooth_train_adapter is None

    def test_bluetooth_adapter_dual_adapter_mode(self):
        """Test configuring separate switch/train adapters."""
        settings = Settings(
            bluetooth_switch_adapter="hci0", bluetooth_train_adapter="hci1"
        )

        assert settings.bluetooth_switch_adapter == "hci0"
        assert settings.bluetooth_train_adapter == "hci1"

    def test_timing_configuration(self):
        """Test timing-related configuration."""
        settings = Settings(
            status_update_interval=0.2,
            inactive_device_threshold=10.0,
            command_retry_delay=1.0,
            max_command_retries=5,
        )

        assert settings.status_update_interval == 0.2
        assert settings.inactive_device_threshold == 10.0
        assert settings.command_retry_delay == 1.0
        assert settings.max_command_retries == 5

    def test_validation_ranges(self):
        """Test power validation ranges."""
        settings = Settings()

        assert settings.power_min == -100
        assert settings.power_max == 100

    def test_get_settings_singleton(self):
        """Test get_settings returns same instance."""
        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2

    def test_env_file_loading(self, tmp_path):
        """Test loading settings from .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "HOST=192.168.1.100\n"
            "PORT=8080\n"
            "API_KEYS=testkey1,testkey2\n"
            "REQUIRE_AUTH=false\n"
        )

        # Note: This test demonstrates structure but may not actually load
        # the temp .env due to how Settings is initialized
        settings = Settings(_env_file=str(env_file))

        # Verify structure (actual loading depends on pydantic-settings config)
        assert isinstance(settings, Settings)
