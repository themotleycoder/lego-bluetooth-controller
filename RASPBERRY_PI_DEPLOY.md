# Raspberry Pi Deployment - Quick Start Guide

## 🎯 Overview

Deploy the authenticated LEGO Train Controller service to your Raspberry Pi and connect your Flutter app.

**What's Ready:**
- ✅ API authentication with X-API-Key
- ✅ Structured logging
- ✅ CORS configuration
- ✅ Comprehensive test suite
- ✅ Flutter app with authentication

---

## 📋 Pre-Deployment Checklist

### On Your Mac (Preparation)

1. **Copy API Key** from `.env` file:
   ```bash
   grep API_KEYS .env
   ```

   You'll need this value for the Raspberry Pi and Flutter app configuration.

2. **Get Raspberry Pi IP Address**
   - You'll need this for the Flutter app configuration
   - Example: `192.168.1.100` (update Flutter .env with this)

---

## 🚀 Raspberry Pi Setup

### Step 1: Transfer Files to Raspberry Pi

**Option A: Using Git (Recommended)**
```bash
# On Raspberry Pi
cd ~
git clone https://github.com/themotleycoder/lego-bluetooth-controller.git
cd legocontroller

# Or pull latest changes if already cloned
git pull origin main
```

**Option B: Using rsync/scp**
```bash
# On your Mac (update the paths to match your setup)
rsync -av --exclude 'venv' --exclude '__pycache__' \
  /path/to/lego-bluetooth-controller/ \
  pi@192.168.1.100:~/lego-bluetooth-controller/
```

### Step 2: Install Dependencies

```bash
# On Raspberry Pi
cd ~/lego-bluetooth-controller  # or ~/legocontroller if using git

# Update system
sudo apt-get update && sudo apt-get upgrade -y

# Install Python and Bluetooth tools
sudo apt-get install python3 python3-pip python3-venv bluez bluetooth -y

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

### Step 3: Configure Environment

```bash
# Create .env file
nano .env
```

**Add this configuration:**
```bash
# Server Configuration
HOST=0.0.0.0
PORT=8000

# API Authentication
API_KEYS=your-api-key-here
REQUIRE_AUTH=true

# CORS Configuration
ALLOWED_ORIGINS=http://localhost:8080,capacitor://localhost,http://192.168.1.100:8080

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=text

# Bluetooth
BLUETOOTH_RESET_ON_STARTUP=true
```

Save and exit (Ctrl+X, Y, Enter)

### Step 4: Configure Passwordless Sudo for Bluetooth

```bash
# Create sudoers rule for Bluetooth commands
echo 'pi ALL=(ALL) NOPASSWD: /usr/bin/hcitool, /usr/bin/bluetoothctl, /usr/sbin/hciconfig' | sudo tee /etc/sudoers.d/bluetooth

# Set proper permissions
sudo chmod 0440 /etc/sudoers.d/bluetooth

# Test it works
sudo -n hcitool cmd 0x08 0x000A 00
```

If the test works without asking for a password, you're good!

### Step 5: Start the Service

```bash
# Make sure you're in the virtual environment
source ~/lego-bluetooth-controller/venv/bin/activate

# Start the service
python3 webservice/train_service.py
```

**Expected Output:**
```
2026-01-30 18:00:00 | INFO | Starting server on 0.0.0.0:8000
INFO: Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
Starting up LEGO Train Controller API service
Authentication enabled
Controller initialized successfully
```

---

## 🛤️ RFID Autonomous Dispatch (Optional)

If you want trains to run autonomously with collision protection instead of manual control only, you'll additionally need a Mosquitto MQTT broker on the Pi and a Raspberry Pi Pico 2 W + RFID reader per train. Full setup steps (installing Mosquitto, `.env` config, testing with `python -m dispatcher --mock`) are in **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#rfid-dispatcher-optional)**; firmware flashing/wiring is in **[pico/README.md](pico/README.md)**. Skip this section if manual control is all you need — `DISPATCHER_ENABLED` defaults to `false`, so nothing changes unless you opt in.

---

## 📱 Flutter App Configuration

### Update Flutter .env File

```bash
# On your Mac
nano ~/src/lego_trains/legocontroller/.env
```

**Update with Raspberry Pi IP:**
```bash
BACKEND_URL=http://192.168.1.100:8000
API_KEY=your-api-key-here
REQUEST_TIMEOUT_SECONDS=5
POLL_INTERVAL_SECONDS=1
```

**Restart Flutter App:**
```bash
cd ~/src/lego_trains/legocontroller
flutter run
```

---

## 🧪 Testing the Deployment

### 1. Test Health Endpoint (No Auth Required)

```bash
# From your Mac or Raspberry Pi
curl http://192.168.1.100:8000/health | python3 -m json.tool
```

**Expected Response:**
```json
{
    "status": "healthy",
    "timestamp": 1769792000.0,
    "version": "1.0.0",
    "bluetooth_available": true,
    "connected_trains": 1,
    "connected_switches": 0,
    "authentication_enabled": true
}
```

### 2. Test Authenticated Endpoint

```bash
# Should FAIL without API key
curl -X GET http://192.168.1.100:8000/connected/trains

# Should SUCCEED with API key
curl -X GET http://192.168.1.100:8000/connected/trains \
  -H "X-API-Key: your-api-key-here"
```

### 3. Test Train Control

```bash
curl -X POST http://192.168.1.100:8000/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 30}'
```

**Train should move!** 🚂

---

## 🔄 Running as a Service (Production)

### Create systemd Service

```bash
sudo nano /etc/systemd/system/lego-bluetooth-controller.service
```

**Add this configuration:**
```ini
[Unit]
Description=LEGO Train Controller API
After=network.target bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/lego-bluetooth-controller
Environment="PYTHONPATH=/home/pi/lego-bluetooth-controller"
Environment="HOST=0.0.0.0"
ExecStart=/home/pi/lego-bluetooth-controller/venv/bin/python3 webservice/train_service.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Enable and start the service:**
```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable lego-bluetooth-controller.service

# Start the service
sudo systemctl start lego-bluetooth-controller.service

# Check status
sudo systemctl status lego-bluetooth-controller.service

# View logs
sudo journalctl -u lego-bluetooth-controller.service -f
```

**Service Commands:**
```bash
sudo systemctl start lego-train    # Start service
sudo systemctl stop lego-train     # Stop service
sudo systemctl restart lego-train  # Restart service
sudo systemctl status lego-train   # Check status
```

---

## 🐛 Troubleshooting

### Service Won't Start

**Check logs:**
```bash
sudo journalctl -u lego-bluetooth-controller.service -n 50
```

**Common issues:**
- Virtual environment path wrong
- Missing dependencies: `pip install -r requirements.txt`
- Bluetooth not enabled: `sudo systemctl enable bluetooth`

### Authentication Errors

**401 Unauthorized:**
- API key missing from Flutter .env
- Restart Flutter app after changing .env

**403 Forbidden:**
- API key doesn't match between Flutter and Python
- Check both .env files

### Train Not Moving

**Check Bluetooth:**
```bash
hciconfig hci0
sudo systemctl status bluetooth
```

**Check sudo permissions:**
```bash
sudo -n hcitool cmd 0x08 0x000A 00
# Should not ask for password
```

**View service logs:**
```bash
# If running manually
tail -f ~/lego-bluetooth-controller/logs/train_service.log

# If running as service
sudo journalctl -u lego-bluetooth-controller.service -f
```

### Can't Connect from Flutter App

**Check network:**
```bash
# On Raspberry Pi
hostname -I  # Get IP address
ping 192.168.1.50  # Ping your Mac
```

**Check firewall (if enabled):**
```bash
sudo ufw allow 8000
```

**Test from Mac:**
```bash
curl http://192.168.1.100:8000/health
```

---

## 📊 Monitoring

### Check Service Status

```bash
# Service status
systemctl status lego-train

# Recent logs
sudo journalctl -u lego-bluetooth-controller.service --since "10 minutes ago"

# Real-time logs
sudo journalctl -u lego-bluetooth-controller.service -f
```

### Check Connected Devices

```bash
curl -s http://localhost:8000/connected/trains \
  -H "X-API-Key: your-api-key-here" \
  | python3 -m json.tool
```

---

## 🔐 Security Notes

### Production Recommendations

1. **Generate New API Key:**
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Update Both .env Files:**
   - Raspberry Pi: `~/lego-bluetooth-controller/.env`
   - Flutter: `~/src/lego_trains/legocontroller/.env`

3. **Enable Firewall:**
   ```bash
   sudo ufw enable
   sudo ufw allow 8000
   sudo ufw allow ssh
   ```

4. **Regular Updates:**
   ```bash
   sudo apt-get update && sudo apt-get upgrade -y
   ```

---

## 📝 Quick Reference

### Raspberry Pi Commands
```bash
# Start service manually
cd ~/lego-bluetooth-controller
source venv/bin/activate
python3 webservice/train_service.py

# Start as systemd service
sudo systemctl start lego-train

# View logs
sudo journalctl -u lego-train -f

# Stop service
sudo systemctl stop lego-train
```

### Flutter App Commands
```bash
# Update BACKEND_URL
nano ~/src/lego_trains/legocontroller/.env

# Restart app
cd ~/src/lego_trains/legocontroller
flutter run
```

### Testing Commands
```bash
# Health check
curl http://192.168.1.100:8000/health

# Get trains
curl http://192.168.1.100:8000/connected/trains \
  -H "X-API-Key: your-api-key-here"

# Control train
curl -X POST http://192.168.1.100:8000/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key-here" \
  -d '{"hub_id": "90:84:2B:18:28:36", "power": 50}'
```

---

## ✅ Deployment Complete!

Your authenticated LEGO Train Controller is now:
- ✅ Running on Raspberry Pi
- ✅ Secured with API key authentication
- ✅ Accessible from Flutter app
- ✅ Ready to control trains

**Next Steps:**
1. Test all train controls from Flutter app
2. Add more trains/switches as needed
3. Monitor logs for any issues

For issues or questions, check:
- [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) - Full deployment guide
- [SECURITY.md](SECURITY.md) - Security best practices
- [pico/README.md](pico/README.md) - RFID dispatcher firmware setup (optional)
