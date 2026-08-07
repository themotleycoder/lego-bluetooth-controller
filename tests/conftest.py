"""
Pytest configuration and shared fixtures for LEGO Train Controller tests.

This module provides common fixtures and configuration for all test modules.
"""

import asyncio
import os
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# Set test environment before importing app
os.environ["REQUIRE_AUTH"] = "true"
os.environ["API_KEYS"] = "test-api-key-12345"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:8080"
os.environ["LOG_LEVEL"] = "DEBUG"
os.environ["BLUETOOTH_RESET_ON_STARTUP"] = "false"
os.environ["DISPATCHER_ENABLED"] = "false"


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def test_api_key() -> str:
    """Test API key fixture."""
    return "test-api-key-12345"


@pytest.fixture
def invalid_api_key() -> str:
    """Invalid API key fixture."""
    return "invalid-key-67890"


@pytest.fixture
def mock_bluetooth_scanner():
    """Mock BLE scanner to avoid actual Bluetooth operations."""
    with patch("servers.bluetooth_scanner.BetterBleScanner") as mock:
        mock_instance = MagicMock()
        mock_instance.start_scan = AsyncMock()
        mock_instance.stop_scan = AsyncMock()
        mock_instance.reset_bluetooth = AsyncMock()
        mock_instance.is_scanning = False
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_lego_controller():
    """Mock LegoController to avoid Bluetooth initialization."""
    with patch("webservice.train_service.LegoController") as mock:
        mock_instance = MagicMock()
        mock_instance.initialize = AsyncMock()
        mock_instance.running = True

        # Mock train controller
        mock_instance.train_controller = MagicMock()
        mock_instance.train_controller.handle_command = AsyncMock()
        mock_instance.train_controller.get_connected_trains = MagicMock(return_value={})
        mock_instance.train_controller.start_status_monitoring = AsyncMock()
        mock_instance.train_controller.stop_status_monitoring = AsyncMock()
        mock_instance.train_controller.reset_bluetooth = AsyncMock()

        # Mock switch controller
        mock_instance.switch_controller = MagicMock()
        mock_instance.switch_controller.send_command_with_retry = AsyncMock(
            return_value=True
        )
        mock_instance.switch_controller.get_connected_switches = MagicMock(
            return_value={}
        )
        mock_instance.switch_controller.start_status_monitoring = AsyncMock()
        mock_instance.switch_controller.scanner = MagicMock()
        mock_instance.switch_controller.scanner.reset_bluetooth = AsyncMock()

        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def client(mock_lego_controller) -> TestClient:
    """
    Create a test client for the FastAPI app.

    Uses TestClient for synchronous tests.
    """
    from webservice.train_service import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(mock_lego_controller) -> AsyncGenerator:
    """
    Create an async test client for the FastAPI app.

    Uses AsyncClient for asynchronous tests.
    """
    from webservice.train_service import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
def sample_train_status():
    """Sample train status data for testing."""
    return {
        "90:84:2B:18:28:36": {
            "connected": True,
            "state": "connected",
            "name": "Train Hub",
            "rssi": -60,
            "last_update_seconds_ago": 0.5,
            "last_command_time": 1700000000.0,
            "active": True,
        },
        "F3:33:66:0C:3A:6A": {
            "connected": False,
            "state": "disconnected",
            "name": "Train Hub",
            "rssi": -55,
            "last_update_seconds_ago": 1.2,
            "last_command_time": None,
            "active": False,
        },
    }


@pytest.fixture
def sample_switch_status():
    """Sample switch status data for testing."""
    return {
        1: {
            "switch_positions": {
                "SWITCH_A": 0,  # STRAIGHT
                "SWITCH_B": 1,  # DIVERGING
                "SWITCH_C": 0,
                "SWITCH_D": 1,
            },
            "switch_states": {
                "SWITCH_A": 1,  # Connected
                "SWITCH_B": 1,
                "SWITCH_C": 0,  # Disconnected
                "SWITCH_D": 1,
            },
            "last_update_seconds_ago": 0.8,
            "name": "Technic Hub",
            "status": 5,
            "connected": True,
            "rssi": -58,
            "reliability": {},
        }
    }
