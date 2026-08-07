# Deployment Guide - LEGO Train Controller

## Table of Contents
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Service](#running-the-service)
- [Testing](#testing)
- [RFID Dispatcher (Optional)](#rfid-dispatcher-optional)
- [Production Deployment](#production-deployment)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements
- Linux system (Raspberry Pi or similar)
- Python 3.9 or higher
- Bluetooth adapter (built-in or USB)
- Root/sudo access for Bluetooth control

### Software Dependencies
```bash
# Update system packages
sudo apt-get update && sudo apt-get upgrade -y

# Install Python and pip
sudo apt-get install python3 python3-pip python3-venv -y

# Install Bluetooth tools
sudo apt-get install bluez bluetooth -y

# Verify Bluetooth is working
hciconfig hci0
```

## Installation

### 1. Clone Repository
```bash
git clone https://github.com/themotleycoder/lego-bluetooth-controller.git
cd lego-bluetooth-controller
```

### 2. Create Virtual Environment
```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip
```

### 3. Install Dependencies
```bash
# Install Python packages
pip install -r requirements.txt

# Verify installation
python -c "import fastapi, bleak, pydantic; print('Dependencies OK')"
```

## Configuration

### 1. Create Environment File
```bash
# Copy example configuration
cp .env.example .env

# Edit configuration
nano .env
```

### 2. Generate API Keys
```bash
# Generate a secure API key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Example output: zQ7yJ8xR9K3nF2mL4vB6wT5pC8hN1dG7eX9jM0kA3s

# Add to .env file
echo "API_KEYS=zQ7yJ8xR9K3nF2mL4vB6wT5pC8hN1dG7eX9jM0kA3s" >> .env
```

### 3. Configure CORS Origins
```bash
# For local Flutter development
ALLOWED_ORIGINS=http://localhost:8080,capacitor://localhost

# For production (add your actual domain)
ALLOWED_ORIGINS=https://your-domain.com,capacitor://localhost
```

### 4. Minimum Configuration (.env)
```bash
# Required settings
API_KEYS=your-generated-key-here
ALLOWED_ORIGINS=http://localhost:8080
REQUIRE_AUTH=true
LOG_LEVEL=INFO
LOG_FORMAT=json
```

## Running the Service

### Development Mode

**Option 1: Direct Python**
```bash
# Activate virtual environment
source venv/bin/activate

# Run with sudo (required for Bluetooth)
sudo PYTHONPATH=. venv/bin/python webservice/train_service.py
```

**Option 2: Uvicorn**
```bash
# Activate virtual environment
source venv/bin/activate

# Run with Uvicorn
sudo PYTHONPATH=. venv/bin/uvicorn webservice.train_service:app --host 0.0.0.0 --port 8000 --reload
```

### Production Mode (Systemd Service)

**1. Create systemd service file:**
```bash
sudo nano /etc/systemd/system/lego-bluetooth-controller.service
```

**2. Add service configuration:**
```ini
[Unit]
Description=LEGO Train Controller API Service
After=network.target bluetooth.target
Requires=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/pi/lego-bluetooth-controller
Environment="PATH=/home/pi/lego-bluetooth-controller/venv/bin"
Environment="PYTHONPATH=/home/pi/lego-bluetooth-controller"
ExecStart=/home/pi/lego-bluetooth-controller/venv/bin/uvicorn webservice.train_service:app --host 0.0.0.0 --port 8000

# Restart on failure
Restart=on-failure
RestartSec=5s

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=lego-bluetooth-controller

[Install]
WantedBy=multi-user.target
```

**3. Enable and start service:**
```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable lego-bluetooth-controller

# Start service
sudo systemctl start lego-bluetooth-controller

# Check status
sudo systemctl status lego-bluetooth-controller
```

**4. View logs:**
```bash
# Follow live logs
sudo journalctl -u lego-bluetooth-controller -f

# View recent logs
sudo journalctl -u lego-bluetooth-controller -n 100

# View logs since boot
sudo journalctl -u lego-bluetooth-controller -b
```

## Testing

### 1. Health Check
```bash
# Test health endpoint (no auth required)
curl http://localhost:8000/health

# Expected response:
{
  "status": "healthy",
  "timestamp": 1234567890.123,
  "version": "1.0.0",
  "bluetooth_available": true,
  "connected_trains": 0,
  "connected_switches": 0,
  "authentication_enabled": true
}
```

### 2. Test Authentication
```bash
# Without API key (should fail)
curl -X POST http://localhost:8000/train \
  -H "Content-Type: application/json" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 50}'

# Expected: 401 Unauthorized

# With valid API key (should succeed)
curl -X POST http://localhost:8000/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 50}'

# Expected: {"status": "success", "hub_id": "90:84:2B:18:28:36", "power": 50}
```

### 3. Test Train Control
```bash
# Set train power (hub_id is the train hub's BLE address)
curl -X POST http://localhost:8000/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 75}'

# Get connected trains
curl -X GET http://localhost:8000/connected/trains \
  -H "X-API-Key: your-api-key-here"
```

### 4. Test Switch Control
```bash
# Control switch
curl -X POST http://localhost:8000/switch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{"hub_id": 1, "switch": "A", "position": "DIVERGING"}'

# Get connected switches
curl -X GET http://localhost:8000/connected/switches \
  -H "X-API-Key: your-api-key-here"
```

### 5. API Documentation
```bash
# Open interactive API docs in browser
http://localhost:8000/docs

# Or ReDoc
http://localhost:8000/redoc
```

## RFID Dispatcher (Optional)

Enables fully autonomous, collision-protected train movement using RFID tags at track block boundaries. Skip this section if you're only using manual train/switch control.

### 1. Install Mosquitto (MQTT Broker)
```bash
sudo apt-get install mosquitto mosquitto-clients -y

# Optional but recommended: require broker auth
sudo mosquitto_passwd -c /etc/mosquitto/passwd your-mqtt-username
echo "password_file /etc/mosquitto/passwd" | sudo tee -a /etc/mosquitto/conf.d/local.conf
echo "allow_anonymous false" | sudo tee -a /etc/mosquitto/conf.d/local.conf
sudo systemctl restart mosquitto

# Verify
mosquitto_sub -t 'train/+/tag' -u your-mqtt-username -P your-mqtt-password
```

### 2. Configure the Dispatcher (.env)
```bash
DISPATCHER_ENABLED=true
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
MQTT_USERNAME=your-mqtt-username
MQTT_PASSWORD=your-mqtt-password
TRAIN_HUB_MAPPING=TRN-A=90:84:2B:18:28:36,TRN-B=F3:33:66:0C:3A:6A
```

See `config.py` for the full list of `mqtt_*`/`dispatcher_*` settings (broker keepalive, topic templates, watchdog timeout, cruise power).

### 3. Test the Dispatcher Logic Without Hardware
```bash
# Runs the routing/block-protection state machine with a scripted event
# sequence -- no broker, BLE hardware, or physical track needed
python -m dispatcher --mock --duration 10
```

### 4. Flash the Pico Firmware

Each train needs a Raspberry Pi Pico 2 W + MFRC522 reader running the firmware in `pico/`, configured with matching `MQTT_BROKER_HOST`/`MQTT_USERNAME`/`MQTT_PASSWORD` and a unique `TRAIN_ID`. See **[pico/README.md](pico/README.md)** for wiring, flashing, and verification steps.

### 5. Start the Service and Verify

Restart the service as usual (see [Running the Service](#running-the-service)); the dispatcher starts automatically as a background task when `DISPATCHER_ENABLED=true`. Confirm tag events are flowing:
```bash
mosquitto_sub -t 'train/+/tag' -u your-mqtt-username -P your-mqtt-password
```

## Production Deployment

### 1. Security Hardening

See [SECURITY.md](SECURITY.md) for detailed security guidelines.

**Quick checklist:**
```bash
# Set restrictive permissions on .env
chmod 600 .env

# Ensure strong API keys
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Verify CORS settings
grep ALLOWED_ORIGINS .env

# Enable authentication
grep "REQUIRE_AUTH=true" .env
```

### 2. Reverse Proxy with HTTPS

**Install Nginx:**
```bash
sudo apt-get install nginx certbot python3-certbot-nginx -y
```

**Configure Nginx:**
```bash
sudo nano /etc/nginx/sites-available/lego-bluetooth-controller
```

```nginx
server {
    listen 80;
    server_name trains.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Enable site and get SSL certificate:**
```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/lego-bluetooth-controller /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Get SSL certificate
sudo certbot --nginx -d trains.yourdomain.com
```

### 3. Firewall Configuration
```bash
# Allow SSH
sudo ufw allow ssh

# Allow HTTP/HTTPS (if using nginx)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Or allow specific IP only
sudo ufw allow from 192.168.1.0/24 to any port 8000

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status
```

### 4. Log Management
```bash
# Create log directory
sudo mkdir -p /var/log/lego-bluetooth-controller
sudo chown root:root /var/log/lego-bluetooth-controller

# Configure log rotation
sudo nano /etc/logrotate.d/lego-bluetooth-controller
```

```
/var/log/lego-bluetooth-controller/*.log {
    daily
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 root root
}
```

### 5. Monitoring

**Health Check Script:**
```bash
#!/bin/bash
# /usr/local/bin/check-lego-bluetooth-controller.sh

HEALTH_URL="http://localhost:8000/health"
RESPONSE=$(curl -s "$HEALTH_URL")
STATUS=$(echo "$RESPONSE" | jq -r '.status')

if [ "$STATUS" != "healthy" ]; then
    echo "Service unhealthy: $RESPONSE"
    systemctl restart lego-bluetooth-controller
fi
```

**Add to crontab:**
```bash
# Check every 5 minutes
*/5 * * * * /usr/local/bin/check-lego-bluetooth-controller.sh
```

## Troubleshooting

### Service Won't Start

**Check logs:**
```bash
sudo journalctl -u lego-bluetooth-controller -n 50 --no-pager
```

**Common issues:**
1. **Import errors** - Verify virtual environment: `source venv/bin/activate && pip list`
2. **Permission errors** - Ensure service runs as root or with bluetooth permissions
3. **Port in use** - Check: `sudo lsof -i :8000`

### Bluetooth Issues

**Reset Bluetooth:**
```bash
# Manual reset
sudo systemctl restart bluetooth
sudo hciconfig hci0 down
sudo hciconfig hci0 up

# Or use API endpoint
curl -X POST http://localhost:8000/reset \
  -H "X-API-Key: your-api-key-here"
```

**Check Bluetooth status:**
```bash
# Check adapter
hciconfig hci0

# Check for LEGO devices
sudo hcitool lescan

# Check service
sudo systemctl status bluetooth
```

### Connection Timeouts

**Increase timeouts in .env:**
```bash
STATUS_UPDATE_INTERVAL=0.2
INACTIVE_DEVICE_THRESHOLD=10.0
MAX_COMMAND_RETRIES=5
```

### Authentication Errors

**Verify API key:**
```bash
# Check configured keys
grep API_KEYS .env

# Test key
curl -X GET http://localhost:8000/connected/trains \
  -H "X-API-Key: your-key-here" -v
```

### High CPU Usage

**Check monitoring tasks:**
```bash
# View process details
ps aux | grep python

# Reduce update frequency in .env
STATUS_UPDATE_INTERVAL=0.5
```

## Updating the Service

### Pull Latest Changes
```bash
# Stop service
sudo systemctl stop lego-bluetooth-controller

# Update code
git pull origin main

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt --upgrade

# Restart service
sudo systemctl start lego-bluetooth-controller

# Verify
sudo systemctl status lego-bluetooth-controller
```

## Support

### Useful Commands
```bash
# Service management
sudo systemctl start lego-bluetooth-controller
sudo systemctl stop lego-bluetooth-controller
sudo systemctl restart lego-bluetooth-controller
sudo systemctl status lego-bluetooth-controller

# View logs
sudo journalctl -u lego-bluetooth-controller -f

# Test configuration
sudo PYTHONPATH=. venv/bin/python -c "from config import get_settings; print(get_settings())"

# Check health
curl http://localhost:8000/health | jq
```

### Getting Help
- Check logs: `sudo journalctl -u lego-bluetooth-controller -n 100`
- Review [SECURITY.md](SECURITY.md) for security questions
- Test with curl commands above
- Check GitHub issues

---

**Deployment completed successfully!** 🚂

Your LEGO Train Controller is now running with:
- ✅ API key authentication
- ✅ Structured logging
- ✅ Input validation
- ✅ Health monitoring
- ✅ Production-ready configuration
