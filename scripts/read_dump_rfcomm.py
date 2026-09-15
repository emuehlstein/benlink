#!/usr/bin/env python3
"""Read-only dump of a Benshi radio over RFCOMM (Linux only).

Companion to read_dump.py, which uses BLE. On Linux the classic RFCOMM
transport is usually the better path: bonding is scriptable via bluetoothctl,
there is no per-process Bluetooth grant, and a classic bond does not suppress
the transport the way it does on macOS.

Requires socket.AF_BLUETOOTH / BTPROTO_RFCOMM (absent on macOS) and, for
channel="auto", sdptool from bluez-utils.

Usage:
    python scripts/read_dump_rfcomm.py [MAC] [CHANNEL]

MAC defaults to $BENLINK_N76_MAC. CHANNEL defaults to "auto" (SDP lookup).
"""
import asyncio
import os
import socket
import sys
from pathlib import Path

import benlink.controller as bc

sys.path.insert(0, str(Path(__file__).parent))
from read_dump import capture_and_save  # noqa: E402


async def main() -> int:
    if not hasattr(socket, "BTPROTO_RFCOMM"):
        print("RFCOMM unavailable: this CPython has no Bluetooth socket "
              "support (expected on macOS). Use scripts/read_dump.py "
              "over BLE instead.", file=sys.stderr)
        return 2

    mac = (sys.argv[1] if len(sys.argv) > 1
           else os.environ.get("BENLINK_N76_MAC", ""))
    if not mac:
        print("need a MAC: argv[1] or $BENLINK_N76_MAC", file=sys.stderr)
        return 2

    raw_channel = sys.argv[2] if len(sys.argv) > 2 else "auto"
    channel: "int | str" = (
        int(raw_channel) if raw_channel.isdigit() else raw_channel
    )

    print(f"connecting RFCOMM to {mac} channel={channel}", flush=True)
    async with bc.RadioController.new_rfcomm(mac, channel=channel) as radio:
        print("connected + hydrated", flush=True)
        await capture_and_save(radio, mac)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
