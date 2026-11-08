"""mBot RC for phones: drive mBots from any phone browser on your Wi-Fi.

This PC connects to the robots over Bluetooth and serves a touch controller page to phones
on the same Wi-Fi. Each phone picks its own robot: phones on different robots drive them
independently, and phones on the same robot share it (one drives at a time).

Run:   python mbot_phone.py               (random free port from 8000-8999)
       python mbot_phone.py --port 8090   (use this port)
then open the printed http://<this PC>:<port> address on a phone.

Safety: the mBot's firmware keeps its motors running until told otherwise, so stopping is
done here. The phone re-sends the joystick every ~100 ms, and the robot stops if the driving
phone goes quiet for PHONE_TIMEOUT (finger lifted, phone locked, Wi-Fi lost). If the Bluetooth
link itself drops, the robot is stopped as soon as it reconnects; until then it carries on with
its last command.
"""
import asyncio
import json
import math
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import mbot_protocol as mp
from mbot_link import BleLink, find_robots

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "mbot_phone.html")
SETTINGS_FILE = os.path.join(HERE, "mbot_settings.json")
PORT_RANGE = (8000, 8999)   # each run uses a random free port from here (--port N picks one)
PHONE_TIMEOUT = 0.35   # stop if the driving phone sends nothing for this long (s)
DRIVER_IDLE = 1.0      # one phone drives at a time: another may take over once it's sent nothing for this long (s)
REAP_AFTER = 60        # disconnect a robot no phone has used for this long (s)
TICK = 0.05            # how often motor power is updated (s)
RESEND = 1.0           # repeat an unchanged running motor command this often, in case one was lost (s)
POLL_MOVING, POLL_IDLE = 0.12, 0.4   # sensor reads while driving / standing (s)
READ_TIMEOUT = 1.0     # s to wait for a sensor reply
MISSES_LOST = 4        # this many unanswered reads in a row means the robot is gone
GUARD_CM = 12          # Wall guard: no driving forward when something is closer than this (cm)
HORN = (523, 120)      # Hz, ms (the robot waits for the beep, so keep it short)
HORN_GAP = 0.3         # at most one honk per this many s
SCAN_SECONDS = 5
DIRECTIONS = {"up", "down", "left", "right"}
# Each robot's LEDs light up in its own colour so you can tell which one you picked
COLOURS = [("Red", (60, 0, 0)), ("Green", (0, 60, 0)), ("Blue", (0, 0, 60)), ("Yellow", (60, 40, 0)),
           ("Purple", (40, 0, 60)), ("Cyan", (0, 50, 50)), ("Orange", (60, 18, 0)), ("White", (40, 40, 40))]


def take_port_arg():
    """Remove `--port N` from the command line and return N (or None)."""
    args = sys.argv
    if "--port" not in args:
        return None
    i = args.index("--port")
    if i + 1 < len(args) and args[i + 1].isdigit():
        port = int(args[i + 1])
        del args[i:i + 2]
        return port
    sys.exit("Use:  python mbot_phone.py --port 8090")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, SETTINGS_FILE)


def parse_stick(value):
    """A /drive request's joystick position as (x, y), each clamped to -1..1, or None if it
    isn't one (json.loads accepts NaN and Infinity, which would reach the motors)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
        return None
    return tuple(max(-1.0, min(1.0, float(v))) for v in value)


def client_id(value):
    """A phone page's random id as sent (letters, digits, dashes; at most 64), or None."""
    if isinstance(value, str) and 0 < len(value) <= 64 and all(c.isalnum() or c == "-" for c in value):
        return value
    return None


def hex_colour(rgb):
    top = max(rgb) or 1   # LED levels are kept low; show the hue at full brightness
    return "#" + "".join(f"{round(v * 255 / top):02x}" for v in rgb)


def describe(e):
    return str(e) or type(e).__name__


class Robot:
    """One mBot: its Bluetooth link, sensor readings, and the joystick of the phones that picked
    it turned into motor power. Everything runs on the asyncio loop."""

    def __init__(self, info, colour, make_link=BleLink):
        self.id, self.name = info["id"], info["name"]
        self.colour = colour   # (name, rgb) from COLOURS
        self.loop = asyncio.get_running_loop()
        self.link = make_link(self.id, self._on_data, self._on_lost)
        self.parser = mp.Parser()
        self.waiting = {}      # reply index -> future
        self.next_index = 1
        self.lost = asyncio.Event()
        self.status = "Connecting over Bluetooth…"
        self.ready = False     # connected and answering
        self.readings = {"distance": None, "light": None, "line": None}
        self.rtt = None
        self.misses = 0
        self.settings = load_settings().get(self.id, {})
        self.mode = self.settings.get("mode") if self.settings.get("mode") in dict(mp.MODES) else "Normal"
        self.held, self.stick = set(), None
        self.target = self.level = (0, 0)   # wheel space: + = forward
        self.sent, self.sent_at = None, 0.0
        self.last_command = 0.0
        self.driver, self.driver_seen = None, 0.0
        self.guarded = False
        self.last_horn = 0.0
        self.used = time.monotonic()
        self.closing = False
        self.tasks = [asyncio.create_task(self._run()), asyncio.create_task(self._tick())]

    # ----- link -----
    def _on_data(self, data):
        self.loop.call_soon_threadsafe(self._replies, data)

    def _replies(self, data):
        for index, value in self.parser.feed(data):
            fut = self.waiting.pop(index, None)
            if fut and not fut.done():
                fut.set_result(value)

    def _on_lost(self):
        self.loop.call_soon_threadsafe(self.lost.set)

    async def read(self, device, port):
        """One sensor value, or None if the robot didn't answer in time."""
        index = self.next_index
        self.next_index = index % 250 + 1
        fut = self.loop.create_future()
        self.waiting[index] = fut
        started = time.monotonic()
        try:
            await self.link.write(mp.read_request(index, device, port))
            value = await asyncio.wait_for(fut, READ_TIMEOUT)
        except asyncio.TimeoutError:
            self.misses += 1
            return None
        finally:
            self.waiting.pop(index, None)
        self.misses = 0
        self.rtt = time.monotonic() - started
        return value

    async def _run(self):
        """Connect, then keep reading the sensors; reconnect whenever the link drops."""
        while not self.closing:
            try:
                self.status = "Connecting over Bluetooth…"
                self.lost.clear()
                self.parser = mp.Parser()
                await self.link.connect()
                self.sent = None
                await self._send_motors(0, 0)   # whatever it was doing when the link dropped
                await self.link.write(mp.leds_command(*self.colour[1]))
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status = f"Can't reach {self.name}: {describe(e)}"
            self.ready = False
            await self.link.close()
            if not self.closing:
                await asyncio.sleep(2)

    async def _poll(self):
        n = 0
        while not self.closing:
            if self.lost.is_set() or not self.link.connected:
                raise ConnectionError("the Bluetooth link dropped")
            distance = await self.read(mp.ULTRASONIC, mp.ULTRASONIC_PORT)
            if distance is not None:
                self.readings["distance"] = distance
                self.ready, self.status = True, "Connected"
            if n % 3 == 0:   # these change slowly
                for key, device, port in (("light", mp.LIGHT, mp.LIGHT_PORT),
                                          ("line", mp.LINE_FOLLOWER, mp.LINE_PORT)):
                    value = await self.read(device, port)
                    if value is not None:
                        self.readings[key] = value
            if self.misses >= MISSES_LOST:
                raise ConnectionError("it stopped answering")
            n += 1
            await asyncio.sleep(POLL_MOVING if self.moving else POLL_IDLE)

    # ----- motors -----
    @property
    def moving(self):
        return any(self.level) or any(self.target)

    def _update_target(self):
        trim = self.settings.get("trim", 0)
        if self.stick:
            target = mp.stick_commands(*self.stick, self.mode, trim)
        else:
            target = mp.wheel_commands(self.held, self.mode, trim)
        d = self.readings["distance"]
        self.guarded = bool(self.settings.get("guard", True) and target[0] + target[1] > 0
                            and d is not None and 2 <= d < GUARD_CM)
        self.target = (0, 0) if self.guarded else target

    async def _tick(self):
        last = time.monotonic()
        while not self.closing:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            dt, last = now - last, now
            if (self.held or self.stick) and now - self.last_command > PHONE_TIMEOUT:
                self.held, self.stick = set(), None   # the driving phone went quiet
            self._update_target()
            self.level = tuple(mp.ramp(lv, t, dt) for lv, t in zip(self.level, self.target))
            await self._apply()

    async def _apply(self):
        s = self.settings
        powers = mp.motor_powers(*self.level, s.get("invert_left", False), s.get("invert_right", False),
                                 s.get("swap", False))
        if powers != self.sent or (any(powers) and time.monotonic() - self.sent_at > RESEND):
            await self._send_motors(*powers)

    async def _send_motors(self, m1, m2):
        if not self.link.connected:
            return
        try:
            await self.link.write(mp.motors_command(m1, m2))
        except Exception:
            return   # tried again next tick
        self.sent, self.sent_at = (m1, m2), time.monotonic()

    # ----- phones -----
    def _claim(self, client, moving):
        """One phone drives at a time. True if `client` may drive now: nobody holds the
        controls (or the holder has sent nothing that moves the robot for DRIVER_IDLE), or it
        holds them. Moving input claims them."""
        now = time.monotonic()
        if self.driver not in (None, client) and now - self.driver_seen < DRIVER_IDLE:
            return False
        if moving:
            self.driver, self.driver_seen = client, now
        return True

    def control(self, client):
        """For the page: "you" (holds the controls), "other" (another phone does) or "free"."""
        if self.driver is None or time.monotonic() - self.driver_seen >= DRIVER_IDLE:
            return "free"
        return "you" if self.driver == client else "other"

    def drive(self, held, mode=None, stick=None, client=None):
        stick = parse_stick(stick)
        held = {d for d in held if d in DIRECTIONS} if isinstance(held, list) else set()
        if not self._claim(client, stick is not None or bool(held)):
            return False   # another phone is driving: ignore this one
        if mode in dict(mp.MODES) and mode != self.mode:
            self.set_mode(mode)
        self.stick, self.held = stick, held
        self.last_command = time.monotonic()
        self._update_target()
        return True

    async def stop(self):
        self.held, self.stick = set(), None
        self.target = self.level = (0, 0)
        self.sent = None   # send it even if the last command was a stop
        await self._apply()

    async def request_stop(self, client, explicit=False):
        """The STOP button (explicit) stops the robot from any phone, for safety. A phone just
        going away (locked, switched app) only stops it if that phone is the one driving."""
        if explicit or self._claim(client, False):
            await self.stop()

    async def take_over(self, client):
        await self.stop()
        self.driver, self.driver_seen = client, time.monotonic()

    def set_mode(self, mode):
        self.mode = mode
        self._save({"mode": mode})   # this robot opens in the same gear next time

    def _save(self, changes):
        self.settings.update(changes)
        data = load_settings()
        data[self.id] = {**data.get(self.id, {}), **changes}
        try:
            save_settings(data)
        except OSError:
            pass   # still used for this run

    async def update_setup(self, data):
        changes = {k: data[k] for k in ("invert_left", "invert_right", "swap", "guard")
                   if isinstance(data.get(k), bool)}
        trim = data.get("trim")
        if isinstance(trim, (int, float)) and not isinstance(trim, bool) and math.isfinite(trim):
            changes["trim"] = int(max(-mp.TRIM_RANGE, min(mp.TRIM_RANGE, trim)))
        if changes:
            await self.stop()
            self._save(changes)

    async def horn(self):
        now = time.monotonic()
        if self.ready and now - self.last_horn >= HORN_GAP:
            self.last_horn = now
            await self.link.write(mp.tone_command(*HORN))

    async def set_colour(self, name):
        colour = dict(COLOURS).get(name)
        if colour:
            self.colour = (name, colour)
            if self.ready:
                await self.link.write(mp.leds_command(*colour))

    async def close(self):
        self.closing = True
        await self.stop()
        if self.link.connected:
            try:
                await self.link.write(mp.leds_command(0, 0, 0))
            except Exception:
                pass
        for task in self.tasks:
            task.cancel()
        await self.link.close()

    def state(self, client):
        s = self.settings
        return {
            "robot": {"id": self.id, "name": self.name, "connected": self.ready, "status": self.status,
                      "colour": self.colour[0], "hex": hex_colour(self.colour[1]),
                      "ms": round(self.rtt * 1000) if self.rtt and self.ready else None},
            "control": self.control(client),
            "readings": self.readings,
            "guarded": self.guarded,
            "left": self.level[0], "right": self.level[1],
            "mode": self.mode,
            "setup": {"invert_left": s.get("invert_left", False), "invert_right": s.get("invert_right", False),
                      "swap": s.get("swap", False), "trim": s.get("trim", 0), "guard": s.get("guard", True)},
        }


class Hub:
    """The robots this PC is connected to, and which one each phone drives. New phones pick a
    robot from the list (a Bluetooth search runs at start and whenever a phone asks)."""

    def __init__(self, make_robot=Robot):
        self.make_robot = make_robot
        self.robots = {}    # robot id -> Robot
        self.picked = {}    # phone id -> robot id
        self.seen = {}      # phone id -> when it last asked for anything
        self.found = []     # the last search's results
        self.scan = {"running": False, "error": "", "done": False}
        self.tasks = [asyncio.create_task(self._reap_loop())]

    def start_scan(self):
        if not self.scan["running"]:
            self.scan["running"] = True
            self.tasks.append(asyncio.create_task(self._scan()))

    async def _scan(self):
        try:
            self.found = await find_robots(SCAN_SECONDS)
            self.scan["error"] = ""
        except Exception as e:
            self.scan["error"] = f"The Bluetooth search failed ({describe(e)}). Is Bluetooth turned on on this PC?"
        finally:
            self.scan.update(running=False, done=True)

    def _free_colour(self):
        used = {r.colour[0] for r in self.robots.values()}
        return next((c for c in COLOURS if c[0] not in used), COLOURS[len(self.robots) % len(COLOURS)])

    async def select(self, client, robot_id):
        info = next((f for f in self.found if f["id"] == robot_id), None)
        if robot_id in self.robots:
            info = {"id": robot_id, "name": self.robots[robot_id].name}
        if not info or not client:
            return "That robot isn't in the list any more. Tap Search again."
        old = self.robots.get(self.picked.get(client))
        if old and old.id != robot_id and old.driver == client:
            await old.stop()   # stop what this phone was driving before it moves on
        self.picked[client] = robot_id
        if robot_id not in self.robots:
            self.robots[robot_id] = self.make_robot(info, self._free_colour())
        self.robots[robot_id].used = time.monotonic()
        return ""

    async def _release_unused(self, now=None):
        """Forget phones gone for REAP_AFTER, and disconnect robots no phone has used for as long."""
        now = time.monotonic() if now is None else now
        for client in [c for c, t in self.seen.items() if now - t > REAP_AFTER]:
            self.seen.pop(client, None)
            self.picked.pop(client, None)
        in_use = set(self.picked.values())
        for robot_id in [r for r, robot in self.robots.items()
                         if r not in in_use and now - robot.used > REAP_AFTER]:
            await self.robots.pop(robot_id).close()

    async def _reap_loop(self):
        while True:
            await asyncio.sleep(5)
            await self._release_unused()

    def _robot_list(self, client):
        phones = {}
        for c, r in self.picked.items():
            phones[r] = phones.get(r, 0) + 1
        out = [{"id": r.id, "name": r.name, "connected": r.ready, "status": r.status, "colour": r.colour[0],
                "hex": hex_colour(r.colour[1]), "phones": phones.get(r.id, 0), "signal": None,
                "yours": self.picked.get(client) == r.id}
               for r in self.robots.values()]
        out += [{**f, "connected": False, "status": "", "colour": None, "hex": None, "phones": 0, "yours": False}
                for f in self.found if f["id"] not in self.robots]
        return out

    def state(self, client):
        robot = self.robots.get(self.picked.get(client))
        state = robot.state(client) if robot else {"robot": None, "control": "free", "readings": {},
                                                   "guarded": False, "left": 0, "right": 0,
                                                   "mode": "Normal", "setup": None}
        return {**state, "modes": [{"name": n, "pct": p} for n, p in mp.MODES], "max": mp.MAX_SPEED,
                "guard_cm": GUARD_CM, "trim_range": mp.TRIM_RANGE, "colours": [n for n, _ in COLOURS],
                "robots": self._robot_list(client), "scan": dict(self.scan)}

    async def handle(self, path, data, client):
        """One request from a phone."""
        now = time.monotonic()
        if client:
            self.seen[client] = now
        if path == "/scan":
            self.start_scan()
        elif path == "/select":
            error = await self.select(client, data.get("robot"))
            return {**self.state(client), "error": error}
        robot = self.robots.get(self.picked.get(client))
        if robot:
            robot.used = now
            if path == "/drive":
                robot.drive(data.get("held", []), data.get("mode"), data.get("stick"), client)
            elif path == "/stop":
                await robot.request_stop(client, data.get("explicit") is True)
            elif path == "/takeover":
                await robot.take_over(client)
            elif path == "/mode":
                if data.get("mode") in dict(mp.MODES) and robot._claim(client, False):
                    robot.set_mode(data["mode"])
            elif path == "/horn":
                await robot.horn()
            elif path == "/colour":
                await robot.set_colour(data.get("colour"))
            elif path == "/setup":
                await robot.update_setup(data)
        return self.state(client)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for robot in list(self.robots.values()):
            await robot.close()
        self.robots.clear()


LOOP = HUB = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse lets a second copy take a port that's already in use
    # (and phones then reach either one), so there a busy port must fail instead.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A phone closing the tab or switching apps mid-request is normal; don't print tracebacks.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hub(self, path, data, client):
        """Run the request on the asyncio loop, where the robots live."""
        return asyncio.run_coroutine_threadsafe(HUB.handle(path, data, client), LOOP).result(timeout=10)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:   # read each time so edits show up on refresh
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.partition("?")[0] == "/state":
            query = parse_qs(self.path.partition("?")[2])
            self._json(self._hub("/state", {}, client_id(query.get("c", [None])[0])))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "bad json"}, 400)
        if self.path not in ("/scan", "/select", "/drive", "/stop", "/takeover", "/mode", "/horn",
                             "/colour", "/setup"):
            return self.send_error(404)
        self._json(self._hub(self.path, data, client_id(data.get("client"))))

    def log_message(self, *args):   # keep the console quiet (10 requests a second)
        pass


def lan_addresses():
    """This PC's addresses a phone on the same network could reach."""
    found = []
    try:   # the interface the default route uses (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found and not ip.startswith("127."):
            found.append(ip)
    return found


def start_server(wanted=None):
    """The web server on the `wanted` port, or on a random free port in PORT_RANGE."""
    if wanted:
        try:
            return Server(("0.0.0.0", wanted), Handler)
        except OSError as e:
            sys.exit(f"Can't use port {wanted} ({e.strerror or e}).\n"
                     f"Something else is using it: pick another, or leave out --port for a random free one.")
    ports = list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
    random.shuffle(ports)
    for port in ports:
        try:
            return Server(("0.0.0.0", port), Handler)
        except OSError:
            continue   # in use: try another
    sys.exit(f"No free port between {PORT_RANGE[0]} and {PORT_RANGE[1]}.")


async def amain(port_arg):
    global LOOP, HUB
    LOOP = asyncio.get_running_loop()
    server = start_server(port_arg)
    HUB = Hub()
    HUB.start_scan()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    print("\nmBot RC phone controller")
    for ip in lan_addresses():
        print(f"  open on your phone:  http://{ip}:{port}")
    print("  (phone must be on the same Wi-Fi; allow Python through the firewall if asked)")
    print("  Ctrl+C to quit")
    try:
        await asyncio.Event().wait()
    finally:
        print("stopping…")
        await HUB.close()   # stops every robot and turns its lights off


def main():
    port = take_port_arg()
    try:
        asyncio.run(amain(port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
