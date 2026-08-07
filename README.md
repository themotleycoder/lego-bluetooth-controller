# lego-bluetooth-controller

A Python-based controller for LEGO Technic Hubs using Bluetooth Low Energy (BLE) communication. This project allows you to control LEGO switches and trains through both a REST API and command-line interface. Trains are controlled over a direct GATT connection to stock LEGO hub firmware; switches use a Pybricks-based broadcast/observe protocol. It supports reliable switch control and an optional RFID-based dispatcher for fully autonomous, collision-protected multi-train operation.

## Prerequisites

- Python 3.x
- `bleak` library for Bluetooth Low Energy communication
- `msgpack` for data serialization
- `paho-mqtt` for the optional RFID dispatcher's MQTT connection
- Linux system with Bluetooth support (tested on Raspberry Pi)
- Root/sudo privileges for Bluetooth operations
- FastAPI and uvicorn for the web service
- Python virtual environment (recommended)
- LEGO Technic Hubs (City Hub running stock firmware for trains, Technic Hub running Pybricks for switches)
- *Optional, for autonomous RFID dispatch:* a Mosquitto MQTT broker, plus a Raspberry Pi Pico 2 W + MFRC522 RFID reader per train and RFID tags at track block boundaries — see [pico/README.md](pico/README.md) and the "RFID Autonomous Dispatch" section below

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/themotleycoder/lego-bluetooth-controller.git
   cd lego-bluetooth-controller
   ```

2. Create and activate a virtual environment (recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to set your `API_KEYS`, `ALLOWED_ORIGINS`, and (if you're using
   the optional RFID dispatcher) your MQTT broker and track wiring settings.
   See `.env.example` for every available setting and its default.

## Running the Web Service

### Direct Command Line

You can run the web service directly using uvicorn:

```bash
sudo PYTHONPATH=/path/to/lego-bluetooth-controller uvicorn webservice.train_service:app --host 0.0.0.0 --port 8000
```

### As a System Service

To run as a system service (recommended for production), see **[SYSTEMD_SERVICE_SETUP.md](SYSTEMD_SERVICE_SETUP.md)** for a comprehensive guide.

Quick setup:

1. Create the service file:
   ```bash
   sudo nano /etc/systemd/system/lego-controller.service
   ```

2. Add the configuration (update paths to match your installation):
   ```ini
   [Unit]
   Description=LEGO Train and Switch Controller Service
   After=network.target bluetooth.target
   Requires=bluetooth.service

   [Service]
   Type=simple
   User=pi
   Group=bluetooth
   WorkingDirectory=/home/pi/lego-bluetooth-controller
   Environment="PYTHONPATH=/home/pi/lego-bluetooth-controller"
   ExecStart=/home/pi/lego-bluetooth-controller/.venv/bin/uvicorn webservice.train_service:app --host 0.0.0.0 --port 8000
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable lego-controller
   sudo systemctl start lego-controller
   sudo systemctl status lego-controller   # Check service status
   ```

See [SYSTEMD_SERVICE_SETUP.md](SYSTEMD_SERVICE_SETUP.md) for troubleshooting, log management, and advanced configuration.

## Web Service API Endpoints

The web service runs on port 8000 and provides the following REST API endpoints.

### Authentication & Rate Limiting

Every endpoint except `/health` requires an `X-API-Key` header matching one of
the comma-separated keys in `API_KEYS` (`.env`), unless `REQUIRE_AUTH=false`.
Each endpoint is also rate-limited (per client IP); limits are noted below.

```bash
curl -X POST http://localhost:8000/train \
  -H "X-API-Key: your-secret-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 40}'
```

### Health
- `GET /health` *(no auth required, 100/minute)* - Service status, Bluetooth availability, and connected device counts.

### Train Control
- `POST /train` *(30/minute)* — `hub_id` is the train hub's BLE address
  ```json
  {
    "hub_id": "90:84:2B:18:28:36",
    "power": 40  // -100 to 100
  }
  ```

### Switch Control
- `POST /switch` *(30/minute)*
  ```json
  {
    "hub_id": 0,
    "switch": "A",  // A, B, C, or D
    "position": "STRAIGHT"  // STRAIGHT or DIVERGING
  }
  ```

### Status Endpoints
- `GET /connected/trains` *(200/minute)* - List connected train hubs (keyed by BLE address) with detailed status information including:
  - Connection state (connected/connecting/disconnected/error)
  - Connection quality (RSSI)
  - Last update timestamp
  
- `GET /connected/switches` *(200/minute)* - List connected switch hubs with detailed information including:
  - Current position of each switch (STRAIGHT or DIVERGING)
  - Connected motor ports
  - Command reliability statistics
  - Connection quality (RSSI)
  - Last update timestamp

### System Control
- `POST /reset` *(10/minute)* - Reset Bluetooth connections

> **Note:** The optional RFID dispatcher (see below) does not add any REST endpoints. It runs as a background task inside the same service and drives trains/switches through the same internal controllers the API uses — manual REST commands still work but bypass the dispatcher's block protection.

## Features

### Train Control
- Manual control with variable speed (-100 to 100) over a direct GATT connection to stock hub firmware
- Persistent per-hub BLE connections with automatic reconnection
- Support for multiple train hubs, addressed by BLE address
- Real-time status monitoring and reporting

### Switch Control
- Support for both Motor and DCMotor switch types
- Control of up to 4 switches per hub (ports A-D)
- Automatic detection of connected motors
- Command verification with retry mechanism
- Reliability statistics for each switch

### RFID Autonomous Dispatch (Optional)
- Raspberry Pi Pico 2 W + RFID reader on each train reports track position over MQTT
- Block protection: a train is held at a block boundary until the block ahead is free
- Automatic switch alignment before a train enters a block that requires it
- Deadlock avoidance when two trains contend for the same block (closer train proceeds)
- Watchdog failsafe stops every train if one misses an expected tag, auto-clearing once it reappears
- Runs standalone with no hardware (`python -m dispatcher --mock`) for testing the routing logic
- See [pico/README.md](pico/README.md) for firmware setup and wiring

### System Features
- Real-time status monitoring of switch and train positions
- Automatic Bluetooth connection management with recovery
- Command queuing for improved performance
- Robust error handling and recovery
- Support for multiple LEGO Technic Hubs
- REST API for remote control
- Systemd service integration for production deployment
- Logging to system files (/var/log/lego-bluetooth-controller.log)

## Technical Details

The project consists of several components:

### Project Structure
```
lego-bluetooth-controller/
├── __init__.py
├── .env.example                    # Environment variable template (copy to .env)
├── .gitignore
├── config.py                       # pydantic-settings Settings class, loads .env
├── lego-bluetooth-controller.service_example  # Systemd service template (customize before use)
├── lego-controller.service          # Your customized service file (not in git)
├── pytest.ini                      # Test runner + coverage config
├── README.md
├── DEPLOYMENT_GUIDE.md, RASPBERRY_PI_DEPLOY.md, SECURITY.md, SYSTEMD_SERVICE_SETUP.md  # Additional docs
├── requirements.txt
├── controllers/                    # Controller logic
│   ├── __init__.py
│   ├── switch_controller.py        # Switch control logic
│   └── train_controller.py         # Train control logic
├── dispatcher/                     # Optional RFID-based autonomous dispatcher
│   ├── track_model.py              # Track topology graph + train/block state
│   ├── block_manager.py            # Block protection, switch locking, contention
│   ├── mqtt_bridge.py              # MQTT bridge to Pico-based RFID readers
│   ├── dispatcher.py               # Main orchestrator + watchdog failsafe
│   ├── factory.py                  # Builds a Dispatcher from app settings
│   └── __main__.py                 # `python -m dispatcher --mock` standalone runner
├── hubs/                           # Code that runs on LEGO hubs
│   ├── switch_receiver_dcmotor.py  # DCMotor-based switch control
│   ├── switch_receiver_motor.py    # Motor-based switch control
│   └── train_receiver.py           # Legacy Pybricks train firmware (unused -- trains run stock firmware now)
├── middleware/                     # Request middleware
│   └── auth.py                     # X-API-Key header verification
├── pico/                           # MicroPython firmware for the RFID reader
│   ├── config.example.py           # Per-train config template (copy to config.py)
│   ├── main.py                     # WiFi/MQTT/RFID polling loop
│   ├── mfrc522.py                  # Vendored MFRC522 driver (MIT licensed)
│   └── README.md                   # Wiring, flashing, and troubleshooting
├── servers/                        # Backend server components
│   ├── __init__.py
│   ├── bluetooth_scanner.py        # Enhanced BLE scanning
│   ├── lego_service.py             # Core service functionality
│   └── main.py                     # Main controller entry point
├── tests/                          # pytest suite (mocks all BLE/MQTT I/O)
│   ├── conftest.py                 # Shared fixtures, test env setup
│   ├── test_api.py, test_auth.py, test_config.py
│   └── test_dispatcher/            # Dispatcher package tests
├── utils/                          # Shared utilities
│   ├── __init__.py
│   ├── constants.py                # Shared constants
│   └── logging_config.py           # Logging setup
└── webservice/                     # API layer
    └── train_service.py            # FastAPI implementation (also starts the dispatcher)
```

### Server Components
- `BetterBleScanner`: Custom BLE scanner with forced cleanup and auto-recovery
- `SwitchController`: Manages switch positions and commands with verification
- `TrainController`: Handles train movement and speed control over persistent per-hub GATT connections
- `FastAPI Web Service`: Provides REST API endpoints for remote control
- `verify_api_key` (`middleware/auth.py`): X-API-Key header validation for mutating endpoints
- `Settings` (`config.py`): pydantic-settings config loaded from `.env`

### Hub Components
- Train hubs run **stock LEGO Powered Up firmware** and are controlled directly over GATT by `TrainController` -- no code is uploaded to them. (`hubs/train_receiver.py` is legacy Pybricks firmware, no longer used.)
- `switch_receiver_motor.py`: Runs on LEGO Technic Hub to control switches using Motor
- `switch_receiver_dcmotor.py`: Runs on LEGO Technic Hub to control switches using DCMotor

### Dispatcher Components (Optional)
- `TrackModel`: Directed graph of RFID tag positions, track blocks, and switch requirements
- `BlockManager`: Grants/queues block entry, sets switches, resolves contention between trains
- `MqttBridge`: Subscribes to RFID tag events from trains, bridges paho-mqtt's network thread to asyncio
- `Dispatcher`: Orchestrates position tracking, block protection, and watchdog failsafe stops
- `pico/main.py`: Runs on a Raspberry Pi Pico 2 W to poll an RFID reader and publish tag events over MQTT

### Communication
- Bluetooth Low Energy (BLE) for wireless communication
- Custom protocol for reliable command transmission
- Status monitoring with automatic reconnection
- Command queuing and batching for improved performance

### Recent Structure Changes

The project has recently undergone a structural reorganization to improve code organization and maintainability:

1. **Controller Logic Separation**:
   - Controller logic has been moved from `servers/` to a dedicated `controllers/` directory
   - This separates the business logic from the server infrastructure

2. **Shared Utilities**:
   - Constants and shared utilities have been moved to a dedicated `utils/` directory
   - This improves reusability and makes dependencies clearer

3. **Import Structure**:
   - Changed from relative imports to absolute imports for better reliability
   - This prevents issues with imports when running from different contexts

These changes make the codebase more modular and easier to maintain, while preserving all functionality.

### Recommended Structure Improvements
For future development, consider these additional structural improvements:

1. **Examples**:
   - Add an `examples/` directory with sample scripts
   - Include example configurations for different setups

2. **Client-Server Separation**:
   - Consider separating client code into a dedicated package
   - This would allow for easier distribution of client libraries

## Troubleshooting

If you encounter issues:

1. Check the service logs:
   ```bash
   sudo journalctl -u lego-controller -f
   ```

2. View application logs:
   ```bash
   sudo tail -f /var/log/lego-bluetooth-controller.log
   sudo tail -f /var/log/lego-controller.error.log
   ```

3. Reset Bluetooth connections:
   ```bash
   curl -X POST http://localhost:8000/reset -H "X-API-Key: your-secret-api-key-here"
   ```

4. Verify Bluetooth status:
   ```bash
   sudo hciconfig
   ```

## Error Handling and Reliability

The system includes comprehensive error handling for:
- Bluetooth connection issues with automatic recovery
- Command transmission failures with intelligent retry mechanisms
- Invalid API requests with detailed error responses
- Status parsing errors with fallback mechanisms
- Service recovery and auto-restart
- Command verification with position feedback
- Reliability statistics for monitoring system performance

Each operation includes multiple fallback mechanisms to ensure reliable communication with the LEGO hubs, with special attention to the challenges of Bluetooth communication in potentially noisy environments.
