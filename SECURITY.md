# Security Guide - LEGO Train Controller

## Overview

This document outlines the security features and best practices for the LEGO Train Controller API service.

## Authentication

### API Key Authentication

The service uses API key authentication to protect all control endpoints.

**How it works:**
- All requests (except `/health`) require a valid API key
- API keys are passed via the `X-API-Key` HTTP header
- Multiple API keys can be configured for different clients

**Configuration:**
```bash
# In .env file
API_KEYS=key1_abc123,key2_def456,key3_ghi789
REQUIRE_AUTH=true
```

**Generate secure API keys:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Disabling Authentication (Development Only)

For local development and testing, you can disable authentication:

```bash
# In .env file
REQUIRE_AUTH=false
```

⚠️ **WARNING:** Never disable authentication in production or on network-accessible services!

## MQTT Security (Optional RFID Dispatcher)

If you enable the RFID dispatcher (`DISPATCHER_ENABLED=true`), it connects to a Mosquitto MQTT broker to receive tag reads from trains and is a separate attack surface from the REST API.

**Configuration:**
```bash
# In .env file
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
MQTT_USERNAME=your-broker-username   # omit/leave blank to disable MQTT auth
MQTT_PASSWORD=your-broker-password
```

**Recommendations:**
- Configure Mosquitto with username/password auth (`mosquitto_passwd`) rather than running it open, especially if the Pi is reachable beyond your local network
- Bind Mosquitto to `localhost` or your local subnet only — there is no built-in TLS for the MQTT link (`mosquitto.conf`'s `listener`/`bind_address`, or a firewall rule)
- Each train's Pico firmware (`pico/config.py`) must be configured with the same `MQTT_USERNAME`/`MQTT_PASSWORD` if broker auth is enabled — see [pico/README.md](pico/README.md)
- A spoofed/malicious tag-read publish on `train/+/tag` could feed the dispatcher false position data; broker auth plus a trusted local network are the primary mitigations, since the dispatcher does not authenticate individual tag events beyond checking the UID is a known track tag

## CORS Configuration

Cross-Origin Resource Sharing (CORS) is configured to restrict which origins can access the API.

**Configuration:**
```bash
# In .env file
ALLOWED_ORIGINS=http://localhost:8080,capacitor://localhost,http://192.168.1.100:8080
```

**Production recommendations:**
- Never use `ALLOWED_ORIGINS=*` in production
- Only allow specific Flutter app origins
- Use HTTPS origins when possible
- Update origins list when deploying to new domains

## Input Validation

All API endpoints validate input using Pydantic models:

### Train Power Validation
- `hub_id`: BLE address string of the train hub
- `power`: Constrained to -100 to 100

### Switch Command Validation
- `hub_id`: Must be non-negative integer
- `switch`: Must be A, B, C, or D
- `position`: Must be STRAIGHT or DIVERGING

Invalid inputs return HTTP 400 with detailed error messages.

## Logging and Monitoring

### Structured Logging

All requests and errors are logged with:
- Timestamp (ISO 8601 UTC)
- Request ID for tracing
- Client IP address
- Request method and path
- Response status code
- Processing duration

**Log format options:**
```bash
# JSON format (recommended for production)
LOG_FORMAT=json

# Human-readable text (for development)
LOG_FORMAT=text
```

### Log Levels

Configure logging verbosity:
```bash
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

**Production recommendation:** Use `INFO` or `WARNING` level

### Log File Output

Enable file logging for persistence:
```bash
LOG_FILE=/var/log/lego-controller.log
```

Ensure the service has write permissions to the log directory.

## Security Best Practices

### 1. Network Security

**Firewall Configuration:**
```bash
# Allow only specific IPs
sudo ufw allow from 192.168.1.0/24 to any port 8000

# Or bind to localhost only
HOST=127.0.0.1  # In .env
```

**Reverse Proxy (Recommended):**
Use nginx or Apache as a reverse proxy for:
- HTTPS/TLS termination
- Rate limiting
- Additional access controls

Example nginx config:
```nginx
server {
    listen 443 ssl;
    server_name trains.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### 2. API Key Management

**DO:**
- ✅ Generate long, random API keys (32+ characters)
- ✅ Use different keys for different clients
- ✅ Rotate keys periodically
- ✅ Store keys in environment variables or secrets manager
- ✅ Revoke compromised keys immediately

**DON'T:**
- ❌ Commit API keys to version control
- ❌ Share keys via insecure channels
- ❌ Use predictable or short keys
- ❌ Reuse keys across environments

### 3. Privilege Management

**Root Access:**
The service requires `sudo` for Bluetooth HCI commands. Minimize risk:

1. **Run as dedicated user:**
```bash
sudo useradd -r -s /bin/false lego-controller
```

2. **Grant specific sudo permissions:**
```bash
# /etc/sudoers.d/lego-controller
lego-controller ALL=(ALL) NOPASSWD: /usr/bin/hcitool
lego-controller ALL=(ALL) NOPASSWD: /usr/bin/bluetoothctl
```

3. **Use systemd service:**
```ini
[Service]
User=lego-controller
Group=lego-controller
```

### 4. Environment Variables

Never expose sensitive configuration:

```bash
# Set restrictive permissions on .env
chmod 600 .env
chown lego-controller:lego-controller .env
```

### 5. Health Monitoring

The `/health` endpoint is public (no auth required) for monitoring:

```bash
# Check service health
curl http://localhost:8000/health

# Expected response
{
  "status": "healthy",
  "bluetooth_available": true,
  "connected_trains": 2,
  "connected_switches": 1
}
```

Monitor health checks and alert on failures.

## Threat Model

### Protected Against

✅ **Unauthorized API access** - API key authentication
✅ **Cross-site requests** - CORS restrictions
✅ **Invalid inputs** - Pydantic validation
✅ **Information disclosure** - Structured error responses
✅ **Log injection** - Structured JSON logging

### Not Protected Against

⚠️ **Network sniffing** - No HTTPS (use reverse proxy)
⚠️ **DoS attacks** - No rate limiting (use reverse proxy or firewall)
⚠️ **Physical device access** - Bluetooth is inherently local
⚠️ **Malicious Bluetooth devices** - Trust local BLE environment
⚠️ **MQTT eavesdropping/spoofing** - No TLS on the MQTT link; a compromised or unauthenticated broker could feed the dispatcher false train positions (see MQTT Security above)

## Incident Response

### If API Key is Compromised

1. **Immediately** remove the compromised key from `API_KEYS`
2. Restart the service: `sudo systemctl restart lego-controller`
3. Generate and distribute new keys to legitimate clients
4. Review logs for unauthorized access:
   ```bash
   grep "Invalid API key" /var/log/lego-controller.log
   ```

### If Service is Compromised

1. Stop the service: `sudo systemctl stop lego-controller`
2. Review system logs: `journalctl -u lego-controller`
3. Check for unauthorized Bluetooth connections
4. Verify file integrity of Python files
5. Rotate all API keys
6. Review and update firewall rules

## Security Checklist

Before deploying to production:

- [ ] Strong API keys generated and configured
- [ ] `REQUIRE_AUTH=true` in .env
- [ ] CORS origins restricted to known Flutter apps
- [ ] Logging enabled (JSON format recommended)
- [ ] Log file permissions set correctly
- [ ] Service running as dedicated non-root user
- [ ] Firewall rules configured
- [ ] Reverse proxy with HTTPS configured (recommended)
- [ ] Health monitoring set up
- [ ] .env file permissions set to 600
- [ ] API keys not committed to version control
- [ ] If using the RFID dispatcher: Mosquitto configured with username/password auth and bound to a trusted network only
- [ ] Pico firmware `config.py` files (per train) are not committed to version control

## Contact

For security issues or questions:
- Review this document
- Check GitHub issues
- Contact the development team

**Do not disclose security vulnerabilities publicly.**
