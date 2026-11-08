"""Bluetooth LE to the mBot: finding robots and a byte pipe to one of them.

The mBot's Bluetooth module is a wireless serial cable: bytes written to FFE3 reach the board,
and the board's replies come back as notifications on FFE2. Windows needs no pairing for this.
"""
import asyncio

from bleak import BleakClient, BleakScanner

WRITE_CHAR = "0000ffe3-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ffe2-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "Makeblock"
CONNECT_TIMEOUT = 15   # s


def short_name(ble_name, address):
    """'Makeblock_LE001b1062b6a5' → 'mBot B6A5': the end of its address, easy to read out loud."""
    tail = address.replace(":", "")[-4:].upper()
    return f"mBot {tail}" if ble_name.startswith(NAME_PREFIX) else ble_name


async def find_robots(seconds=5):
    """mBots advertising nearby: [{"id", "name", "signal"}], strongest signal first. A robot
    already connected (to this PC or anything else) stops advertising, so it isn't listed."""
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    robots = [{"id": dev.address, "name": short_name(adv.local_name or dev.name or "", dev.address),
               "signal": adv.rssi}
              for dev, adv in found.values() if (adv.local_name or dev.name or "").startswith(NAME_PREFIX)]
    return sorted(robots, key=lambda r: -r["signal"])


class BleLink:
    """One robot's connection. on_data(bytes) gets everything it sends; on_lost() is called if
    the link drops (out of range, switched off)."""

    def __init__(self, address, on_data, on_lost):
        self.address = address
        self.on_data = on_data
        self.on_lost = on_lost
        self.client = None
        self.lock = asyncio.Lock()   # one write at a time

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    async def connect(self):
        self.client = BleakClient(self.address, timeout=CONNECT_TIMEOUT,
                                  disconnected_callback=lambda _: self.on_lost())
        await self.client.connect()
        await self.client.start_notify(NOTIFY_CHAR, lambda _, data: self.on_data(bytes(data)))

    async def write(self, data):
        async with self.lock:
            await self.client.write_gatt_char(WRITE_CHAR, data, response=False)

    async def close(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
