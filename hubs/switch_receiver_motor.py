from pybricks.hubs import TechnicHub
from pybricks.pupdevices import Motor
from pybricks.parameters import Port, Color, Stop
from pybricks.tools import wait
from usys import stdin, stdout
from uselect import poll

# Motor constants. Position is driven to an absolute angle (not a timed
# power pulse) so it reliably completes the full throw every time instead
# of sometimes falling short -- STRAIGHT is always 0 degrees (the angle
# each motor is zeroed to at startup, see reset_angle below), DIVERGING is
# always MOTOR_ANGLE degrees from there. Tune MOTOR_ANGLE to match the
# physical switch mechanism's actual throw. MOTOR_SPEED intentionally low:
# too high a speed target makes the position controller chase the speed
# under load instead of applying steady torque into the turn, so it
# strains without completing the throw.
MOTOR_SPEED = 150  # deg/s
MOTOR_ANGLE = 40  # degrees of rotation for DIVERGING

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
        motor = Motor(port)
        motor.reset_angle(0)  # this startup position is STRAIGHT (angle 0)
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


def set_switch_position(motor, switch_name, position):
    """Set switch position using motor and update tracking"""
    target_angle = MOTOR_ANGLE if position else 0
    motor.run_target(MOTOR_SPEED, target_angle, then=Stop.HOLD, wait=True)

    # Update position tracking
    switch_positions[switch_name] = position

    # Calculate status based on current positions
    status = 0
    for port, pos in switch_positions.items():
        if pos:
            status += 1 << (ord("D") - ord(port))

    send_status(status)


def send_status(status_value):
    """Write a 2-byte status frame [status_value, port_connections] to stdout."""
    try:
        port_connections = port_connections_bitmap()
        stdout.buffer.write(bytes([status_value, port_connections]))
    except Exception:
        hub.light.on(Color.RED)


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

        wait(10)

    except Exception:
        hub.light.on(Color.RED)
        wait(100)
