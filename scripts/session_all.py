#!/usr/bin/env python3
"""Do ALL outstanding N76 radio work in ONE connection.

Connections to this radio are expensive and scarce: on macOS each bond costs a
Forget-This-Device + re-pair cycle, and on Linux the radio appears to accept
only one RFCOMM session per Bluetooth-enable cycle. Running one script per
question wastes a whole session each time, so this batches everything:

  1. read-only config dump           (device info, settings, beacon, battery)
  2. SET_REGION reply-shape probe    (khusmann/benlink PR #28 open question)
  3. full region survey              (channel table for every region)

Transport-agnostic:
    python scripts/session_all.py --transport rfcomm --mac AA:BB:.. --channel 1
    python scripts/session_all.py --transport ble                   # scans

Steps 2 and 3 switch regions via SET_REGION and restore the original region
afterwards, then verify the baseline channel snapshot is unchanged. Use
--read-only to run step 1 alone with no writes at all.
"""
import argparse
import asyncio
import datetime as dt
import json
import socket
import sys
import time
from pathlib import Path

import benlink.controller as bc
import benlink.protocol as p
from benlink.command import UnknownProtocolMessage

sys.path.insert(0, str(Path(__file__).parent))
from read_dump import OUT_DIR, capture_and_save, jsonable  # noqa: E402
from t3_1_set_region_probe import (  # noqa: E402
    diff_snapshots,
    send_set_region,
    snapshot_channels,
    wait_curr_region,
)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def banner(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}", flush=True)


def probe_rfcomm_channels(mac: str, hi: int = 12) -> list:
    """Raw-socket scan for channels with a live RFCOMM listener.

    Needed because this radio allocates its SPP channel dynamically (observed
    on ch1, then ch4/ch5 minutes later) and `sdptool browse` returns nothing
    for it, so benlink's channel="auto" SDP lookup hard-fails.
    """
    found = []
    for ch in range(1, hi + 1):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                          socket.BTPROTO_RFCOMM)
        s.settimeout(6)
        try:
            s.connect((mac, ch))
            found.append(ch)
            log(f"  ch{ch}: OPEN")
        except Exception as e:
            log(f"  ch{ch}: {type(e).__name__}")
        finally:
            s.close()
    return found


async def open_radio(args):
    if args.transport == "rfcomm":
        if not args.mac:
            raise SystemExit("--mac is required for rfcomm")
        if str(args.channel) == "probe":
            log("probing for a live RFCOMM channel")
            candidates = probe_rfcomm_channels(args.mac)
            if not candidates:
                raise SystemExit("no open RFCOMM channel; toggle the radio's "
                                 "Bluetooth off/on and retry")
            log(f"open channels: {candidates}")
            for ch in candidates:
                try:
                    ctl = bc.RadioController.new_rfcomm(args.mac, channel=ch)
                    await ctl.connect()
                    log(f"hydrated on channel {ch}")
                    return ctl
                except Exception as e:
                    log(f"  ch{ch} hydrate failed: {type(e).__name__}: {e}")
            raise SystemExit("open channels found but none hydrated")
        ch = int(args.channel) if str(args.channel).isdigit() else args.channel
        log(f"RFCOMM {args.mac} channel={ch}")
        return bc.RadioController.new_rfcomm(args.mac, channel=ch)
    addr = args.mac
    if not addr:
        from read_dump import find_radio
        addr = await find_radio(timeout=12.0)
    log(f"BLE {addr}")
    return bc.RadioController.new_ble(addr)


async def probe_set_region(radio, baseline_region: int, n_regions: int):
    """Does SET_REGION emit a reply frame? Capture every raw frame to find out."""
    banner("STEP 2 — SET_REGION reply-shape probe (PR #28)")

    reply_events = []

    def raw_handler(radio_msg):
        if isinstance(radio_msg, UnknownProtocolMessage):
            return
        msg = getattr(radio_msg, "message", radio_msg)
        cmd = getattr(msg, "command", None)
        if cmd is None:
            return
        reply_events.append((time.monotonic(), msg))
        body = getattr(msg, "body", b"")
        body_hex = body.hex() if isinstance(body, (bytes, bytearray)) else repr(body)
        grp = getattr(getattr(msg, "command_group", None), "name", "?")
        log(f"    <- {grp} {getattr(cmd, 'name', cmd)} "
            f"is_reply={getattr(msg, 'is_reply', '?')} body={body_hex}")

    remove_raw = radio._conn._add_message_handler(raw_handler)
    try:
        target = (baseline_region + 1) % max(n_regions, 1)
        log(f"baseline region {baseline_region}; probing switch to {target}")

        reply_events.clear()
        await send_set_region(radio, target)
        await asyncio.sleep(2.0)
        switched = await wait_curr_region(radio, target, timeout_s=4.0)
        log(f"region switch observed: {switched}")

        log(f"restoring baseline region {baseline_region}")
        await send_set_region(radio, baseline_region)
        await asyncio.sleep(2.0)
        restored = await wait_curr_region(radio, baseline_region, timeout_s=4.0)
        log(f"baseline restored: {restored}")

        set_region_replies = [
            (t, m) for (t, m) in reply_events
            if getattr(m, "command", None) == p.BasicCommand.SET_REGION
            and getattr(m, "is_reply", False)
        ]
        print(flush=True)
        log(f"VERDICT — SET_REGION reply frames observed: "
            f"{len(set_region_replies)}")
        for _t, m in set_region_replies:
            body = getattr(m, "body", b"")
            log(f"  reply body: "
                f"{body.hex() if isinstance(body, (bytes, bytearray)) else body!r}")
        if not set_region_replies:
            log("  => fire-and-forget; matches HTCommander / jasonhuber")
        return {
            "set_region_reply_count": len(set_region_replies),
            "switch_observed": switched,
            "baseline_restored": restored,
            "all_frames": [
                {
                    "command": getattr(getattr(m, "command", None), "name", "?"),
                    "group": getattr(getattr(m, "command_group", None), "name", "?"),
                    "is_reply": getattr(m, "is_reply", None),
                    "body": (m.body.hex()
                             if isinstance(getattr(m, "body", None), (bytes, bytearray))
                             else None),
                }
                for _t, m in reply_events
            ],
        }
    finally:
        remove_raw()


async def survey_regions(radio, baseline_region: int, n_regions: int):
    """Walk every region and capture its channel table, then restore."""
    banner("STEP 3 — full region survey")
    out = {}
    try:
        for rid in range(n_regions):
            if not await wait_curr_region(radio, rid, timeout_s=0.1):
                await send_set_region(radio, rid)
                await asyncio.sleep(1.5)
                if not await wait_curr_region(radio, rid, timeout_s=4.0):
                    log(f"region {rid}: switch FAILED, skipping")
                    out[rid] = {"error": "switch failed"}
                    continue
            snap = await snapshot_channels(radio)
            data = jsonable(snap)
            # snapshot_channels yields (name, rx, tx, bandwidth) tuples, not
            # objects -- getattr(c, "name") silently misses every entry.
            named = [v for v in (data.values() if isinstance(data, dict)
                                 else data)
                     if isinstance(v, (list, tuple)) and v and v[0]]
            log(f"region {rid}: {len(named)} named channels")
            out[rid] = data
    finally:
        log(f"restoring baseline region {baseline_region}")
        await send_set_region(radio, baseline_region)
        await asyncio.sleep(1.5)
        ok = await wait_curr_region(radio, baseline_region, timeout_s=5.0)
        log(f"baseline restored: {ok}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=("ble", "rfcomm"), default="rfcomm")
    ap.add_argument("--mac", default="")
    ap.add_argument("--channel", default="1")
    ap.add_argument("--read-only", action="store_true",
                    help="step 1 only; no SET_REGION writes")
    args = ap.parse_args()

    ctl = await open_radio(args)
    # open_radio may already have connected (channel probing needs to hydrate
    # to confirm a channel), so connect only when it did not.
    if not ctl.is_connected:
        await ctl.connect()
    try:
        radio = ctl
        log("connected + hydrated")
        info = radio.device_info
        status = radio.status
        n_regions = getattr(info, "region_count", 0) or 0
        baseline = status.curr_region
        log(f"fw={info.firmware_version} regions={n_regions} "
            f"current_region={baseline}")

        banner("STEP 1 — read-only config dump")
        baseline_snapshot = await snapshot_channels(radio)
        await capture_and_save(radio, args.mac or "ble")

        if args.read_only:
            log("--read-only set; skipping steps 2 and 3")
            return 0

        probe_result = await probe_set_region(radio, baseline, n_regions)
        regions = await survey_regions(radio, baseline, n_regions)

        banner("INTEGRITY CHECK")
        final_snapshot = await snapshot_channels(radio)
        drift = diff_snapshots(baseline_snapshot, final_snapshot)
        if drift:
            log(f"!! {len(drift)} channel(s) DRIFTED from baseline")
            for d in drift[:10]:
                log(f"   {d}")
        else:
            log("no channel drift — radio state matches baseline")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = OUT_DIR / f"n76-session-{stamp}.json"
        path.write_text(json.dumps({
            "captured_at": dt.datetime.now().astimezone().isoformat(),
            "transport": args.transport,
            "firmware": info.firmware_version,
            "baseline_region": baseline,
            "region_count": n_regions,
            "set_region_probe": probe_result,
            "regions": regions,
            "channel_drift": jsonable(drift),
        }, indent=2))
        log(f"saved: {path}")
    finally:
        await ctl.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
