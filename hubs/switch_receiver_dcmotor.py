from pybricks.hubs import TechnicHub
from pybricks.pupdevices import DCMotor
from pybricks.parameters import Port, Color
from pybricks.tools import wait, StopWatch
from usys import stdin, stdout
from uselect import poll

# How often to re-send the status frame when idle, so battery voltage stays
# current even if the switch hasn't moved -- otherwise the last known
# reading could be arbitrarily stale for a switch that rarely gets thrown.
BATTERY_REPORT_INTERVAL_MS = 15000

# Initialize hub. Commands/status now travel over the Pybricks GATT
# connection (stdin/stdout) instead of broadcast/observe -- see
# controllers/switch_controller.py for the host side of this protocol.
#
# IMPORTANT: stdout here carries the binary status protocol
# (stdout.buffer.write below), so this script must never call print() --
# Pybricks routes print() through the same stdout stream, which would
# interleave text with the binary status frames the controller parses.
# Use hub.light for on-hub diagnostics instead.
hub = TechnicHub()

# Poll object used to check for incoming stdin bytes without blocking.
keyboard = poll()
keyboard.register(stdin)

# Dictionary to store motors and their ports
motors = {}
active_ports = []

for port, port_name in zip([Port.A, Port.B, Port.C, Port.D], ["A", "B", "C", "D"]):
    try:
        motor = DCMotor(port)
        motors[port_name] = motor
        active_ports.append(port_name)
    except Exception:
        pass

# Track switch positions (0=straight, 1=diverging)
switch_positions = {}
for port in active_ports:
    switch_positions[port] = 0


def port_connections_bitmap():
    """Bitmap of which ports have a motor attached (port_bit = 1 << (D - port))."""
    port_connections = 0
    for port in ["A", "B", "C", "D"]:
        if port in active_ports:
            port_connections += 1 << (ord("D") - ord(port))
    return port_connections


current_status = 0  # last computed status byte, resent on the battery heartbeat


def set_switch_position(motor, switch_name, position):
    """Set switch position using motor and update tracking"""
    global current_status
    motor.dc(70 if position else -70)
    wait(200)
    motor.brake()

    # Update position tracking
    switch_positions[switch_name] = position

    # Calculate status based on current positions
    status = 0
    for port, pos in switch_positions.items():
        if pos:
            status += 1 << (ord("D") - ord(port))

    current_status = status
    send_status(status)


def send_status(status_value):
    """Write a 4-byte status frame [status_value, port_connections,
    battery_mv_high, battery_mv_low] to stdout."""
    try:
        port_connections = port_connections_bitmap()
        battery_mv = hub.battery.voltage()
        stdout.buffer.write(
            bytes([status_value, port_connections]) + battery_mv.to_bytes(2, "big")
        )
    except Exception:
        hub.light.on(Color.RED)


battery_timer = StopWatch()
hub.light.on(Color.GREEN)
send_status(0)
hub.light.on(Color.BLUE)

while True:
    try:
        if keyboard.poll(0):
            # Command format: 2 bytes [switch_num(1-4), position(0/1)]
            data = stdin.buffer.read(2)
            if data and len(data) == 2:
                switch_num, position = data[0], data[1]
                switch = chr(ord("A") + switch_num - 1)
                if switch in motors and position in (0, 1):
                    set_switch_position(motors[switch], switch, position)

        if battery_timer.time() >= BATTERY_REPORT_INTERVAL_MS:
            send_status(current_status)
            battery_timer.reset()

        wait(10)

    except Exception:
        hub.light.on(Color.RED)
        wait(100)
