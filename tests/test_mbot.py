"""Offline tests: message format, drive maths, and the phone server's Robot/Hub with a fake robot."""
import asyncio
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbot_protocol as mp   # noqa: E402
import mbot_phone   # noqa: E402


def reply(index, value):
    return b"\xff\x55" + bytes([index, 2]) + struct.pack("<f", value) + b"\r\n"


class Protocol(unittest.TestCase):
    def test_requests_match_the_bytes_the_real_robot_answered(self):
        self.assertEqual(mp.read_request(1, mp.ULTRASONIC, 3).hex(" "), "ff 55 04 01 01 01 03")
        self.assertEqual(mp.read_request(2, mp.LIGHT, 6).hex(" "), "ff 55 04 02 01 03 06")

    def test_write_commands(self):
        self.assertEqual(mp.motors_command(-100, 100).hex(" "), "ff 55 07 00 02 05 9c ff 64 00")
        self.assertEqual(mp.leds_command(1, 2, 3).hex(" "), "ff 55 09 00 02 08 07 02 00 01 02 03")
        self.assertEqual(mp.tone_command(262, 500).hex(" "), "ff 55 07 00 02 22 06 01 f4 01")

    def test_parser_handles_split_frames_acks_and_the_boot_banner(self):
        p = mp.Parser()
        data = b"Version: 06.01.009\r\n" + b"\xff\x55\r\n" + reply(1, 400.0) + reply(2, 12.5)
        out = []
        for i in range(0, len(data), 3):   # three bytes at a time, like small Bluetooth packets
            out += p.feed(data[i:i + 3])
        self.assertEqual(out, [(1, 400.0), (2, 12.5)])

    def test_parser_does_not_cut_a_float_that_contains_a_line_end(self):
        raw = b"\x0d\x0a\x20\x41"   # a float whose bytes include 0D 0A
        value = struct.unpack("<f", raw)[0]
        self.assertEqual(mp.Parser().feed(b"\xff\x55\x05\x02" + raw + b"\r\n"), [(5, value)])


class Driving(unittest.TestCase):
    def test_stick_at_the_eight_arrow_directions_equals_the_keys(self):
        for x, y, keys in [(0, 1, {"up"}), (1, 1, {"up", "right"}), (1, 0, {"right"}), (1, -1, {"down", "right"}),
                           (0, -1, {"down"}), (-1, -1, {"down", "left"}), (-1, 0, {"left"}), (-1, 1, {"up", "left"})]:
            for mode, _ in mp.MODES:
                self.assertEqual(mp.stick_commands(x, y, mode), mp.wheel_commands(keys, mode), (x, y, mode))

    def test_deadzone_and_minimum_speed(self):
        self.assertEqual(mp.stick_commands(0.05, 0.05, "Fast"), (0, 0))
        left, right = mp.stick_commands(0, 0.2, "Slow")
        self.assertEqual((left, right), (mp.MIN_SPEED, mp.MIN_SPEED))

    def test_forward_runs_the_left_motor_backwards(self):
        self.assertEqual(mp.motor_powers(100, 100), (-100, 100))
        self.assertEqual(mp.motor_powers(100, 100, invert_left=True), (100, 100))
        self.assertEqual(mp.motor_powers(100, 50, swap=True), (50, -100))

    def test_ramp_speeds_up_gradually_but_stops_at_once(self):
        self.assertLess(mp.ramp(0, 255, 0.05), 255)
        self.assertEqual(mp.ramp(200, 0, 0.05), 0)
        self.assertEqual(mp.ramp(200, -200, 0.05), 0)   # stop before reversing
        self.assertEqual(mp.ramp(200, 100, 0.05), 100)


class FakeLink:
    """Answers every sensor read with `distance`, and records motor commands."""

    def __init__(self, address, on_data, on_lost):
        self.on_data, self.on_lost = on_data, on_lost
        self.connected = False
        self.distance = 100.0
        self.motors = []

    async def connect(self):
        self.connected = True

    async def write(self, data):
        if data[4] == mp.READ:
            asyncio.get_running_loop().call_soon(self.on_data, reply(data[3], self.distance))
        elif data[5] == mp.JOYSTICK:
            self.motors.append(struct.unpack("<hh", data[6:10]))

    async def close(self):
        self.connected = False


class PhoneServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = mbot_phone.SETTINGS_FILE
        mbot_phone.SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")   # not the real one

    def tearDown(self):
        mbot_phone.SETTINGS_FILE = self.saved
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    async def robot(self):
        robot = mbot_phone.Robot({"id": "AA", "name": "mBot 00AA"}, mbot_phone.COLOURS[0], make_link=FakeLink)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if robot.ready:
                return robot
        self.fail("fake robot never became ready")

    def test_drives_then_stops_when_the_phone_goes_quiet(self):
        async def go():
            robot = await self.robot()
            robot.drive([], stick=[0, 1], client="a")
            await asyncio.sleep(0.2)
            self.assertLess(robot.link.motors[-1][0], 0)   # forward: left motor backwards
            self.assertGreater(robot.link.motors[-1][1], 0)
            await asyncio.sleep(mbot_phone.PHONE_TIMEOUT + 0.2)   # no more commands from the phone
            self.assertEqual(robot.link.motors[-1], (0, 0))
            await robot.close()
        self.run_async(go())

    def test_wall_guard_blocks_forward_but_not_reverse(self):
        async def go():
            robot = await self.robot()
            robot.link.distance = 5.0
            await asyncio.sleep(0.5)   # a sensor read sees the wall
            robot.drive(["up"], client="a")
            await asyncio.sleep(0.1)
            self.assertTrue(robot.guarded)
            self.assertEqual(robot.level, (0, 0))
            robot.drive(["down"], client="a")
            await asyncio.sleep(0.1)
            self.assertLess(robot.level[0], 0)
            await robot.close()
        self.run_async(go())

    def test_one_phone_drives_at_a_time_but_any_phone_can_stop(self):
        async def go():
            robot = await self.robot()
            self.assertTrue(robot.drive(["up"], client="a"))
            self.assertFalse(robot.drive(["down"], client="b"))
            self.assertEqual(robot.control("b"), "other")
            await robot.request_stop("b", explicit=True)
            self.assertEqual(robot.target, (0, 0))
            await robot.close()
        self.run_async(go())

    def test_hub_gives_each_robot_its_own_colour(self):
        async def go():
            make = lambda info, colour: mbot_phone.Robot(info, colour, make_link=FakeLink)
            hub = mbot_phone.Hub(make_robot=make)
            hub.found = [{"id": "AA", "name": "mBot 00AA", "signal": -50},
                         {"id": "BB", "name": "mBot 00BB", "signal": -60}]
            self.assertEqual(await hub.select("phone1", "AA"), "")
            self.assertEqual(await hub.select("phone2", "BB"), "")
            self.assertNotEqual(hub.robots["AA"].colour, hub.robots["BB"].colour)
            self.assertTrue(await hub.select("phone3", "CC"))   # not found: an error
            state = hub.state("phone1")
            self.assertEqual(state["robot"]["id"], "AA")
            await hub.close()
        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
