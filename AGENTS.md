# mBot RC — notes for coding agents

PC-side tools for Makeblock mBots (mCore board, ATmega328) running Makeblock's **factory
firmware** (seen: `Version: 06.01.009`). The PC talks to each robot over **Bluetooth LE** with
`bleak`: the robot's Bluetooth module is a wireless serial cable (write to characteristic
`FFE3`, replies arrive as notifications on `FFE2`). No pairing, nothing installed on the robot.
Modelled on `../ev3/ev3_dashboard` (its `ev3_phone.py` / `ev3_phone.html`); keep the two
controllers feeling the same.

## Files

| File | What it is |
|---|---|
| `mbot_protocol.py` | No I/O. Message frames (`read_request`, `motors_command`, `leds_command`, `tone_command`), the reply `Parser` (cuts frames by the length their type gives, since a float can contain `0D 0A`; skips the `FF 55 0D 0A` write acks and the boot banner), and the drive maths: `arrow_speeds`/`wheel_commands`/`stick_commands` (same blending as EV3: the eight stick directions equal the arrow keys), `drift_fix`, `lift` (`MIN_SPEED`, below which motors only hum), `ramp` (speeding up limited by `RAMP_SECONDS`; slowing, stopping and the stop before reversing are instant), `motor_powers` (wheel space → M1/M2: the left motor is mirrored, then Invert/Swap). |
| `mbot_link.py` | `find_robots()` (BLE scan for names starting `Makeblock`; connected robots stop advertising) and `BleLink` (connect, `write`, notifications → `on_data`, `on_lost`). |
| `mbot_phone.py` + `mbot_phone.html` | Phone controller. asyncio runs on the main thread (bleak on Windows prefers it); the HTTP server runs in a thread and hands every request to `Hub.handle()` on the loop with `run_coroutine_threadsafe`, so all robot state is touched from one thread. `Hub`: one `Robot` per robot in use, `picked` phone → robot, Bluetooth search (`/scan`), `select()` gives each new robot a free `COLOURS` entry (its LEDs light in it), robots unused for `REAP_AFTER` are stopped and disconnected. `Robot`: `_run` (connect, send a motor stop, set LEDs, poll sensors; reconnect on loss or `MISSES_LOST` unanswered reads), `_tick` every `TICK` (phone timeout → stop, target → ramp → `_apply`, which sends only changes plus a `RESEND` repeat), one phone drives at a time (`_claim`, `DRIVER_IDLE`, `/takeover`, explicit `/stop` from any phone) like EV3, Wall guard (`GUARD_CM`, blocks forward only). Settings per robot (by Bluetooth address) in `mbot_settings.json`: `invert_left`, `invert_right`, `swap`, `trim`, `mode`, `guard`. |
| `mbot_multi.py` | Stand-alone check: connect to every mBot in range at once, colour their LEDs, read sensors. |
| `ble_scan.py` | Lists nearby BLE devices, flags mBots. |
| `how-mbot-works.html` | Beginner explainer page (published as an Artifact). |
| `tests/test_mbot.py` | Offline tests: frames, parser, drive maths, `Robot`/`Hub` with a `FakeLink`. |

## Firmware commands used

| What | Bytes |
|---|---|
| Read ultrasonic (port 3) | `FF 55 04 <i> 01 01 03` → float cm, 400 = nothing in range |
| Read light (on board) | `FF 55 04 <i> 01 03 06` → float 0–1023 |
| Read line follower (port 2) | `FF 55 04 <i> 01 11 02` → float 0 both black, 1 left black, 2 right black, 3 both white |
| Motors | `FF 55 07 00 02 05 <M1 int16> <M2 int16>` (−255..255; runs until changed) |
| LEDs | `FF 55 09 00 02 08 07 02 00 R G B` |
| Beep | `FF 55 07 00 02 22 <Hz int16> <ms int16>` (the firmware blocks for the beep) |

## Rules that must hold

- **The firmware has no motor timeout**: a motor command runs until the next one. All stopping
  is done by the PC (`PHONE_TIMEOUT`, STOP, page hidden/blurred, `Hub.close()` on Ctrl+C, a stop
  as the first command after every (re)connect). Never add a path that starts the motors
  without going through `Robot._tick`'s timeout. If the Bluetooth link drops, the robot keeps
  its last command until it reconnects; a custom firmware with a watchdog would be the fix.
- **Do not move the real robot on your own.** Reading sensors and setting LEDs are fine; ask
  the user before anything that turns a motor, and let them do the driving tests.
- Keep the phone driving exactly like the keys (the stick's eight directions equal the arrows).
- Tests that make a `Robot` must point `mbot_phone.SETTINGS_FILE` at a temp file.

## Checking a change

1. `.venv\Scripts\python -m unittest discover -s tests -v`
2. `.venv\Scripts\python mbot_phone.py`, open the printed address, pick the robot.
3. Hand the driving check to the user with concrete steps.
