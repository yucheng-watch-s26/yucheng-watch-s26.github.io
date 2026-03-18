#!/usr/bin/env python3
"""
YCBT Watch — Python BLE client
Device uses standard GATT profiles (no custom service on this firmware).

Usage:
    pip install bleak
    python3 ycbt.py                     # auto-scan
    python3 ycbt.py E2:D5:1F:01:8E:A1  # direct MAC
"""
import asyncio, struct, sys, time
from bleak import BleakClient, BleakScanner

# Standard GATT UUIDs (confirmed present on this device)
HR_SVC      = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEAS     = "00002a37-0000-1000-8000-00805f9b34fb"  # notify
HR_SENSOR   = "00002a38-0000-1000-8000-00805f9b34fb"  # read

BAT_SVC     = "0000180f-0000-1000-8000-00805f9b34fb"
BAT_LEVEL   = "00002a19-0000-1000-8000-00805f9b34fb"  # notify, read

DEV_SVC     = "0000180a-0000-1000-8000-00805f9b34fb"
MANUFACTURER= "00002a29-0000-1000-8000-00805f9b34fb"  # read

# Custom service (may be present on other firmware versions)
YC_SVC      = "be940000-7333-be46-b7ae-689e71722bd5"
YC_WRITE    = "be940001-7333-be46-b7ae-689e71722bd5"
YC_NOTIFY   = "be940002-7333-be46-b7ae-689e71722bd5"


def parse_hr(data: bytes) -> dict:
    """Parse standard Heart Rate Measurement (0x2a37)"""
    flags = data[0]
    hr_fmt_16 = flags & 0x01
    energy_present = (flags >> 3) & 0x01
    rr_present = (flags >> 4) & 0x01

    offset = 1
    if hr_fmt_16:
        hr = struct.unpack_from("<H", data, offset)[0]
        offset += 2
    else:
        hr = data[offset]
        offset += 1

    energy = None
    if energy_present:
        energy = struct.unpack_from("<H", data, offset)[0]
        offset += 2

    rr = []
    while rr_present and offset + 1 < len(data):
        rr.append(struct.unpack_from("<H", data, offset)[0] / 1024.0)
        offset += 2

    return {"hr": hr, "energy": energy, "rr": rr}


def parse_battery(data: bytes) -> int:
    return data[0]


async def main():
    mac = sys.argv[1] if len(sys.argv) > 1 else None

    if not mac:
        print("Scanning...")
        devices = await BleakScanner.discover(timeout=8.0)
        candidates = [d for d in devices if d.name and
                      any(x in d.name for x in ["Pro", "YC", "Watch", "Band", "80M"])]
        if not candidates:
            print("No watch found. All devices:")
            for d in sorted(devices, key=lambda x: x.rssi or -99, reverse=True):
                print(f"  {d.address}  rssi={d.rssi:4}  {d.name or '(no name)'}")
            return
        mac = candidates[0].address
        print(f"Found: {candidates[0].name} @ {mac}\n")

    print(f"Connecting to {mac}...")

    async with BleakClient(mac, timeout=15.0) as client:
        print(f"Connected: {client.is_connected}\n")

        # Dump all services
        print("Services:")
        all_chars = {}
        for svc in client.services:
            print(f"  {svc.uuid}")
            for c in svc.characteristics:
                all_chars[c.uuid] = c
                print(f"    {c.uuid}  [{','.join(c.properties)}]")
        print()

        has_yc = YC_SVC in [s.uuid for s in client.services]
        has_hr = HR_MEAS in all_chars
        has_bat = BAT_LEVEL in all_chars

        print(f"Custom YC service : {'YES' if has_yc else 'NO'}")
        print(f"HR Measurement    : {'YES' if has_hr else 'NO'}")
        print(f"Battery Level     : {'YES' if has_bat else 'NO'}")
        print()

        # Read static info
        if MANUFACTURER in all_chars:
            try:
                data = await client.read_gatt_char(MANUFACTURER)
                print(f"Manufacturer : {data.decode('utf-8', errors='replace')}")
            except Exception as e:
                print(f"Manufacturer read failed: {e}")

        if BAT_LEVEL in all_chars:
            try:
                data = await client.read_gatt_char(BAT_LEVEL)
                print(f"Battery      : {parse_battery(data)} %")
            except Exception as e:
                print(f"Battery read failed: {e}")
        print()

        # Subscribe to Heart Rate
        if has_hr:
            def on_hr(_, data: bytearray):
                r = parse_hr(bytes(data))
                rr_str = f"  RR={[f'{x:.3f}s' for x in r['rr']]}" if r['rr'] else ""
                print(f"  ❤  HR: {r['hr']:3} BPM{rr_str}")

            await client.start_notify(HR_MEAS, on_hr)
            print("Subscribed to Heart Rate notify")
        else:
            print("WARNING: No Heart Rate characteristic found")

        # Subscribe to Battery
        if has_bat:
            def on_bat(_, data: bytearray):
                print(f"  🔋 Battery: {parse_battery(bytes(data))} %")
            try:
                await client.start_notify(BAT_LEVEL, on_bat)
                print("Subscribed to Battery notify")
            except:
                pass  # battery notify is optional

        # Custom YC protocol if available
        if has_yc and YC_WRITE in all_chars and YC_NOTIFY in all_chars:
            print("Custom YC service found — also subscribing...")

            def on_yc(_, data: bytearray):
                b = bytes(data)
                print(f"  YC RX: {b.hex(' ').upper()}")

            try:
                await client.start_notify(YC_NOTIFY, on_yc)
                print("Subscribed to YC notify")
            except Exception as e:
                print(f"YC notify failed: {e}")

        # If nothing to subscribe to
        if not has_hr and not has_yc:
            print("ERROR: No usable characteristics found.")
            print("Try pairing the device first via Windows Bluetooth settings.")
            return

        print("\n── Listening (Ctrl+C to stop) ───────────────")
        print("HR data will appear automatically when the watch measures.\n")

        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
