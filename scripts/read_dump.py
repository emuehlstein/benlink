#!/usr/bin/env python3
"""Read-only dump of a Benshi radio (VR-N76). No writes, no region walking.

Captures device info, status, settings, beacon settings, battery, position,
region names, and the channels of the CURRENT region only.

Writes a human-readable .txt and a machine-readable .json to ~/src/n76/backups/.
"""
import asyncio
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

from bleak import BleakScanner
import benlink.controller as bc

OUT_DIR = Path.home() / "src" / "n76" / "backups"


def jsonable(obj):
    """Best-effort conversion of benlink dataclasses/enums to plain JSON."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name, None))
                for f in dataclasses.fields(obj)}
    if hasattr(obj, "name") and hasattr(obj, "value"):  # enum
        return obj.name
    if hasattr(obj, "__dict__"):
        return {k: jsonable(v) for k, v in vars(obj).items()
                if not k.startswith("_")}
    return repr(obj)


async def find_radio(timeout: float = 20.0) -> str:
    print(f"scanning up to {timeout}s for VR-N76...", flush=True)
    found = asyncio.get_running_loop().create_future()

    def cb(d, adv):
        nm = d.name or adv.local_name or ""
        if "VR-N76" in nm.upper() and not found.done():
            print(f"  found {nm!r} @ {d.address} rssi={adv.rssi}", flush=True)
            found.set_result(d.address)

    async with BleakScanner(detection_callback=cb):
        try:
            return await asyncio.wait_for(asyncio.shield(found), timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("VR-N76 not found — check BT enabled on radio")


def grab(radio, attr):
    """Read a hydrated property off the controller."""
    try:
        return getattr(radio, attr)
    except Exception as e:  # noqa: BLE001
        return f"<unavailable: {type(e).__name__}: {e}>"


async def grab_async(radio, attr):
    """Call an async accessor.

    battery_level, battery_voltage, battery_level_as_percentage and position
    are coroutine methods, not properties: a bare getattr returns the bound
    method, which serializes to {} and silently looks like an empty read.
    """
    try:
        return await getattr(radio, attr)()
    except Exception as e:  # noqa: BLE001
        return f"<unavailable: {type(e).__name__}: {e}>"


async def capture_and_save(radio, address: str) -> None:
    """Pull every read-only surface off an already-hydrated controller."""
    snap = {
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "ble_address": address,
        "device_info": grab(radio, "device_info"),
        "status": grab(radio, "status"),
        "settings": grab(radio, "settings"),
        "beacon_settings": grab(radio, "beacon_settings"),
        "battery_level": await grab_async(radio, "battery_level"),
        "battery_voltage": await grab_async(radio, "battery_voltage"),
        "battery_pct": await grab_async(
            radio, "battery_level_as_percentage"
        ),
        "region_names": grab(radio, "region_names"),
        "position": await grab_async(radio, "position"),
        "channels_current_region": grab(radio, "channels"),
    }

    data = jsonable(snap)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = OUT_DIR / f"n76-read-{stamp}.json"
    txt_path = OUT_DIR / f"n76-read-{stamp}.txt"

    json_path.write_text(json.dumps(data, indent=2, sort_keys=False))

    lines: list[str] = []
    lines.append(f"VR-N76 read-only dump — {data['captured_at']}")
    lines.append(f"BLE: {address}")
    lines.append("")
    for key in ("device_info", "status", "settings", "beacon_settings", "position"):
        lines.append(f"=== {key} ===")
        val = data.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                lines.append(f"  {k:<28} {v}")
        else:
            lines.append(f"  {val}")
        lines.append("")

    lines.append("=== battery ===")
    lines.append(f"  level   {data.get('battery_level')}")
    lines.append(f"  volts   {data.get('battery_voltage')}")
    lines.append(f"  percent {data.get('battery_pct')}")
    lines.append("")

    lines.append("=== region names ===")
    rn = data.get("region_names")
    if isinstance(rn, list):
        for i, n in enumerate(rn):
            lines.append(f"  [{i:>2}] {n!r}")
    else:
        lines.append(f"  {rn}")
    lines.append("")

    cur = data.get("status", {})
    cur_region = cur.get("curr_region") if isinstance(cur, dict) else "?"
    lines.append(f"=== channels (current region {cur_region}) ===")
    chans = data.get("channels_current_region")
    if isinstance(chans, list):
        for i, ch in enumerate(chans):
            if not isinstance(ch, dict):
                lines.append(f"  ch{i+1:>2}: {ch}")
                continue
            nm = ch.get("name", "")
            rx = ch.get("rx_freq", 0) or 0
            tx = ch.get("tx_freq", 0) or 0
            if not nm and not rx and not tx:
                continue
            lines.append(
                f"  ch{i+1:>2}: name={nm!r:<16} rx={rx:>10} tx={tx:>10} "
                f"bw={ch.get('bandwidth')} txT={ch.get('tx_sub_audio')} "
                f"rxT={ch.get('rx_sub_audio')} scan={ch.get('scan')} "
                f"txdis={ch.get('tx_disable')}"
            )
    else:
        lines.append(f"  {chans}")

    body = "\n".join(lines)
    txt_path.write_text(body + "\n")

    print()
    print(body)
    print()
    print(f"saved: {txt_path}")
    print(f"saved: {json_path}")


async def main() -> None:
    address = await find_radio()
    async with bc.RadioController.new_ble(address) as radio:
        print("connected", flush=True)
        await capture_and_save(radio, address)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
