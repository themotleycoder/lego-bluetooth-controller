"""
Unit tests for SwitchController adapter selection.

Covers passing a BlueZ adapter (e.g. "hci0") through to the controller's
scanner and to BleakClient connections, for multi-adapter deployments.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from controllers.switch_controller import SwitchController


class TestSwitchControllerAdapter:
    """Test suite for adapter-aware scanning and connecting."""

    def test_default_adapter_is_none(self):
        """Test the default constructor preserves single-adapter behavior."""
        controller = SwitchController()

        assert controller._adapter is None
        assert controller.scanner.adapter is None

    def test_adapter_passed_to_scanner(self):
        """Test a configured adapter is stored and forwarded to the scanner."""
        controller = SwitchController(adapter="hci0")

        assert controller._adapter == "hci0"
        assert controller.scanner.adapter == "hci0"

    async def test_connect_hub_passes_adapter_to_bleakclient(self):
        """Test _connect_hub forwards the configured adapter to BleakClient."""
        controller = SwitchController(adapter="hci0")
        controller._prepare_hub = AsyncMock(return_value=True)

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        device = MagicMock()
        device.address = "AA:BB:CC:DD:EE:FF"

        with patch(
            "controllers.switch_controller.BleakClient", return_value=mock_client
        ) as mock_client_cls:
            await controller._connect_hub(4, device)

        _, kwargs = mock_client_cls.call_args
        assert kwargs["adapter"] == "hci0"
        assert controller._connection_state[4] == "connected"
