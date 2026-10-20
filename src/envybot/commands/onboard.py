#!/usr/bin/env python3
"""USB-serial onboard for a MeshCore repeater (text CLI, not companion protocol)."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

from envybot.apply import stamp_profile_after_onboard
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import load_sites_for_book
from envybot.onboard_book import (
    PUB_HEX_LEN,
    PRV_HEX_LEN,
    format_neighbors,
    load_registry,
    normalize_hex,
)
from envybot.onboard_serial_apply import apply_profile_over_serial, require_ok
from envybot.onboard_usb_session import (
    DISCOVER_ROUNDS,
    DISCOVER_WAIT_S,
    add_common_usb_args,
    run_usb_onboard_session,
)
from envybot.onboard_usb import (
    BAUD,
    pick_port,
    reconnect_after_reboot,
    open_repeater,
    send_reboot,
)
from envybot.radio import (
    parse_bootloader,
    parse_firmware,
    parse_get_value,
    parse_ota_self,
)
from envybot.repeater_serial import (
    CliError,
    PortProbe,
    RepeaterSerial,
    probe_repeater,
    reply_failed,
)

# Re-exports for flash, range, seed, tests
from envybot.onboard_usb import (  # noqa: F401
    open_repeater,
    pick_port,
    prompt_reboot_after_flash,
    resolve_port_arg,
)

# USA/Canada recommended: 910.525 MHz, BW 62.5, SF7, CR 4/5
RADIO_FREQ = 910.525
RADIO_BW = 62.5
RADIO_SF = 7
RADIO_CR = 5
RADIO_CMD = f"set radio {RADIO_FREQ},{RADIO_BW},{RADIO_SF},{RADIO_CR}"

DISCOVER_CMD_TIMEOUT = 30.0
DISCOVER_CMD_WAIT_S = 90.0
NEIGH_RE = re.compile(r"^([0-9a-fA-F]{8}):(\d+):(-?\d+)$")


def read_bootloader(cli: RepeaterSerial) -> tuple[str, str]:
    raw = cli.cmd("get bootloader.ver")
    parsed = parse_bootloader(raw) or ""
    if parsed.lower() == "unknown":
        parsed = ""
    return parsed, raw


def read_ota_self(cli: RepeaterSerial) -> tuple[str | None, str]:
    raw = cli.cmd("ota self")
    parsed = parse_ota_self(raw)
    return parsed, raw


def parse_radio(text: str | None) -> tuple[float, float, int, int] | None:
    val = parse_get_value(text) or (text or "").strip()
    parts = [p.strip() for p in val.replace(" ", ",").split(",") if p.strip()]
    if len(parts) < 4:
        return None
    try:
        return float(parts[0]), float(parts[1]), int(float(parts[2])), int(float(parts[3]))
    except ValueError:
        return None


def radio_matches(got: tuple[float, float, int, int] | None) -> bool:
    if got is None:
        return False
    freq, bw, sf, cr = got
    return abs(freq - RADIO_FREQ) < 0.01 and abs(bw - RADIO_BW) < 0.1 and sf == RADIO_SF and cr == RADIO_CR


def parse_neighbors_cli(text: str) -> list[dict[str, Any]]:
    body = text.strip()
    if not body or body.startswith("-none-"):
        return []
    out: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("->"):
            line = line.split("->", 1)[1].strip()
        if not line or line.startswith("-none-"):
            continue
        m = NEIGH_RE.match(line)
        if not m:
            continue
        out.append(
            {
                "pubkey": m.group(1).lower(),
                "secs_ago": int(m.group(2)),
                "snr": int(m.group(3)) / 4.0,
            }
        )
    return out


def antenna_ready(reply: str) -> bool:
    return reply.strip().lower() not in ("s", "n", "no", "skip")


def confirm_antenna() -> bool:
    if not sys.stdin.isatty():
        print("neighbor baseline skipped (no TTY to confirm antenna)")
        return False
    try:
        reply = input("Attach an antenna, then press Enter (s to skip) ")
    except EOFError:
        print("neighbor baseline skipped")
        return False
    if antenna_ready(reply):
        return True
    print("neighbor baseline skipped")
    return False


def discover_baseline(
    cli: RepeaterSerial,
    *,
    rounds: int,
    wait_s: float,
) -> list[dict[str, Any]]:
    latest: list[dict[str, Any]] = []
    for i in range(1, rounds + 1):
        print(f"   round {i}/{rounds} discover.neighbors …")
        require_ok(
            "discover.neighbors",
            cli.send_command(
                "discover.neighbors",
                timeout=DISCOVER_CMD_TIMEOUT,
                wait_deadline_s=DISCOVER_CMD_WAIT_S,
            ),
        )
        time.sleep(wait_s)
        raw = cli.cmd("neighbors")
        if reply_failed(raw):
            raise CliError(f"neighbors: {raw}")
        latest = parse_neighbors_cli(raw)
        print(f"   {format_neighbors(latest)}")
    return latest


def onboard(
    cli: RepeaterSerial,
    *,
    admin_pw: str,
    guest_pw: str,
    admin_src: str,
    guest_src: str,
    force: bool,
    doc: dict[str, Any],
    keys: dict[str, list[str]],
    sites: dict[str, dict[str, Any]],
    pubkey: str | None = None,
    node: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    from envybot.onboard_serial_apply import apply_if_needed

    ver_raw = cli.cmd("ver")
    if reply_failed(ver_raw):
        raise CliError(f"not a repeater CLI: {ver_raw}")
    fw, platform = parse_firmware(ver_raw)
    print(f"   fw {ver_raw}")

    print("   bootloader …")
    bl, bl_raw = read_bootloader(cli)
    if bl:
        print(f"   bl {bl}")
    else:
        print(f"   bl unknown ({bl_raw})")

    print("   ota self …")
    base_hash, ota_raw = read_ota_self(cli)
    if base_hash is None:
        print(f"   base hash unparsed ({ota_raw.strip()[:60]})")
    elif base_hash:
        print(f"   base hash {base_hash}")
    else:
        print(f"   base hash empty ({ota_raw.strip()[:60]})")

    if not pubkey:
        pubkey = normalize_hex(parse_get_value(cli.cmd("get public.key")) or "", PUB_HEX_LEN, "public.key")
    print(f"   pub {pubkey}")

    print("1. radio USA/Canada …")
    radio = parse_radio(cli.cmd("get radio"))
    radio_changed = apply_if_needed(
        cli,
        step="set radio",
        already=radio_matches(radio),
        setter=RADIO_CMD,
        verify=lambda: radio_matches(parse_radio(cli.cmd("get radio"))),
        ok_label=f"{RADIO_FREQ},{RADIO_BW},{RADIO_SF},{RADIO_CR}",
        force=force,
    )
    if radio_changed:
        print("   (reboot to apply)")

    profile_node = dict(node or {})
    profile_node.setdefault("unit_id", (key or "").upper())
    profile_node["admin_password"] = admin_pw
    profile_node["guest_password"] = guest_pw

    admin_changed, guest_changed = apply_profile_over_serial(
        cli,
        key or "",
        profile_node,
        doc=doc,
        keys=keys,
        sites=sites,
        admin_pw=admin_pw,
        guest_pw=guest_pw,
        admin_src=admin_src,
        guest_src=guest_src,
        fw=fw,
        force=force,
    )

    print("10. secret key …")
    prv = normalize_hex(parse_get_value(cli.cmd("get prv.key")) or "", PRV_HEX_LEN, "prv.key")
    print(f"   {prv}")

    print("11. clock …")
    now = int(time.time())
    clock_reply = cli.cmd(f"time {now}")
    if "cannot go backwards" in clock_reply.lower():
        print(f"   skipped ({clock_reply})")
    else:
        require_ok("time", clock_reply)
        print(f"   {clock_reply}")

    return {
        "pubkey": pubkey,
        "secret": prv,
        "admin_password": admin_pw,
        "guest_password": guest_pw,
        "admin_changed": admin_changed,
        "guest_changed": guest_changed,
        "radio_changed": radio_changed,
        "firmware_version": fw,
        "firmware_platform": platform or "meshcore",
        "firmware_raw": ver_raw,
        "bootloader_version": bl,
        "base_hash": base_hash,
        "node_clock": now,
        "neighbors": None,
    }


def stamp_fleet_ready(nodes_path: Path, unit: str) -> str | None:
    doc, nodes = load_registry(nodes_path)
    node = nodes.get(unit)
    if not isinstance(node, dict):
        raise CliError(f"{unit}: missing after register")
    from envybot.history import open_history

    conn = open_history(nodes_path.parent)
    try:
        return stamp_profile_after_onboard(
            conn,
            unit,
            node,
            load_sites_for_book(nodes_path),
            doc=doc,
            keys=load_keys(keys_path(nodes_path)),
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_usb_args(parser)
    parser.add_argument("--unit", metavar="me0041", help="Bind to this nodes.yaml key (create if missing)")
    args = parser.parse_args(argv)
    return run_usb_onboard_session(
        args,
        mode="onboard",
        onboard_fn=onboard,
        discover_baseline=discover_baseline,
        confirm_antenna=confirm_antenna,
        stamp_fleet_ready=stamp_fleet_ready,
    )


# Re-exports for tests and sibling commands
from envybot.onboard_serial_apply import (  # noqa: E402
    apply_admin_password,
    apply_agc_reset_interval_policy,
    apply_ota_autofetch_policy,
    apply_path_hash_policy,
)
from envybot.onboard_usb import wait_usb_gone  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
