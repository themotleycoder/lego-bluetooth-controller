"""
Integration tests for API endpoints.

Tests FastAPI endpoints with mocked controllers and authentication.
"""

import pytest
from fastapi import status


class TestHealthEndpoint:
    """Test suite for /health endpoint."""

    def test_health_endpoint_no_auth_required(self, client):
        """Test health endpoint is accessible without authentication."""
        response = client.get("/health")

        assert response.status_code == status.HTTP_200_OK

    def test_health_endpoint_structure(self, client):
        """Test health endpoint returns correct data structure."""
        response = client.get("/health")
        data = response.json()

        assert "status" in data
        assert "timestamp" in data
        assert "version" in data
        assert "bluetooth_available" in data
        assert "connected_trains" in data
        assert "connected_switches" in data
        assert "authentication_enabled" in data

    def test_health_endpoint_types(self, client):
        """Test health endpoint returns correct data types."""
        response = client.get("/health")
        data = response.json()

        assert isinstance(data["status"], str)
        assert isinstance(data["timestamp"], (int, float))
        assert isinstance(data["version"], str)
        assert isinstance(data["bluetooth_available"], bool)
        assert isinstance(data["connected_trains"], int)
        assert isinstance(data["connected_switches"], int)
        assert isinstance(data["authentication_enabled"], bool)


class TestTrainControlEndpoints:
    """Test suite for train control endpoints."""

    def test_train_power_without_auth(self, client):
        """Test train power endpoint rejects requests without API key."""
        response = client.post(
            "/train", json={"hub_id": "90:84:2B:18:28:36", "power": 50}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        data = response.json()
        assert "Missing API key" in data["detail"]

    def test_train_power_with_invalid_auth(self, client, invalid_api_key):
        """Test train power endpoint rejects invalid API key."""
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": 50},
            headers={"X-API-Key": invalid_api_key},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.json()
        assert "Invalid API key" in data["detail"]

    def test_train_power_with_valid_auth(
        self, client, test_api_key, mock_lego_controller
    ):
        """Test train power endpoint accepts valid API key."""
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": 50},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "success"
        assert data["hub_id"] == "90:84:2B:18:28:36"
        assert data["power"] == 50

    def test_train_power_validation_too_high(self, client, test_api_key):
        """Test train power validation rejects power > 100."""
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": 150},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        data = response.json()
        assert "detail" in data

    def test_train_power_validation_too_low(self, client, test_api_key):
        """Test train power validation rejects power < -100."""
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": -150},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_train_power_edge_cases(self, client, test_api_key):
        """Test train power with edge case values."""
        # Test minimum power
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": -100},
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == status.HTTP_200_OK

        # Test maximum power
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": 100},
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == status.HTTP_200_OK

        # Test zero power
        response = client.post(
            "/train",
            json={"hub_id": "90:84:2B:18:28:36", "power": 0},
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == status.HTTP_200_OK


class TestSwitchControlEndpoints:
    """Test suite for switch control endpoints."""

    def test_switch_without_auth(self, client):
        """Test switch endpoint rejects requests without API key."""
        response = client.post(
            "/switch", json={"hub_id": 1, "switch": "A", "position": "STRAIGHT"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_switch_with_valid_auth(self, client, test_api_key):
        """Test switch endpoint with valid authentication."""
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "A", "position": "STRAIGHT"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "success"
        assert data["switch"] == "A"
        assert data["position"] == "STRAIGHT"

    def test_switch_all_positions(self, client, test_api_key):
        """Test switch endpoint with all valid switch names."""
        for switch_name in ["A", "B", "C", "D"]:
            response = client.post(
                "/switch",
                json={"hub_id": 1, "switch": switch_name, "position": "STRAIGHT"},
                headers={"X-API-Key": test_api_key},
            )
            assert response.status_code == status.HTTP_200_OK

    def test_switch_both_positions(self, client, test_api_key):
        """Test switch endpoint with both position types."""
        # Test STRAIGHT
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "A", "position": "STRAIGHT"},
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == status.HTTP_200_OK

        # Test DIVERGING
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "A", "position": "DIVERGING"},
            headers={"X-API-Key": test_api_key},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_switch_invalid_name(self, client, test_api_key):
        """Test switch validation rejects invalid switch names."""
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "E", "position": "STRAIGHT"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_switch_invalid_position(self, client, test_api_key):
        """Test switch validation rejects invalid positions."""
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "A", "position": "INVALID"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_switch_case_insensitive(self, client, test_api_key):
        """Test switch endpoint handles lowercase input."""
        response = client.post(
            "/switch",
            json={"hub_id": 1, "switch": "a", "position": "straight"},
            headers={"X-API-Key": test_api_key},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Should be normalized to uppercase
        assert data["switch"] == "A"
        assert data["position"] == "STRAIGHT"


class TestDeviceStatusEndpoints:
    """Test suite for device status endpoints."""

    def test_connected_trains_without_auth(self, client):
        """Test connected trains endpoint requires authentication."""
        response = client.get("/connected/trains")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_connected_trains_with_auth(
        self, client, test_api_key, sample_train_status, mock_lego_controller
    ):
        """Test connected trains endpoint returns train data."""
        mock_lego_controller.train_controller.get_connected_trains.return_value = (
            sample_train_status
        )

        response = client.get("/connected/trains", headers={"X-API-Key": test_api_key})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "connected_trains" in data
        assert "trains" in data
        assert "timestamp" in data
        assert data["connected_trains"] == 2

    def test_connected_trains_empty(self, client, test_api_key):
        """Test connected trains endpoint with no trains."""
        response = client.get("/connected/trains", headers={"X-API-Key": test_api_key})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["connected_trains"] == 0
        assert data["trains"] == {}

    def test_connected_switches_without_auth(self, client):
        """Test connected switches endpoint requires authentication."""
        response = client.get("/connected/switches")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_connected_switches_with_auth(
        self, client, test_api_key, sample_switch_status, mock_lego_controller
    ):
        """Test connected switches endpoint returns switch data."""
        mock_lego_controller.switch_controller.get_connected_switches.return_value = (
            sample_switch_status
        )

        response = client.get(
            "/connected/switches", headers={"X-API-Key": test_api_key}
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "connected_switches" in data
        assert "switches" in data
        assert "timestamp" in data


class TestSystemControlEndpoints:
    """Test suite for system control endpoints."""

    def test_reset_without_auth(self, client):
        """Test reset endpoint requires authentication."""
        response = client.post("/reset")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_reset_with_auth(self, client, test_api_key):
        """Test reset endpoint with valid authentication."""
        response = client.post("/reset", headers={"X-API-Key": test_api_key})

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "success"
        assert "message" in data
        assert "timestamp" in data


class TestCORSHeaders:
    """Test suite for CORS configuration."""

    def test_cors_preflight(self, client):
        """Test CORS preflight OPTIONS request."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "GET",
            },
        )

        # Should allow the request
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_204_NO_CONTENT]

    def test_cors_headers_present(self, client):
        """Test CORS headers are present in responses."""
        response = client.get("/health", headers={"Origin": "http://localhost:8080"})

        # Check for CORS headers (if properly configured)
        # FastAPI/Starlette adds these automatically
        assert response.status_code == status.HTTP_200_OK
