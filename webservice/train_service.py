#!/usr/bin/env python3
"""
LEGO Train Controller REST API Service.

Provides HTTP endpoints for controlling LEGO trains and switches via Bluetooth.
Implements security, logging, and health monitoring.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from config import get_settings
from dispatcher.factory import build_dispatcher
from middleware.auth import api_key_header, verify_api_key
from servers.main import LegoController
from utils.logging_config import setup_logging, get_logger

# Initialize logging
setup_logging()
logger = get_logger(__name__)

# Initialize settings
settings = get_settings()

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Create FastAPI app with metadata
app = FastAPI(
    title="LEGO Bluetooth Controller API",
    description="REST API for controlling LEGO Powered Up trains and switches via Bluetooth",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Add rate limiter to app state and register exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Add CORS middleware with configured origins
logger.info(f"Configuring CORS with allowed origins: {settings.allowed_origins_list}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Initialize controller
controller = LegoController()

# Initialize the RFID dispatcher (shares the controller's BLE controllers)
dispatcher = (
    build_dispatcher(controller.train_controller, controller.switch_controller)
    if settings.dispatcher_enabled
    else None
)

# Request counter for tracking
request_counter = 0


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Middleware to log all HTTP requests with timing."""
    global request_counter
    request_counter += 1
    request_id = f"req-{request_counter}"

    start_time = time.time()
    logger.info(
        f"Request started: {request.method} {request.url.path}",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else "unknown",
        },
    )

    response = await call_next(request)

    duration = time.time() - start_time
    logger.info(
        f"Request completed: {request.method} {request.url.path} - {response.status_code}",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration * 1000, 2),
        },
    )

    return response


@app.on_event("startup")
async def startup_event():
    """Initialize the controller when the FastAPI app starts."""
    logger.info("Starting up LEGO Bluetooth Controller API service")
    logger.info(f"Authentication {'enabled' if settings.require_auth else 'disabled'}")
    logger.info(f"Bluetooth reset on startup: {settings.bluetooth_reset_on_startup}")

    try:
        await controller.initialize()
        logger.info("Controller initialized successfully")

        # Start monitoring in background tasks. Staggered on purpose: both
        # controllers run their own continuous BLE scan loop against the
        # same adapter, and starting them simultaneously causes BlueZ
        # "Operation already in progress" errors (the switch controller's
        # scan start also power-cycles the adapter via reset_bluetooth,
        # which can kill a scan the train controller just started).
        controller.running = True
        asyncio.create_task(controller.switch_controller.start_status_monitoring())
        await asyncio.sleep(3)
        asyncio.create_task(controller.train_controller.start_status_monitoring())
        logger.info("Background monitoring tasks started")

        if dispatcher is not None:
            asyncio.create_task(dispatcher.run())
            logger.info("RFID dispatcher started")

    except Exception as e:
        logger.error(f"Failed to initialize controller: {e}", exc_info=True)
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup when server shuts down."""
    logger.info("Shutting down LEGO Train Controller API service")
    try:
        controller.running = False
        await controller.train_controller.stop_status_monitoring()
        if dispatcher is not None:
            await dispatcher.stop()
        await asyncio.sleep(1)  # Give time for cleanup
        logger.info("Shutdown completed successfully")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}", exc_info=True)


# ===========================================
# Pydantic Models with Validation
# ===========================================


class TrainPowerCommand(BaseModel):
    """Command to control train motor power."""

    hub_id: str = Field(..., description="BLE address of the train hub")
    power: int = Field(..., ge=-100, le=100, description="Motor power from -100 to 100")

    class Config:
        json_schema_extra = {"example": {"hub_id": "90:84:2B:18:28:36", "power": 50}}


class SwitchCommand(BaseModel):
    """Command to control track switch position."""

    hub_id: int = Field(..., ge=0, description="Hub ID of the switch controller")
    switch: str = Field(..., description="Switch name (A, B, C, or D)")
    position: str = Field(..., description="Switch position (STRAIGHT or DIVERGING)")

    @validator("switch")
    def validate_switch_name(cls, v):
        """Validate switch name is A-D."""
        if v.upper() not in settings.valid_switch_names_list:
            raise ValueError(
                f"Switch must be one of {settings.valid_switch_names_list}"
            )
        return v.upper()

    @validator("position")
    def validate_position(cls, v):
        """Validate position is STRAIGHT or DIVERGING."""
        if v.upper() not in settings.valid_switch_positions_list:
            raise ValueError(
                f"Position must be one of {settings.valid_switch_positions_list}"
            )
        return v.upper()

    class Config:
        json_schema_extra = {
            "example": {"hub_id": 1, "switch": "A", "position": "DIVERGING"}
        }


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str = Field(..., description="Service status (healthy/degraded/unhealthy)")
    timestamp: float = Field(..., description="Current Unix timestamp")
    version: str = Field(..., description="API version")
    bluetooth_available: bool = Field(..., description="Bluetooth adapter availability")
    connected_trains: int = Field(..., description="Number of connected trains")
    connected_switches: int = Field(..., description="Number of connected switches")
    authentication_enabled: bool = Field(
        ..., description="Whether API key auth is enabled"
    )


# ===========================================
# Health Check Endpoint (No auth required)
# ===========================================


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Monitoring"],
    summary="Health check endpoint",
    description="Check service health and availability. No authentication required.",
)
@limiter.limit("100/minute")
async def health_check(request: Request):
    """
    Health check endpoint for monitoring.

    Returns service status, connected devices, and system health.
    Does not require authentication.
    """
    try:
        # Check Bluetooth availability
        bluetooth_available = True
        try:
            import subprocess

            result = subprocess.run(
                ["hciconfig", "hci0"], capture_output=True, timeout=2
            )
            bluetooth_available = result.returncode == 0
        except Exception as e:
            logger.warning(f"Bluetooth health check failed: {e}")
            bluetooth_available = False

        # Get connected device counts
        connected_trains = len(controller.train_controller.get_connected_trains())
        connected_switches = len(controller.switch_controller.get_connected_switches())

        # Determine overall status
        if not bluetooth_available:
            status_str = "unhealthy"
        elif connected_trains == 0 and connected_switches == 0:
            status_str = "healthy"  # No devices but service is operational
        else:
            status_str = "healthy"

        return HealthResponse(
            status=status_str,
            timestamp=time.time(),
            version="1.0.0",
            bluetooth_available=bluetooth_available,
            connected_trains=connected_trains,
            connected_switches=connected_switches,
            authentication_enabled=settings.require_auth,
        )

    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "timestamp": time.time(), "error": str(e)},
        )


# ===========================================
# Train Control Endpoints (Auth required)
# ===========================================


@app.post(
    "/train",
    tags=["Train Control"],
    summary="Control train power",
    description="Set the motor power for a train (-100 to 100)",
)
@limiter.limit("30/minute")
async def control_train_power(
    request: Request, command: TrainPowerCommand, api_key: str = Depends(api_key_header)
):
    """Control train motor power."""
    await verify_api_key(api_key)

    try:
        logger.info(
            f"Power command received: hub_id={command.hub_id}, power={command.power}"
        )
        await controller.train_controller.handle_command(command.hub_id, command.power)
        logger.info(f"Power command successful for train {command.hub_id}")
        return {"status": "success", "hub_id": command.hub_id, "power": command.power}

    except ValueError as e:
        logger.warning(f"Invalid power command: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Power command failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# Switch Control Endpoints (Auth required)
# ===========================================


@app.post(
    "/switch",
    tags=["Switch Control"],
    summary="Control track switch",
    description="Set the position of a track switch (STRAIGHT or DIVERGING)",
)
@limiter.limit("30/minute")
async def control_switch(
    request: Request, command: SwitchCommand, api_key: str = Depends(api_key_header)
):
    """Control track switch position."""
    await verify_api_key(api_key)

    try:
        logger.info(
            f"Switch command received: hub_id={command.hub_id}, "
            f"switch={command.switch}, position={command.position}"
        )

        # Convert position to numeric value
        position = 1 if command.position == "DIVERGING" else 0

        # Send the command to the switch controller
        success = await controller.switch_controller.send_command_with_retry(
            command.hub_id,
            f"SWITCH_{command.switch}",  # Keep the SWITCH_ prefix for compatibility
            position,
        )

        if success:
            logger.info(
                f"Switch command successful: {command.switch} -> {command.position}"
            )
            return {
                "status": "success",
                "hub_id": command.hub_id,
                "switch": command.switch,
                "position": command.position,
            }
        else:
            logger.warning(f"Switch command failed after retries: {command.switch}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to control switch after multiple attempts",
            )

    except ValueError as e:
        logger.warning(f"Invalid switch command: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Switch command failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# Device Status Endpoints (Auth required)
# ===========================================


@app.get(
    "/connected/trains",
    tags=["Device Status"],
    summary="Get connected trains",
    description="Retrieve status information for all connected train hubs",
)
@limiter.limit("200/minute")
async def get_connected_trains(
    request: Request, api_key: str = Depends(api_key_header)
):
    """Get information about all connected train hubs."""
    await verify_api_key(api_key)

    try:
        # Check controller initialization
        if not controller or not controller.train_controller:
            logger.error("Train controller not initialized")
            raise HTTPException(
                status_code=503, detail="Train controller not initialized"
            )

        connected_trains = controller.train_controller.get_connected_trains()
        logger.debug(f"Retrieved {len(connected_trains)} connected trains")

        return {
            "connected_trains": len(connected_trains),
            "trains": connected_trains,
            "timestamp": time.time(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving connected trains: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get(
    "/connected/switches",
    tags=["Device Status"],
    summary="Get connected switches",
    description="Retrieve status information for all connected switch hubs",
)
@limiter.limit("200/minute")
async def get_connected_switches(
    request: Request, api_key: str = Depends(api_key_header)
):
    """
    Get information about all connected switch hubs.

    Returns:
        - connected_switches: Number of connected switch hubs
        - switches: Dictionary of switch hub data with positions and reliability stats
    """
    await verify_api_key(api_key)

    try:
        connected_switches = controller.switch_controller.get_connected_switches()
        logger.debug(f"Retrieved {len(connected_switches)} connected switches")

        return {
            "connected_switches": len(connected_switches),
            "switches": connected_switches,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"Error retrieving connected switches: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# System Control Endpoints (Auth required)
# ===========================================


@app.post(
    "/reset",
    tags=["System Control"],
    summary="Reset Bluetooth adapter",
    description="Reset the Bluetooth adapter to clear connection issues",
)
@limiter.limit("10/minute")
async def reset_bluetooth(request: Request, api_key: str = Depends(api_key_header)):
    """
    Reset Bluetooth adapter.

    Use this endpoint if devices are not connecting or responding properly.
    This will restart the Bluetooth radio.
    """
    await verify_api_key(api_key)

    try:
        logger.warning("Bluetooth reset requested")
        await controller.switch_controller.scanner.reset_bluetooth()
        await controller.train_controller.reset_bluetooth()
        logger.info("Bluetooth reset completed successfully")

        return {
            "status": "success",
            "message": "Bluetooth adapter reset successfully",
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"Bluetooth reset failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# Exception Handlers
# ===========================================


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler with logging."""
    logger.warning(
        f"HTTP {exc.status_code}: {exc.detail}",
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "timestamp": time.time()},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """General exception handler for unhandled errors."""
    logger.error(
        f"Unhandled exception: {exc}",
        exc_info=True,
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "message": str(exc),
            "timestamp": time.time(),
        },
    )


# ===========================================
# Main Entry Point
# ===========================================

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting server on {settings.host}:{settings.port}")
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
