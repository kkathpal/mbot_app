"""Connect to every mBot in range over Bluetooth LE at the same time.

Each robot gets its own LED colour so you can tell them apart, then the script reads
each robot's distance and light sensors a few times.

Usage: python mbot_multi.py [scan seconds]
"""
import asyncio
import struct
import sys

from bleak import BleakClient, BleakScanner

WRITE_CHAR = "0000ffe3-0000-1000-8000-00805f9b34fb"   # bytes to the mBot
NOTIFY_CHAR = "0000ffe2-0000-1000-8000-00805f9b34fb"  # bytes from the mBot
COLOURS = [(20, 0, 0), (0, 20, 0), (0, 0, 20), (20, 12, 0), (12, 0, 20), (0, 15, 15), (20, 20, 20)]
COLOUR_NAMES = ["red", "green", "blue", "yellow", "purple", "cyan", "white"]


class MBot:
    def __init__(self, device, colour_index):
        self.device = device
        self.name = device.name
        self.colour = colour_index
        self.client = BleakClient(device, timeout=20)
        self.buf = bytearray()
        self.replies = {}
        self.next_idx = 1

    def _on_data(self, _, data):
        # Replies can arrive split across several notifications; frames end with 0D 0A
        self.buf.extend(data)
        while True:
            start = self.buf.find(b"\xff\x55")
            end = self.buf.find(b"\r\n", start + 2) if start >= 0 else -1
            if start < 0 or end < 0:
                return
            frame = bytes(self.buf[start + 2:end])
            del self.buf[:end + 2]
            if len(frame) >= 6 and frame[1] == 2:
                fut = self.replies.pop(frame[0], None)
                if fut and not fut.done():
                    fut.set_result(struct.unpack("<f", frame[2:6])[0])

    async def connect(self):
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR, self._on_data)

    async def send(self, body):
        await self.client.write_gatt_char(WRITE_CHAR, bytes([0xFF, 0x55, len(body)]) + bytes(body), response=False)

    async def read(self, device, port):
        idx = self.next_idx
        self.next_idx = idx % 200 + 1
        fut = asyncio.get_running_loop().create_future()
        self.replies[idx] = fut
        await self.send([idx, 0x01, device, port])
        return await asyncio.wait_for(fut, 2)

    async def leds(self, r, g, b):
        await self.send([0x00, 0x02, 0x08, 0x07, 0x02, 0x00, r, g, b])  # both on-board LEDs

    async def disconnect(self):
        try:
            await self.leds(0, 0, 0)
        finally:
            await self.client.disconnect()


async def run_one(bot):
    try:
        await bot.connect()
    except Exception as e:
        print(f"{bot.name}: could not connect ({e})")
        return
    print(f"{bot.name}: connected, LEDs {COLOUR_NAMES[bot.colour % len(COLOURS)]}")
    try:
        await bot.leds(*COLOURS[bot.colour % len(COLOURS)])
        for _ in range(5):
            dist = await bot.read(0x01, 3)   # ultrasonic, port 3
            light = await bot.read(0x03, 6)  # on-board light sensor
            print(f"{bot.name}: distance {dist:6.1f} cm   light {light:5.0f}")
            await asyncio.sleep(1)
    except Exception as e:
        print(f"{bot.name}: lost contact ({e!r})")
    finally:
        await bot.disconnect()


async def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    print(f"Looking for mBots for {seconds:.0f} s...")
    devices = await BleakScanner.discover(timeout=seconds)
    found = sorted((d for d in devices if (d.name or "").startswith("Makeblock")), key=lambda d: d.address)
    print(f"Found {len(found)} mBot(s): " + ", ".join(d.name for d in found))
    if found:
        await asyncio.gather(*(run_one(MBot(d, i)) for i, d in enumerate(found)))


asyncio.run(main())
