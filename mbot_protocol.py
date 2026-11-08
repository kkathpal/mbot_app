"""The mBot factory firmware's message format and the drive maths. No I/O, so tests run offline.

To the robot:  FF 55 <length> <index> <action> <device> [port] [data...]
From it:       FF 55 <index> <type> <value> 0D 0A     (or FF 55 0D 0A after a write)
"""
import math
import struct

READ, WRITE = 1, 2
ULTRASONIC, LIGHT, JOYSTICK, RGBLED, LINE_FOLLOWER, TONE = 1, 3, 5, 8, 17, 34
ULTRASONIC_PORT, LINE_PORT, LIGHT_PORT, BOARD_PORT = 3, 2, 6, 7   # the kit's default wiring

MAX_SPEED = 255          # motor power the firmware accepts (-255..255)
MODES = [("Slow", 40), ("Normal", 70), ("Fast", 100)]   # name, % of MAX_SPEED
TURN_INNER = 0.35        # inner wheel speed while curving, as a fraction of the outer
SPIN_SHARE = 0.6         # spinning in place uses this share of the gear's speed (full speed is dizzying)
MIN_SPEED = 70           # below this the motors only hum, so any move gets at least this much
STICK_DEADZONE = 0.12    # phone joystick: this much of its reach around the centre does nothing
RAMP_SECONDS = 0.35      # stopped to full power; slowing down and stopping are instant
TRIM_RANGE = 20          # Drift fix, in % either way


def frame(index, action, device, port=None, data=b""):
    body = bytes([index, action, device]) + (bytes([port]) if port is not None else b"") + data
    return bytes([0xFF, 0x55, len(body)]) + body


def read_request(index, device, port):
    return frame(index, READ, device, port)


def motors_command(m1, m2):
    """Both motors at once (M1 = left socket, M2 = right), each -255..255. Runs until changed."""
    return frame(0, WRITE, JOYSTICK, data=struct.pack("<hh", int(m1), int(m2)))


def leds_command(r, g, b):
    """Both on-board LEDs to one colour."""
    return frame(0, WRITE, RGBLED, BOARD_PORT, bytes([2, 0, r, g, b]))


def tone_command(hz, ms):
    """A beep. The firmware waits for it to finish, so keep it short."""
    return frame(0, WRITE, TONE, data=struct.pack("<hh", hz, ms))


class Parser:
    """Turns the byte stream from the robot into (index, value) replies. Bytes can arrive split
    over several Bluetooth packets, and a float can itself contain 0D 0A, so frames are cut by
    the length their type says, not by searching for the line end."""
    SIZES = {1: (1, "<B"), 2: (4, "<f"), 3: (2, "<h"), 5: (4, "<l")}

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while True:
            start = self.buf.find(b"\xff\x55")
            if start < 0:   # keep a trailing FF: it may be the start of the next frame
                del self.buf[:-1 if self.buf.endswith(b"\xff") else len(self.buf)]
                return out
            del self.buf[:start]
            if len(self.buf) < 4:
                return out
            if self.buf[2:4] == b"\r\n":   # acknowledgement of a write
                del self.buf[:4]
                continue
            size, fmt = self.SIZES.get(self.buf[3], (None, None))
            if size is None:   # not a frame we know: skip this FF 55
                del self.buf[:2]
                continue
            if len(self.buf) < 4 + size + 2:
                return out
            index = self.buf[2]
            value = struct.unpack(fmt, bytes(self.buf[4:4 + size]))[0]
            del self.buf[:4 + size + 2]
            out.append((index, value))


# ----- driving: same feel as the EV3 phone controller -----
def arrow_speeds(held, mode):
    """Wheel speeds (+ = forward) for held arrows ("up", "down", "left", "right") in this gear."""
    forward = ("up" in held) - ("down" in held)
    turn = ("right" in held) - ("left" in held)
    speed = MAX_SPEED * dict(MODES)[mode] // 100
    if forward == 0:   # spin in place
        spin = int(speed * SPIN_SHARE)
        return turn * spin, -turn * spin
    left = right = forward * speed   # drive, slowing the inner wheel to curve
    if turn > 0:
        right = int(right * TURN_INNER)
    elif turn < 0:
        left = int(left * TURN_INNER)
    return left, right


def drift_fix(left, right, trim):
    """Speed one wheel up and the other down by the same share."""
    t = trim / 100
    clamp = lambda v: int(max(-MAX_SPEED, min(MAX_SPEED, v)))
    return clamp(left * (1 + t)), clamp(right * (1 - t))


def lift(v):
    """Raise a small non-zero speed to MIN_SPEED, keeping its direction."""
    return 0 if v == 0 else int(math.copysign(max(abs(v), MIN_SPEED), v))


def wheel_commands(held, mode, trim=0):
    left, right = drift_fix(*arrow_speeds(held, mode), trim)
    return lift(left), lift(right)


# The arrows' eight moves by stick direction, clockwise from straight ahead.
STICK_MOVES = [{"up"}, {"up", "right"}, {"right"}, {"down", "right"},
               {"down"}, {"down", "left"}, {"left"}, {"up", "left"}]


def stick_commands(x, y, mode, trim=0):
    """Wheel speeds for a joystick at x (right) and y (forward), each -1..1. How far it's pushed
    sets the speed; its direction blends the two nearest arrow moves, so the eight arrow
    directions drive exactly like the keys and nothing jumps around the circle."""
    reach = min(1.0, math.hypot(x, y))
    if reach <= STICK_DEADZONE:
        return 0, 0
    power = (reach - STICK_DEADZONE) / (1 - STICK_DEADZONE)
    i, share = divmod((math.degrees(math.atan2(x, y)) % 360) / 45, 1)
    a, b = (arrow_speeds(STICK_MOVES[(int(i) + k) % 8], mode) for k in (0, 1))
    left, right = (round((p + (q - p) * share) * power) for p, q in zip(a, b))
    left, right = drift_fix(left, right, trim)
    return lift(left), lift(right)


def ramp(level, target, dt, seconds=RAMP_SECONDS):
    """One wheel's next power. Speeding up is limited to full power per `seconds`, which is kinder
    to the plastic gears; slowing, stopping and the stop before reversing happen at once."""
    if target == 0 or (level and (level > 0) != (target > 0)):
        return 0
    if abs(target) <= abs(level):
        return target
    step = MAX_SPEED * dt / seconds
    return int(math.copysign(min(abs(target), abs(level) + step), target))


def motor_powers(left, right, invert_left=False, invert_right=False, swap=False):
    """Wheel speeds (+ = forward) → the firmware's M1, M2. The motors face each other, so the
    left one runs backwards to drive forward; Invert and Swap fix kits wired differently."""
    left, right = (-left if invert_left else left), (-right if invert_right else right)
    m1, m2 = -left, right
    return (m2, m1) if swap else (m1, m2)
