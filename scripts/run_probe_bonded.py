#!/usr/bin/env python3
"""Bond the N76, then run the Tier 3.1 SET_REGION probe in the same session.

Why this exists: on macOS the radio must be bonded before benlink can write to
the control characteristic, and bonding only succeeds while the radio is in
pairing mode (~30-60s window) from a FOREGROUND app that holds the per-process
Bluetooth grant. Running the probe as a bare one-shot loses that race and dies
with `Insufficient Encryption` / `N76 not found`.

This does the bond coaxing first, then hands the live address straight to the
existing probe's main() so we reuse its exact logic and verdict output.

Answers the open question on khusmann/benlink PR #28: does SET_REGION (60)
emit a reply frame, or is it fire-and-forget as HTCommander reports?
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ble_bond_then_dump import coax_bond, log  # noqa: E402
from read_dump import find_radio  # noqa: E402


async def main() -> int:
    address = await find_radio(timeout=10.0)

    if not await coax_bond(address):
        log("no bond established — was the radio in pairing mode?")
        return 1

    log("bond OK — starting SET_REGION probe")
    print("=" * 60, flush=True)

    # Import after bonding so the probe's teelog starts at probe time, and
    # feed it the address we already resolved instead of letting it re-scan.
    import t3_1_set_region_probe as probe

    async def _resolved(timeout: float = 12.0) -> str:
        return address

    probe.find_radio = _resolved  # type: ignore[assignment]
    await probe.main()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
