# mBot RC

Drive Makeblock mBots from a phone. This PC connects to the robots over Bluetooth, and any
phone on the same Wi-Fi opens a controller page served by this PC: two thumb sticks, STOP,
BEEP, three gears, and the robot's distance, line-follower and light sensors.

Several robots can be connected at once. Each phone picks its own robot, and each robot's
lights turn its own colour so you can tell which one you're driving.

## Setup (once)

1. Install Python 3 (python.org).
2. In this folder:
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
3. Make sure Bluetooth is on in Windows. No pairing is needed.

## Driving

1. Switch the mBots on (their Bluetooth light blinks). Close the mBlock app: a robot can only
   connect to one thing at a time.
2. Run `.venv\Scripts\python mbot_phone.py`. It prints an address like
   `http://192.168.4.59:8123`. Allow Python through the firewall if Windows asks.
3. Open that address on a phone on the same Wi-Fi, and tap your robot in the list.
4. Left stick: forward and back. Right stick: left and right. Let go to stop.

If the robot goes the wrong way, open **Setup** on the phone: Invert a wheel that turns
backwards, or Swap sides. **Wall guard** stops it driving forward into anything closer than
12 cm.

## Other tools

- `python ble_scan.py` lists every Bluetooth device nearby and marks the mBots.
- `python mbot_multi.py` connects to every mBot in range at once and reads their sensors.
- `python -m unittest discover -s tests -v` runs the offline tests (no robot needed).
