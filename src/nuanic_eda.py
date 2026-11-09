"""Nuanic EDA ring recorder (BLE) to data/eda/<session_id>_eda.csv.

Modified 2026-09 (iteration 5): file named from the shared session id; stops on the
orchestrator's stop signal so the buffered samples are written instead of lost.
"""

import asyncio
import struct
import csv
import time
import sys
import threading
from datetime import datetime
from pathlib import Path
from bleak import BleakClient, BleakScanner

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session


NUANIC_SERVICE = "5491faaf-b0c2-4167-8f3d-bc6b31db69e7"
UUID_REALTIME  = "dc9c31a7-fbd3-467a-8777-10900c423d3b"
UUID_LIVE_EDA  = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
UUID_STATE     = "3c180fcc-bfec-4b7c-8e52-1a37f123e449"
UUID_LIVE_DNE  = "d306262b-c8c9-4c4b-9050-3a41dea706e5"
UUID_SAMPLE_RATE = "516b0fb6-d861-4619-9dd0-0105e8b85128"
STATES         = {0: "Init", 1: "Off finger", 2: "On finger", 3: "Docked"}
SAMPLE_RATE = 16 
OUTPUT_DIR     = "data/eda"


records    = []
dne_buffer = {}
stop_flag  = False
start_unix_time = int(time.time() * 1000)


def ohm_to_us(ohm):
    return round((1 / ohm) * 1_000_000, 4) if ohm != 0 else 0.0

def log(msg):
    print(f"[nuanic_eda] {msg}", flush=True)


def on_eda(sender, data: bytearray):
    if stop_flag:
        return
    try:
        boot_count = struct.unpack_from("<H", data, 0)[0]
        timestamp  = struct.unpack_from("<Q", data, 2)[0]
        eda_ohm    = struct.unpack_from("<I", data, 10)[0]
        eda_us     = ohm_to_us(eda_ohm)
        dt         = datetime.fromtimestamp(timestamp / 1000)
        dne        = dne_buffer.get("dne")
        instant    = dne_buffer.get("instant")
        records.append({
            "datetime":   dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "unix_time":  timestamp,
            "eda_ohm":    eda_ohm,
            "eda_us":     eda_us,
            "boot_count": boot_count,
            "dne":        dne,
            "instant":    instant,
        })
        log(f"{dt.strftime('%H:%M:%S.%f')[:-3]}  |  {eda_ohm} Ω  |  {eda_us} µS  |  DNE: {dne}")
    except Exception as e:
        log(f"Parse error: {e}")



def on_dne(sender, data: bytearray):
    if stop_flag:
        return
    try:
        instant = struct.unpack_from("<i", data, 8)[0]
        dne     = struct.unpack_from("<i", data, 12)[0]
        dne_buffer["dne"] = dne
        dne_buffer["instant"] = instant
    except Exception as e:
        print(f" DNE parse error:{e}")


def save_eda_data():
    if not records:
        log("No EDA data collected.")
        return

    path = str(session.stream_path("eda"))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    log(f"Saved {len(records)} records → {path}")


def stop_recording():
    global stop_flag
    log("Stop requested — stopping EDA recording...")
    stop_flag = True


def listen_for_hotkey():
    """Poll the shared stop signal (stop file, or F12 for a manual abort).

    This is what makes EDA survive. Everything collected lives in `records`
    in memory until save_eda_data() runs, so the process must be allowed to
    reach that call rather than being killed.
    """
    while not stop_flag:
        if session.stop_requested():
            stop_recording()
            return
        time.sleep(0.25)


async def find_nuanic():
    log("Scanning for Nuanic ring (10 seconds)...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for d, adv in devices.values():
        name_match    = d.name and "nuanic" in d.name.lower()
        service_match = NUANIC_SERVICE.lower() in [s.lower() for s in adv.service_uuids]
        if name_match or service_match:
            log(f"Found: {d.name} — {d.address}")
            return d
    return None


async def run():
    log(f"Script started at: {start_unix_time} (Unix time: {start_unix_time})")

    threading.Thread(target=listen_for_hotkey, daemon=True).start()

    ring = None
    while not ring and not stop_flag:
        ring = await find_nuanic()
        if not ring:
            log("Ring not found — retrying in 3 seconds...")
            await asyncio.sleep(3)

    if stop_flag:
        log("Stopped before ring was found.")
        return

    log(f"Connecting to {ring.address}...")
    try:
        async with BleakClient(ring, timeout=20.0) as client:
            log("Connected!")

            await client.write_gatt_char(
                UUID_REALTIME,
                struct.pack("<Q", int(time.time() * 1000))
            )

            state_b = await client.read_gatt_char(UUID_STATE)
            state   = STATES.get(state_b[0], "Unknown")
            log(f"Ring state: {state}")
            if state_b[0] != 2:
                log("Warning: ring not on finger — data may be unreliable.")

            await client.start_notify(UUID_LIVE_EDA, on_eda)
            await client.start_notify(UUID_LIVE_DNE, on_dne)
            log("Streaming EDA. Press F12 to stop.")

            while not stop_flag:
                await asyncio.sleep(0.1)

            log("Stopping stream...")
            await client.stop_notify(UUID_LIVE_EDA)
            await client.stop_notify(UUID_LIVE_DNE)

    except Exception as e:
        log(f"Connection error: {e}")

    save_eda_data()
    log("Done.")

if __name__ == "__main__":
    asyncio.run(run())