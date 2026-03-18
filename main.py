#!/usr/bin/env python3
"""
YCBT Watch — real-time BLE client (YC be940000 protocol).

Device E2:D5:1F:01:8E:A1 confirms be940000 service:
  be940001  [write-without-response, write, indicate]  ← write + responses
  be940002  [write-without-response, write]             ← secondary write
  be940003  [indicate]                                  ← responses

Usage:
    pip install bleak
    python main.py [MAC]        # real-time HR + BP + SpO2 (default)
    python main.py [MAC] --hr   # HR only
    python main.py [MAC] --bp   # BP only
    python main.py [MAC] --raw  # dump every raw frame (debug)
"""
import asyncio, struct, sys, time, datetime
from bleak import BleakClient, BleakScanner

DEFAULT_MAC = "E2:D5:1F:01:8E:A1"

# ── YC custom service ──────────────────────────────────────────────────────
YC_WRITE   = "be940001-7333-be46-b7ae-689e71722bd5"  # write + indicate
YC_NOTIFY  = "be940003-7333-be46-b7ae-689e71722bd5"  # indicate (device→app)

# Standard GATT (kept for battery / GATT HR fallback)
HR_MEAS    = "00002a37-0000-1000-8000-00805f9b34fb"
BAT_LEVEL  = "00002a19-0000-1000-8000-00805f9b34fb"
MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"

# ── dataType constants  (group<<8 | key, from CMD.java + Constants.java) ──
DT_TIME_SYNC    = 0x0100   # Setting  key=0  — sync clock
DT_HEART_ON     = 0x0301   # AppCtrl  key=1  — HeartTest on/off
DT_BLOOD_ON     = 0x0302   # AppCtrl  key=2  — BloodTest on/off
DT_BLOOD_CHECK  = 0x0303   # AppCtrl  key=3  — BloodCheck (SpO2) on/off
DT_REAL_STREAM  = 0x0309   # AppCtrl  key=9  — real-time stream on/off
DT_BP_MEASURE2  = 0x032E   # AppCtrl  key=46 — AppStartBloodMeasurement: real BP measure
DT_GET_REAL     = 0x0220   # Get      key=32 — GetAllRealDataFromDevice
DT_GET_SPO2     = 0x0211   # Get      key=17 — GetRealBloodOxygen
DT_HEALTH_BLOOD = 0x0508   # Health   key=8  — Health_HistoryBlood (sync stored BP)
DT_HEALTH_HEART = 0x0506   # Health   key=6  — Health_HistoryHeart
DT_HEALTH_ALL   = 0x0509   # Health   key=9  — Health_HistoryAll
DT_HEALTH_SPO2  = 0x051A   # Health   key=26 — Health_HistoryBloodOxygen
DT_HEALTH_COMP  = 0x052F   # Health   key=47 — Health_HistoryComprehensiveMeasureData

# ── response dataTypes  (device→app, from DataUnpack.java) ────────────────
RT_HR    = 0x0601   # Real UploadHeart:  payload[0]=BPM
RT_BLOOD = 0x0603   # Real UploadBlood:  payload[0]=SBP, [1]=DBP, [2]=HR
RT_SPO2  = 0x0602   # Real UploadBloodOxygen: payload[0]=%
RT_DONE  = 0x040E   # DeviceMeasurementResult: [0]=type, [1]=1ok/2fail
RT_BPDONE= 0x0410   # AppStartBloodMeasurement result: [0]=status(0ok), [1]=SBP, [2]=DBP
RT_COMP  = 0x060A   # Real UploadComprehensive (multi-field)


# ══════════════════════════════════════════════════════════════════════════
# YC frame codec  (YCBTClientImpl.sendData2Device + ByteUtil.crc16_compute)
# ══════════════════════════════════════════════════════════════════════════

def _crc16(data: bytes) -> int:
    s = 0xFFFF
    for b in data:
        s = (((s << 8) & 0xFF00) | ((s >> 8) & 0xFF)) ^ (b & 0xFF)
        s ^= (s & 0xFF) >> 4
        s ^= (s << 12) & 0xFFFF
        s ^= ((s & 0xFF) << 5) & 0xFFFF
    return s & 0xFFFF


def frame(dt: int, payload: bytes = b"") -> bytes:
    group, key = (dt >> 8) & 0xFF, dt & 0xFF
    n = len(payload) + 6
    hdr = bytes([group, key, n & 0xFF, n >> 8]) + payload
    c = _crc16(hdr)
    return hdr + bytes([c & 0xFF, c >> 8])


def parse(data: bytes) -> dict | None:
    if len(data) < 6:
        return None
    g, k = data[0], data[1]
    n = data[2] | (data[3] << 8)
    if n != len(data):
        return None
    return {"dt": (g << 8) | k, "g": g, "k": k, "p": bytes(data[4:n - 2])}


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def yc_dt(ts_yc: int) -> str:
    """Convert YC epoch (seconds since 2000-01-01) to human-readable datetime."""
    try:
        return datetime.datetime.utcfromtimestamp(ts_yc + 946684800).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"ts={ts_yc}"




def show(pkt: dict, raw: bool) -> None:
    dt, p, g, k = pkt["dt"], pkt["p"], pkt["g"], pkt["k"]

    # ── FC = device says "no data for this command" ──────────────────────────
    if len(p) == 1 and p[0] == 0xFC:
        if raw:
            print(f"[{ts()}]  {dt:04X}  no data (FC)")
        return

    # ── Real-time streaming responses ────────────────────────────────────────
    if dt == RT_HR and p:
        print(f"[{ts()}]  HR    {p[0]:3} BPM")
    elif dt == RT_BLOOD and len(p) >= 2:
        hr = f"  HR {p[2]} BPM" if len(p) > 2 and p[2] else ""
        print(f"[{ts()}]  BP    {p[0]}/{p[1]} mmHg{hr}")
    elif dt == RT_SPO2 and p:
        print(f"[{ts()}]  SpO2  {p[0]} %")
    elif dt == RT_COMP and len(p) >= 11:
        print(f"[{ts()}]  COMP  HR={p[7]}  BP={p[8]}/{p[9]}  SpO2={p[10]}")
    elif dt == RT_BPDONE and len(p) >= 3:
        if p[0] == 0:
            print(f"[{ts()}]  BP    {p[1]}/{p[2]} mmHg  (live 0x0410)")
        else:
            print(f"[{ts()}]  BP    FAIL status={p[0]}")
    elif dt == RT_DONE and len(p) >= 2:
        st = {1: "OK", 2: "FAIL", 3: "CANCEL"}.get(p[1], f"?{p[1]}")
        print(f"[{ts()}]  DONE  type={p[0]} {st}")

    # ── Health history: headers & end markers (silent unless --raw) ──────────
    elif dt in (0x0506, 0x0508, 0x0509, 0x0580):
        if raw and len(p) >= 2:
            count = struct.unpack_from("<H", p, 0)[0]
            label = {0x0506: "HR-hdr", 0x0508: "BP-hdr",
                     0x0509: "All-hdr", 0x0580: "xfer-end"}.get(dt, f"{dt:04X}")
            print(f"[{ts()}]  {label}  count/status={count}  raw={p.hex()}")

    # ── Health_HistoryBlood data  (g=5 k=0x17) ───────────────────────────────
    # Record: [ts_yc_le(4), status(1), SBP(1), DBP(1), HR(1)] = 8 bytes
    elif dt == 0x0517:
        for i in range(0, len(p) - 6, 8):
            ts_yc = struct.unpack_from("<I", p, i)[0]
            sbp, dbp, hr_v = p[i + 5], p[i + 6], p[i + 7]
            print(f"[{ts()}]  BP    {sbp}/{dbp} mmHg  HR {hr_v} BPM  @ {yc_dt(ts_yc)}")

    # ── Health_HistoryHeart data  (g=5 k=0x15) ───────────────────────────────
    # Record: [ts_yc_le(4), status(1), HR(1)] = 6 bytes
    elif dt == 0x0515:
        for i in range(0, len(p) - 4, 6):
            ts_yc = struct.unpack_from("<I", p, i)[0]
            hr_v = p[i + 5]
            print(f"[{ts()}]  HR    {hr_v:3} BPM  @ {yc_dt(ts_yc)}")

    # ── Health_HistoryAll data  (g=5 k=0x18) ─────────────────────────────────
    # Record: [ts_yc_le(4), ?(1), ?(1), HR(1), SBP(1), DBP(1), SpO2(1), ...]
    elif dt == 0x0518:
        rec = 20
        for i in range(0, len(p) - (rec - 1), rec):
            ts_yc = struct.unpack_from("<I", p, i)[0]
            hr_v  = p[i + 6]
            sbp   = p[i + 7]
            dbp   = p[i + 8]
            spo2  = p[i + 9]
            temp  = p[i + 11] if len(p) > i + 11 else 0
            parts = [f"BP {sbp}/{dbp} mmHg", f"HR {hr_v} BPM"]
            if spo2:  parts.append(f"SpO2 {spo2}%")
            if temp:  parts.append(f"Temp {temp}°C")
            print(f"[{ts()}]  ALL   {'  '.join(parts)}  @ {yc_dt(ts_yc)}")

    else:
        # Unknown frame — always print so nothing is silently lost
        print(f"[{ts()}]  RAW   g={g:02X} k={k:02X}  {p.hex(' ').upper() or '(empty)'}")


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mac", nargs="?", default=DEFAULT_MAC)
    ap.add_argument("--hr",  action="store_true", help="HR only")
    ap.add_argument("--bp",  action="store_true", help="BP only")
    ap.add_argument("--raw", action="store_true", help="dump all raw frames")
    args = ap.parse_args()

    mac = args.mac
    if mac == DEFAULT_MAC and len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        mac = sys.argv[1]

    # Determine which commands to send
    if args.hr:
        cmds_on  = [(DT_HEART_ON, b"\x01"), (DT_REAL_STREAM, b"\x01")]
        cmds_off = [(DT_REAL_STREAM, b"\x00"), (DT_HEART_ON, b"\x00")]
        mode = "HR only"
    elif args.bp:
        # AppStartBloodMeasurement payload: [start, sbp, dbp, heart, cm, kg, age, sex]
        cmds_on  = [
            (DT_BLOOD_ON,    b"\x01"),
            (DT_REAL_STREAM, b"\x01"),
            (DT_BP_MEASURE2, bytes([1, 115, 80, 70, 170, 70, 30, 0])),
        ]
        cmds_off = [
            (DT_BP_MEASURE2, bytes([0, 115, 80, 70, 170, 70, 30, 0])),
            (DT_REAL_STREAM, b"\x00"),
            (DT_BLOOD_ON,    b"\x00"),
        ]
        mode = "BP only"
    else:
        # Default: all real-time metrics
        cmds_on  = [
            (DT_HEART_ON,    b"\x01"),
            (DT_BLOOD_ON,    b"\x01"),
            (DT_BLOOD_CHECK, b"\x01"),
            (DT_REAL_STREAM, b"\x01"),
        ]
        cmds_off = [
            (DT_REAL_STREAM, b"\x00"),
            (DT_BLOOD_CHECK, b"\x00"),
            (DT_BLOOD_ON,    b"\x00"),
            (DT_HEART_ON,    b"\x00"),
        ]
        mode = "all metrics"

    print(f"Connecting to {mac}  [{mode}]...")

    async with BleakClient(mac, timeout=15.0) as client:
        print(f"Connected.\n")

        all_chars = {c.uuid: c for svc in client.services for c in svc.characteristics}

        # Static info
        for uuid, label in [(MANUFACTURER, "Manufacturer"), (BAT_LEVEL, "Battery   ")]:
            if uuid in all_chars:
                try:
                    d = await client.read_gatt_char(uuid)
                    val = d.decode("utf-8", errors="replace") if uuid == MANUFACTURER else f"{d[0]} %"
                    print(f"  {label}: {val}")
                except Exception:
                    pass

        if YC_WRITE not in all_chars:
            print("ERROR: be940001 not found — is the device paired?")
            return

        # Subscribe to both indication chars (both can carry device→app frames)
        def on_data(_, data: bytearray) -> None:
            pkt = parse(bytes(data))
            if pkt:
                show(pkt, args.raw)
            elif args.raw:
                print(f"[{ts()}]  MALFORMED  {bytes(data).hex(' ').upper()}")

        await client.start_notify(YC_WRITE,  on_data)   # be940001 indicate
        await client.start_notify(YC_NOTIFY, on_data)   # be940003 indicate

        # Subscribe to standard GATT HR as well (passive fallback)
        if HR_MEAS in all_chars:
            def on_gatt_hr(_, data: bytearray) -> None:
                flags = data[0]
                hr = struct.unpack_from("<H", data, 1)[0] if flags & 1 else data[1]
                print(f"[{ts()}]  GATT-HR  {hr:3} BPM")
            await client.start_notify(HR_MEAS, on_gatt_hr)

        # Sync device clock
        epoch_yc = int(time.time()) - 946684800   # YC epoch starts 2000-01-01
        await client.write_gatt_char(YC_WRITE, frame(DT_TIME_SYNC, struct.pack("<I", epoch_yc)))

        # Send measurement start commands
        for dt, payload in cmds_on:
            await client.write_gatt_char(YC_WRITE, frame(dt, payload))
            await asyncio.sleep(0.05)

        print(f"\n── Streaming {mode} (Ctrl+C to stop) ──────────────────────────\n")

        poll = [
            frame(DT_GET_REAL,     b""),   # 0x0220 GetAllRealDataFromDevice
            frame(DT_GET_SPO2,     b""),   # 0x0211 GetRealBloodOxygen
            frame(DT_HEALTH_BLOOD, b""),   # 0x0508 Health_HistoryBlood
            frame(DT_HEALTH_HEART, b""),   # 0x0506 Health_HistoryHeart
            frame(DT_HEALTH_ALL,   b""),   # 0x0509 Health_HistoryAll
            frame(DT_HEALTH_SPO2,  b""),   # 0x051A Health_HistoryBloodOxygen
            frame(DT_HEALTH_COMP,  b""),   # 0x052F Health_HistoryComprehensiveMeasureData
        ]

        keepalive = [frame(dt, pl) for dt, pl in cmds_on]
        REARM_INTERVAL = 60.0  # seconds
        last_rearm = 0.0

        try:
            while True:
                now = time.monotonic()
                if now - last_rearm >= REARM_INTERVAL:
                    print(f"[{ts()}]  >> re-arming measurements")
                    for cmd in keepalive:
                        await client.write_gatt_char(YC_WRITE, cmd)
                        await asyncio.sleep(0.05)
                    last_rearm = time.monotonic()
                for cmd in poll:
                    await client.write_gatt_char(YC_WRITE, cmd)
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.3)
        except KeyboardInterrupt:
            print("\nStopping...")
            for dt, payload in cmds_off:
                try:
                    await client.write_gatt_char(YC_WRITE, frame(dt, payload))
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
