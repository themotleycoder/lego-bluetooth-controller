"""
Unit tests for BetterBleScanner adapter selection.

Covers passing a BlueZ adapter (e.g. "hci1") through to BleakScanner and
scoping reset_bluetooth() to that adapter, for multi-adapter deployments.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from servers.bluetooth_scanner import BetterBleScanner


class TestBetterBleScannerAdapter:
    """Test suite for adapter-aware scanning and reset."""

    def test_default_adapter_is_none(self):
        """Test the default constructor preserves single-adapter behavior."""
        scanner = BetterBleScanner()

        assert scanner.adapter is None

    def test_adapter_stored(self):
        """Test a configured adapter is stored on the instance."""
        scanner = BetterBleScanner(adapter="hci1")

        assert scanner.adapter == "hci1"

    async def test_start_scan_passes_adapter_to_bleakscanner(self):
        """Test start_scan forwards the configured adapter to BleakScanner."""
        scanner = BetterBleScanner(adapter="hci1")
        scanner.reset_bluetooth = AsyncMock()

        mock_bleak_instance = MagicMock()
        mock_bleak_instance.start = AsyncMock()

        with patch(
            "servers.bluetooth_scanner.BleakScanner", return_value=mock_bleak_instance
        ) as mock_bleak_cls:
            callback = MagicMock()
            await scanner.start_scan(callback)

        mock_bleak_cls.assert_called_once_with(callback, adapter="hci1")
        assert scanner.is_scanning is True

    async def test_start_scan_with_no_adapter_configured(self):
        """Test start_scan passes adapter=None when unconfigured (default adapter)."""
        scanner = BetterBleScanner()
        scanner.reset_bluetooth = AsyncMock()

        mock_bleak_instance = MagicMock()
        mock_bleak_instance.start = AsyncMock()

        with patch(
            "servers.bluetooth_scanner.BleakScanner", return_value=mock_bleak_instance
        ) as mock_bleak_cls:
            callback = MagicMock()
            await scanner.start_scan(callback)

        mock_bleak_cls.assert_called_once_with(callback, adapter=None)

    async def test_reset_bluetooth_uses_hciconfig_when_adapter_set(self):
        """Test reset_bluetooth scopes the reset to the configured adapter."""
        scanner = BetterBleScanner(adapter="hci1")

        with patch("servers.bluetooth_scanner.subprocess.run") as mock_run:
            await scanner.reset_bluetooth()

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert ["sudo", "hciconfig", "hci1", "down"] in commands
        assert ["sudo", "hciconfig", "hci1", "up"] in commands
        assert not any("bluetoothctl" in cmd for cmd in commands)

    async def test_reset_bluetooth_uses_bluetoothctl_when_no_adapter(self):
        """Test reset_bluetooth falls back to the global bluetoothctl power cycle."""
        scanner = BetterBleScanner()

        with patch("servers.bluetooth_scanner.subprocess.run") as mock_run:
            await scanner.reset_bluetooth()

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert ["sudo", "bluetoothctl", "power", "off"] in commands
        assert ["sudo", "bluetoothctl", "power", "on"] in commands
        assert not any("hciconfig" in cmd for cmd in commands)
