#!/usr/bin/env python3
"""Force the macOS LE auto-pair prompt, wait for the human to accept, then dump.

CoreBluetooth has no explicit pairing API: it auto-pairs when a client touches
a characteristic that requires encryption. benlink's first hydrate write does
exactly that, but the script normally dies on the resulting
`Insufficient Encryption` error before the human can hit "Pair".

This holds a connection open and retries the encrypted write on a slow loop,
so the OS prompt stays reachable. Once the write lands (bond complete), it
hands off to the same read-only capture as scripts/read_dump.py.
"""
import asyncio
import datetime as dt
import sys
from pathlib import Path

from bleak import BleakScanner, BleakClient
import benlink.controller as bc

sys.path.insert(0, str(Path(__file__).parent))
from read_dump import find_radio, capture_and_save  # noqa: E402

RADIO_WRITE_UUID = "00001101-d102-11e1-9b23-00025b00a5a5"
# benlink's own GetDeviceInfo frame -- a read-only command, the exact bytes
# RadioController._hydrate() sends first. Safe to replay as a bond probe.
GET_DEVICE_INFO = bytes.fromhex("0002000403")
ATTEMPT_WINDOW_MIN = 5.0


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


async def coax_bond(address: str) -> bool:
    """Hold a connection and poke the encrypted char until the bond takes."""
    deadline = dt.datetime.now() + dt.timedelta(minutes=ATTEMPT_WINDOW_MIN)
    attempt = 0
    while dt.datetime.now() < deadline:
        attempt += 1
        try:
            async with BleakClient(address, timeout=20.0) as client:
                log(f"attempt {attempt}: connected, poking encrypted char "
                    f"— ACCEPT THE PAIRING PROMPT ON SCREEN")
                for poke in range(24):
                    try:
                        # The write characteristic is the only one that
                        # requires encryption, so this is what actually
                        # triggers the macOS auto-pair prompt.
                        await client.write_gatt_char(
                            RADIO_WRITE_UUID, GET_DEVICE_INFO, response=True
                        )
                        log("    encrypted write ACCEPTED — bond established")
                        return True
                    except Exception as e:  # noqa: BLE001
                        name = type(e).__name__
                        if poke == 0 or poke % 4 == 0:
                            log(f"    poke {poke}: {name}: {e}")
                        await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001
            log(f"attempt {attempt}: connect failed: {type(e).__name__}: {e}")
            await asyncio.sleep(3)

    return False


async def main() -> int:
    # Short scan: pairing mode is already active by the time we get here,
    # so every second spent scanning is a second of that window burned.
    address = await find_radio(timeout=8.0)

    if not await coax_bond(address):
        log("no bond within the window — is the radio still in pairing mode?")
        return 1

    log("bond in place; pulling read-only dump")
    async with bc.RadioController.new_ble(address) as radio:
        log(f"hydrated: fw={radio.device_info.firmware_version}")
        await capture_and_save(radio, address)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
