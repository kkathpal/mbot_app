"""Scan for Bluetooth LE devices nearby and flag the ones that look like mBots."""
import asyncio
import sys

from bleak import BleakScanner

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 10


async def main():
    print(f"Scanning for {SECONDS:.0f} s...")
    found = await BleakScanner.discover(timeout=SECONDS, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -da[1].rssi)
    for dev, adv in rows:
        name = adv.local_name or dev.name or ""
        mbot = "makeblock" in name.lower() or "mbot" in name.lower()
        uuids = ",".join(u[4:8] for u in adv.service_uuids)
        print(f"{'MBOT ' if mbot else '     '}{dev.address}  rssi {adv.rssi:4d}  {name!r:28} {uuids}")
    print(f"{sum(1 for d, a in rows if 'makeblock' in (a.local_name or d.name or '').lower())} Makeblock device(s), {len(rows)} total")


asyncio.run(main())
