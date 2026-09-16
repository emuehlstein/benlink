#!/usr/bin/env python3
"""Apply a declarative codeplug plan to a Benshi radio (VR-N76 and friends).

This is the generic successor to ``t3_region_setup_apply.py``, which hard-coded
one person's channel list in Python. Everything that varies between radios,
owners, and revisions lives in a JSON plan file instead, so the tool can be
driven by a generator (codeplugger) or hand-written by a human.

Plan format is documented in ``docs/codeplug-plan.md`` (``benlink-codeplug``
version 1). Short version::

    {
      "format": "benlink-codeplug",
      "version": 1,
      "radio": {"model": "vero_vrn76", "vendor_id": 1, "product_id": 259},
      "regions": [
        {"index": 0, "name": "Ham", "blank_unlisted": true,
         "channels": [
           {"slot": 1, "name": "2m Call", "rx_mhz": 146.52, "tx_mhz": 146.52,
            "bandwidth": "WIDE", "tone": null, "scan": true}
         ]}
      ]
    }

Safety rules this tool enforces, in order:

1. The plan's ``vendor_id``/``product_id`` must match what the connected radio
   reports. This is the only thing standing between a GMRS plan and somebody
   else's Benshi HT, so a mismatch is a hard abort, not a warning.
2. ``--apply`` always takes a full backup of every region first, even when the
   plan touches one region. Rollback is worthless if it is partial.
3. After writing, the tool re-reads every slot it wrote and diffs it against
   what was requested. A write that the radio silently declined looks
   identical to a successful one on the wire; only a read-back proves it.
   (Hard-won on the DM-32UV: never trust a progress bar or a return code.)

Regions absent from the plan are never touched. Within a listed region,
``blank_unlisted`` decides whether unlisted slots get cleared or left alone;
it defaults to false so a plan can patch a couple of channels without
nuking the rest of the region.

Usage::

    # no radio needed, prints exactly what would be written
    python scripts/apply_codeplug.py --plan plan.json --dry-run

    # write, then verify (Linux)
    python scripts/apply_codeplug.py --plan plan.json --apply --rfcomm $BENLINK_N76_MAC

    # verify an earlier apply without writing anything
    python scripts/apply_codeplug.py --plan plan.json --verify --rfcomm $BENLINK_N76_MAC

    # put the radio back
    python scripts/apply_codeplug.py --rollback --rfcomm $BENLINK_N76_MAC

Transport: ``--rfcomm MAC`` is Linux-only but survives long write sessions;
``--ble`` works on macOS but needs a bond created by a foreground GUI process.
See docs/testing/N76.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

import benlink.controller as bc
from benlink.command import DCS, Channel

PLAN_FORMAT = "benlink-codeplug"
PLAN_VERSION = 1

BACKUP_DIR = Path(
    os.environ.get("BENLINK_BACKUP_DIR", Path.home() / "src" / "n76" / "backups")
)


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------
class PlanError(Exception):
    """Plan file is malformed or asks for something the radio cannot do."""


def _tone_field(spec: Any) -> Any:
    """Convert a plan tone into the benlink ``Channel`` sub-audio value.

    ``None`` means carrier squelch. ``{"ctcss": 110.9}`` becomes a float,
    ``{"dcs": 244}`` becomes ``DCS(n=244)``. A bare number is accepted as
    CTCSS because hand-written plans keep doing that.
    """
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, dict):
        if "ctcss" in spec:
            return float(spec["ctcss"])
        if "dcs" in spec:
            return DCS(n=int(spec["dcs"]))
    raise PlanError(f"unrecognized tone spec: {spec!r}")


def _tone_repr(value: Any) -> str:
    if value is None:
        return "none"
    if hasattr(value, "n"):
        return f"D{value.n}"
    return f"{float(value):.1f}"


def channel_from_spec(spec: dict) -> Channel:
    """Build a fully-specified ``Channel`` from one plan channel entry.

    Plan slots are 1-based to match the radio's own display and the Vero app;
    the wire format is 0-based, so this is where that translation happens once.
    """
    try:
        slot = int(spec["slot"])
    except (KeyError, TypeError, ValueError):
        raise PlanError(f"channel entry needs an integer 'slot': {spec!r}")

    name = str(spec.get("name", ""))
    rx = float(spec["rx_mhz"])
    tx = float(spec.get("tx_mhz", rx))

    # A single "tone" applies to both directions, which is what a repeater
    # pair normally wants. tx_tone/rx_tone override it per direction so a
    # tone-encode-only channel (common for listening to a repeater without
    # tone squelch) stays expressible.
    both = spec.get("tone", None)
    tx_tone = _tone_field(spec["tx_tone"]) if "tx_tone" in spec else _tone_field(both)
    rx_tone = _tone_field(spec["rx_tone"]) if "rx_tone" in spec else _tone_field(both)

    bandwidth = str(spec.get("bandwidth", "WIDE")).upper()
    if bandwidth in ("12.5", "NFM", "NARROW"):
        bandwidth = "NARROW"
    elif bandwidth in ("25", "25.0", "FM", "WIDE"):
        bandwidth = "WIDE"
    if bandwidth not in ("NARROW", "WIDE"):
        raise PlanError(f"slot {slot}: bandwidth must be NARROW or WIDE, got {bandwidth!r}")

    power = str(spec.get("power", "high")).lower()
    if power not in ("high", "med", "low"):
        raise PlanError(f"slot {slot}: power must be high, med or low, got {power!r}")

    modulation = str(spec.get("modulation", "FM")).upper()

    return Channel(
        channel_id=slot - 1,
        tx_mod=modulation,
        tx_freq=tx,
        rx_mod=modulation,
        rx_freq=rx,
        tx_sub_audio=tx_tone,
        rx_sub_audio=rx_tone,
        scan=bool(spec.get("scan", True)),
        tx_at_max_power=(power == "high"),
        talk_around=bool(spec.get("talk_around", False)),
        bandwidth=bandwidth,
        pre_de_emph_bypass=False,
        sign=False,
        tx_at_med_power=(power == "med"),
        tx_disable=bool(spec.get("tx_disable", False)),
        fixed_freq=False,
        fixed_bandwidth=False,
        fixed_tx_power=False,
        mute=bool(spec.get("mute", False)),
        name=name,
    )


def blank_channel(slot: int) -> Channel:
    """A cleared slot: zero frequencies, empty name, out of the scan list."""
    return Channel(
        channel_id=slot - 1,
        tx_mod="FM",
        tx_freq=0.0,
        rx_mod="FM",
        rx_freq=0.0,
        tx_sub_audio=None,
        rx_sub_audio=None,
        scan=False,
        tx_at_max_power=True,
        talk_around=False,
        bandwidth="WIDE",
        pre_de_emph_bypass=False,
        sign=False,
        tx_at_med_power=False,
        tx_disable=False,
        fixed_freq=False,
        fixed_bandwidth=False,
        fixed_tx_power=False,
        mute=False,
        name="",
    )


def load_plan(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("format") != PLAN_FORMAT:
        raise PlanError(
            f"not a {PLAN_FORMAT} plan (format={data.get('format')!r})"
        )
    version = data.get("version")
    if version != PLAN_VERSION:
        raise PlanError(
            f"plan version {version!r} unsupported; this tool speaks version {PLAN_VERSION}"
        )
    if not isinstance(data.get("regions"), list) or not data["regions"]:
        raise PlanError("plan has no regions")
    return data


def build_writes(plan: dict, channel_count: int, region_count: int) -> dict[int, dict[int, Channel]]:
    """Expand a plan into ``{region_index: {slot: Channel}}``.

    Slot collisions inside one region are an error rather than last-one-wins:
    a generator emitting the same slot twice is a bug worth surfacing before
    it reaches the radio.
    """
    writes: dict[int, dict[int, Channel]] = {}
    for region in plan["regions"]:
        idx = int(region["index"])
        if not 0 <= idx < region_count:
            raise PlanError(
                f"region {idx} out of range: radio reports {region_count} regions"
            )
        slots: dict[int, Channel] = {}
        for spec in region.get("channels", []):
            ch = channel_from_spec(spec)
            slot = ch.channel_id + 1
            if not 1 <= slot <= channel_count:
                raise PlanError(
                    f"region {idx} slot {slot} out of range: radio has {channel_count} slots per region"
                )
            if slot in slots:
                raise PlanError(f"region {idx}: slot {slot} listed twice")
            slots[slot] = ch
        if region.get("blank_unlisted", False):
            for slot in range(1, channel_count + 1):
                slots.setdefault(slot, blank_channel(slot))
        writes[idx] = slots
    return writes


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------
def channel_to_dict(ch: Channel) -> dict:
    d = ch.model_dump()
    for key in ("tx_sub_audio", "rx_sub_audio"):
        value = d.get(key)
        if hasattr(value, "n"):
            d[key] = {"__dcs__": value.n}
    return d


def dict_to_channel(d: dict) -> Channel:
    d = dict(d)
    for key in ("tx_sub_audio", "rx_sub_audio"):
        value = d.get(key)
        if isinstance(value, dict) and "__dcs__" in value:
            d[key] = DCS(n=value["__dcs__"])
    return Channel(**d)


async def read_all_regions(radio) -> dict[int, dict[int, dict]]:
    """Snapshot every channel of every region.

    Switching regions is what makes ``radio.channels`` point somewhere else,
    so this walks them in order and puts the radio back on the region the
    operator left it on.
    """
    saved = radio.status.curr_region
    snapshot: dict[int, dict[int, dict]] = {}
    for r_idx in range(len(radio.region_names)):
        if r_idx != radio.status.curr_region:
            await radio.set_region(r_idx)
        snapshot[r_idx] = {
            i: channel_to_dict(radio.channels[i])
            for i in range(radio.device_info.channel_count)
        }
        print(f"  region {r_idx} ({radio.region_names[r_idx]!r}): {radio.device_info.channel_count} slots")
    if radio.status.curr_region != saved:
        await radio.set_region(saved)
    return snapshot


def save_backup(snapshot: dict, region_names: list[str], note: str = "") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"n76-full-backup-{ts}.json"
    path.write_text(json.dumps({
        "timestamp": ts,
        "note": note,
        "region_names": region_names,
        "regions": {
            str(r): {str(c): v for c, v in chans.items()}
            for r, chans in snapshot.items()
        },
    }, indent=2))
    print(f"backup saved → {path}")
    return path


def latest_backup() -> Path | None:
    files = sorted(BACKUP_DIR.glob("n76-full-backup-*.json"))
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Compare / verify
# ---------------------------------------------------------------------------
#
# Fields the radio owns rather than the plan. ``channel_id`` is positional and
# the fixed_* flags are CPS-side lock bits that some firmware normalizes on
# write; comparing them produces noise, not signal.
VERIFY_FIELDS = (
    "name", "rx_freq", "tx_freq", "rx_mod", "tx_mod",
    "rx_sub_audio", "tx_sub_audio", "bandwidth", "scan",
    "tx_at_max_power", "tx_at_med_power", "tx_disable", "mute", "talk_around",
)


def channel_diff(want: Channel, got: Channel) -> list[str]:
    """Human-readable differences, ignoring fields the plan does not own."""
    out = []
    for field in VERIFY_FIELDS:
        a, b = getattr(want, field), getattr(got, field)
        if field in ("rx_freq", "tx_freq"):
            if abs(float(a) - float(b)) < 1e-6:
                continue
        elif field in ("rx_sub_audio", "tx_sub_audio"):
            if _tone_repr(a) == _tone_repr(b):
                continue
            a, b = _tone_repr(a), _tone_repr(b)
        elif a == b:
            continue
        out.append(f"{field}: want {a!r}, got {b!r}")
    return out


async def verify_writes(radio, writes: dict[int, dict[int, Channel]],
                        names: dict[int, str]) -> int:
    """Re-read written slots and report mismatches. Returns mismatch count."""
    saved = radio.status.curr_region
    bad = 0
    for r_idx in sorted(writes):
        if r_idx != radio.status.curr_region:
            await radio.set_region(r_idx)
        want_name = names.get(r_idx)
        if want_name is not None and radio.region_names[r_idx] != want_name:
            print(f"  MISMATCH region {r_idx} name: want {want_name!r}, got {radio.region_names[r_idx]!r}")
            bad += 1
        for slot in sorted(writes[r_idx]):
            want = writes[r_idx][slot]
            got = radio.channels[slot - 1]
            diffs = channel_diff(want, got)
            if diffs:
                bad += 1
                print(f"  MISMATCH region {r_idx} slot {slot} ({want.name!r}):")
                for d in diffs:
                    print(f"      {d}")
    if radio.status.curr_region != saved:
        await radio.set_region(saved)
    return bad


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def print_plan(plan: dict, writes: dict[int, dict[int, Channel]]) -> None:
    radio = plan.get("radio", {})
    print(f"\nplan: {plan.get('name', '(unnamed)')}")
    print(f"radio: {radio.get('model', '?')} "
          f"vendor_id={radio.get('vendor_id', '?')} product_id={radio.get('product_id', '?')}")
    if plan.get("generated_by"):
        print(f"generated by: {plan['generated_by']}")

    total = 0
    for region in plan["regions"]:
        idx = int(region["index"])
        slots = writes[idx]
        blanks = sum(1 for ch in slots.values() if not ch.name and ch.rx_freq == 0.0)
        label = region.get("name")
        print(f"\n=== region {idx}" + (f" → rename {label!r}" if label else "") + " ===")
        for slot in sorted(slots):
            ch = slots[slot]
            if not ch.name and ch.rx_freq == 0.0:
                continue
            power = "H" if ch.tx_at_max_power else ("M" if ch.tx_at_med_power else "L")
            print(f"  slot {slot:>2}: {ch.name:<10} rx={ch.rx_freq:>9.4f} tx={ch.tx_freq:>9.4f} "
                  f"{ch.bandwidth:<6} tone={_tone_repr(ch.tx_sub_audio):<6} "
                  f"pwr={power} scan={int(ch.scan)} txdis={int(ch.tx_disable)} mute={int(ch.mute)}")
        if blanks:
            print(f"  + {blanks} slot(s) blanked")
        total += len(slots) + (1 if label else 0)
    print(f"\nTOTAL WRITES: {total}")


# ---------------------------------------------------------------------------
# Radio session
# ---------------------------------------------------------------------------
def check_identity(radio, plan: dict) -> None:
    """Abort unless the connected radio is the one the plan was built for.

    Every Benshi HT speaks the same protocol, so without this a VR-N76 plan
    will happily write into some other model in the family.
    """
    want = plan.get("radio", {})
    info = radio.device_info
    for key in ("vendor_id", "product_id"):
        expected = want.get(key)
        if expected is None:
            continue
        actual = getattr(info, key)
        if int(expected) != int(actual):
            raise PlanError(
                f"radio identity mismatch: plan wants {key}={expected}, "
                f"connected radio reports {actual}. Refusing to write."
            )
    print(f"identity ok: vendor_id={info.vendor_id} product_id={info.product_id} "
          f"fw={info.firmware_version}")


async def apply_plan(radio, plan: dict, writes: dict[int, dict[int, Channel]]) -> None:
    conn = radio._conn
    saved = radio.status.curr_region

    for region in plan["regions"]:
        idx = int(region["index"])
        name = region.get("name")
        if name:
            await radio.set_region_name(idx, name)
            print(f"  region {idx} renamed → {name!r}")
        for slot in sorted(writes[idx]):
            ch = writes[idx][slot]
            await conn.write_region_channel(idx, ch)
            label = ch.name or "(blank)"
            print(f"  region {idx} slot {slot:>2} ← {label}")

    if radio.status.curr_region != saved:
        await radio.set_region(saved)


async def rollback(radio, backup_path: Path) -> None:
    conn = radio._conn
    data = json.loads(backup_path.read_text())
    saved = radio.status.curr_region

    for name_idx, name in enumerate(data["region_names"]):
        if radio.region_names[name_idx] != name:
            await radio.set_region_name(name_idx, name)
            print(f"  region {name_idx} name restored → {name!r}")

    for r_str, chans in data["regions"].items():
        r_idx = int(r_str)
        for c_str, cdict in chans.items():
            ch = dict_to_channel(cdict)
            await conn.write_region_channel(r_idx, ch)
        print(f"  region {r_idx}: {len(chans)} slots restored")

    if radio.status.curr_region != saved:
        await radio.set_region(saved)


async def connect(args):
    """Open a controller for whichever transport was requested."""
    if args.rfcomm:
        if not hasattr(socket, "BTPROTO_RFCOMM"):
            raise SystemExit(
                "RFCOMM unavailable: this CPython has no Bluetooth socket support "
                "(expected on macOS). Use --ble, or run this on Linux."
            )
        channel: int | str = int(args.channel) if str(args.channel).isdigit() else args.channel
        print(f"connecting RFCOMM to {args.rfcomm} channel={channel}", flush=True)
        return bc.RadioController.new_rfcomm(args.rfcomm, channel=channel)
    print(f"connecting BLE to {args.ble}", flush=True)
    return bc.RadioController.new_ble(args.ble)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="expand and print the plan; never touches the radio")
    mode.add_argument("--apply", action="store_true",
                      help="back up, write, then read back and verify")
    mode.add_argument("--verify", action="store_true",
                      help="read back and diff against the plan; writes nothing")
    mode.add_argument("--rollback", action="store_true",
                      help="restore a full backup (latest unless --backup given)")

    ap.add_argument("--plan", type=Path, help="codeplug plan JSON")
    ap.add_argument("--backup", type=Path, help="backup file for --rollback")
    ap.add_argument("--rfcomm", metavar="MAC", nargs="?",
                    const=os.environ.get("BENLINK_N76_MAC", ""),
                    help="connect over RFCOMM (Linux); defaults to $BENLINK_N76_MAC")
    ap.add_argument("--ble", metavar="ADDRESS",
                    help="connect over BLE using a known address")
    ap.add_argument("--channel", default="auto",
                    help="RFCOMM channel number, or 'auto' for SDP lookup (default)")
    args = ap.parse_args()

    if not args.rollback and not args.plan:
        ap.error("--plan is required unless --rollback is used")
    if not args.dry_run and not (args.rfcomm or args.ble):
        ap.error("need a transport: --rfcomm MAC or --ble ADDRESS")

    plan = load_plan(args.plan) if args.plan else None

    # Dry run never opens a connection, so it has to assume the geometry the
    # plan was generated against rather than asking the radio.
    if args.dry_run:
        limits = plan.get("radio", {})
        writes = build_writes(
            plan,
            channel_count=int(limits.get("channels_per_region", 32)),
            region_count=int(limits.get("region_count", 6)),
        )
        print_plan(plan, writes)
        print("\n(dry run — radio not contacted)")
        return 0

    async with await connect(args) as radio:
        print(f"connected fw={radio.device_info.firmware_version} "
              f"region={radio.status.curr_region} regions={list(radio.region_names)}")

        if args.rollback:
            path = args.backup or latest_backup()
            if not path or not path.exists():
                print(f"no backup found in {BACKUP_DIR}", file=sys.stderr)
                return 2
            print(f"restoring from {path}...")
            await rollback(radio, path)
            print("rollback complete.")
            return 0

        check_identity(radio, plan)
        writes = build_writes(
            plan,
            channel_count=radio.device_info.channel_count,
            region_count=len(radio.region_names),
        )
        names = {int(r["index"]): r["name"] for r in plan["regions"] if r.get("name")}
        print_plan(plan, writes)

        if args.verify:
            print("\n=== VERIFYING ===")
            bad = await verify_writes(radio, writes, names)
            print("verify: clean" if not bad else f"verify: {bad} mismatch(es)")
            return 0 if not bad else 1

        print("\n=== BACKING UP ===")
        snapshot = await read_all_regions(radio)
        save_backup(snapshot, list(radio.region_names),
                    note=f"pre-apply: {args.plan.name}")

        print("\n=== APPLYING ===")
        await apply_plan(radio, plan, writes)

        # A write that the radio declined is indistinguishable from one it
        # accepted until you read it back.
        print("\n=== VERIFYING ===")
        bad = await verify_writes(radio, writes, names)
        if bad:
            print(f"\n{bad} mismatch(es) — radio did NOT fully accept this plan.")
            print("Roll back with: --rollback")
            return 1
        print("verify: clean — all writes confirmed on the radio.")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        sys.exit(2)
