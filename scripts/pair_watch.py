#!/usr/bin/env python3
"""Continuously watch for the VR-N76 and pounce the moment it advertises.

The radio's pairing mode times out after ~30-60s, and macOS only initiates
bonding when something touches an encrypted characteristic. So we keep a
scanner running and attempt connect+hydrate within milliseconds of the first
advertisement, while the radio is still pairable.

Runs until hydration succeeds (bond established) or the deadline passes.
"""
import asyncio
import datetime as dt
import sys

from bleak import BleakScanner
import benlink.controller as bc

DEADLINE_MIN = 15.0


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


async def wait_for_advert(timeout: float) -> str | None:
    """Return the address as soon as VR-N76 is seen, else None on timeout."""
    loop = asyncio.get_running_loop()
    found: asyncio.Future[str] = loop.create_future()

    def cb(d, adv):
        nm = d.name or adv.local_name or ""
        if "VR-N76" in nm.upper() and not found.done():
            log(f"ADVERT seen: {nm!r} @ {d.address} rssi={adv.rssi}")
            found.set_result(d.address)

    async with BleakScanner(detection_callback=cb):
        try:
            return await asyncio.wait_for(asyncio.shield(found), timeout)
        except asyncio.TimeoutError:
            return None


async def try_bond(address: str) -> bool:
    """Attempt connect + hydrate. Success means the link is encrypted/bonded."""
    try:
        async with bc.RadioController.new_ble(address) as radio:
            log("*** HYDRATED — bond established ***")
            log(f"    device_info: {radio.device_info}")
            return True
    except Exception as e:  # noqa: BLE001
        log(f"    connect failed: {type(e).__name__}: {e}")
        return False


async def main() -> int:
    deadline = dt.datetime.now() + dt.timedelta(minutes=DEADLINE_MIN)
    log(f"watching for VR-N76 until {deadline:%H:%M:%S} "
        f"— put the radio in pairing mode now")

    attempt = 0
    while dt.datetime.now() < deadline:
        remaining = (deadline - dt.datetime.now()).total_seconds()
        addr = await wait_for_advert(min(30.0, remaining))
        if addr is None:
            log("no advert yet...")
            continue

        attempt += 1
        log(f"pouncing (attempt {attempt})")
        if await try_bond(addr):
            log("BONDED — rerun scripts/read_dump.py to pull the config")
            return 0
        await asyncio.sleep(2)

    log("deadline passed without a bond")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
