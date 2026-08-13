"""
Unit tests for TrainController adapter selection and scan-mode behavior.

Covers passing a BlueZ adapter (e.g. "hci1") through to BleakClient
connections, and the controller running its own discovery scan when a
dedicated adapter is configured (dual-adapter deployments) versus relying
on SwitchController's scan (single-adapter/piggyback mode).
"""

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

from controllers.train_controller import TrainController

TRAIN_ADDRESS = "AA:BB:CC:DD:EE:FF"


class TestTrainControllerAdapter:
    """Test suite for adapter-aware connecting and scanning."""

    def test_default_adapter_is_none(self):
        """Test the default constructor preserves single-adapter (piggyback) behavior."""
        controller = TrainController()

        assert controller._adapter is None
        assert controller.scanner.adapter is None

    def test_adapter_passed_to_scanner(self):
        """Test a configured adapter is stored and forwarded to the scanner."""
        controller = TrainController(adapter="hci1")

        assert controller._adapter == "hci1"
        assert controller.scanner.adapter == "hci1"

    async def test_connect_hub_passes_adapter_to_bleakclient(self):
        """Test _connect_hub forwards the configured adapter to BleakClient."""
        controller = TrainController(adapter="hci1", known_addresses=[TRAIN_ADDRESS])

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.start_notify = AsyncMock()
        mock_client.write_gatt_char = AsyncMock()

        device = MagicMock()
        device.address = TRAIN_ADDRESS

        with patch(
            "controllers.train_controller.BleakClient", return_value=mock_client
        ) as mock_client_cls:
            await controller._connect_hub(TRAIN_ADDRESS, device)

        _, kwargs = mock_client_cls.call_args
        assert kwargs["adapter"] == "hci1"
        assert controller._connection_state[TRAIN_ADDRESS] == "connected"

    async def test_start_status_monitoring_starts_own_scan_when_adapter_set(self):
        """Test the controller runs its own scan when constructed with an adapter."""
        controller = TrainController(adapter="hci1")
        controller.scanner.start_scan = AsyncMock()

        monitor_task = asyncio.create_task(controller.start_status_monitoring())
        await asyncio.sleep(0)

        await controller.stop_status_monitoring()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        controller.scanner.start_scan.assert_called_once()

    async def test_start_status_monitoring_does_not_scan_without_adapter(self):
        """Test the controller doesn't scan on its own in piggyback mode."""
        controller = TrainController()
        controller.scanner.start_scan = AsyncMock()

        monitor_task = asyncio.create_task(controller.start_status_monitoring())
        await asyncio.sleep(0)

        await controller.stop_status_monitoring()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        controller.scanner.start_scan.assert_not_called()
