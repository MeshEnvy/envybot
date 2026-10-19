#!/usr/bin/env python3
"""USB-serial onboard for a MeshCore repeater (text CLI, not companion protocol)."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]

from envybot.apply import stamp_profile_after_onboard
from envybot.bench_flash import BenchFlashOptions, run_bench_flash
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import load_sites_for_book
from envybot.onboard_book import (
    PUB_HEX_LEN,
    PRV_HEX_LEN,
    ensure_onboard_unit,
    format_neighbors,
    load_registry,
    normalize_hex,
    register,
    resolve_passwords,
)
from envybot.onboard_serial_apply import apply_profile_over_serial, apply_serial_acl, require_ok
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
    DEFAULT_TIMEOUT,
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

DISCOVER_ROUNDS = 3
DISCOVER_WAIT_S = 12.0
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
    parser.add_argument("port", nargs="?", help="USB modem path (or usbmodemNNNN). Omit to autodetect.")
    parser.add_argument("--unit", metavar="me0041", help="Bind to this nodes.yaml key (create if missing)")
    parser.add_argument("--admin-pw", help="Admin password (generated if omitted; yaml reused on re-run)")
    parser.add_argument("--guest-pw", help="Guest password (generated if omitted; '' for blank)")
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    parser.add_argument("--no-write", action="store_true", help="Do not update nodes.yaml")
    parser.add_argument("--force", action="store_true", help="Re-SET even when already matching")
    parser.add_argument("--no-reboot", action="store_true", help="Skip reboot (radio change stays pending)")
    parser.add_argument("--no-discover", action="store_true", help="Skip neighbor discover/fetch rounds")
    parser.add_argument("--discover-rounds", type=int, default=DISCOVER_ROUNDS, metavar="N")
    parser.add_argument("--discover-wait", type=float, default=DISCOVER_WAIT_S, metavar="SEC")
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--with-flash-bootloader", action="store_true")
    parser.add_argument("--with-flash-firmware", action="store_true")
    parser.add_argument("--target", metavar="SLUG", help="EnvyOS target slug")
    parser.add_argument("--envyos", type=Path, default=None)
    parser.add_argument("--flash-wait", type=float, default=120.0, metavar="SEC")
    parser.add_argument("--no-flash-pause", action="store_true")
    args = parser.parse_args(argv)

    port = pick_port(args.port, baud=args.baud, timeout=args.timeout, verbose=args.verbose)
    usb_identity: str | None = None
    if args.with_flash_bootloader or args.with_flash_firmware:
        port, usb_identity = run_bench_flash(
            port,
            BenchFlashOptions(
                target=args.target,
                envyos=args.envyos,
                flash_wait=args.flash_wait,
                with_bootloader=args.with_flash_bootloader,
                with_firmware=args.with_flash_firmware,
                no_flash_pause=args.no_flash_pause,
                verbose=args.verbose,
                baud=args.baud,
            ),
        )

    cli: RepeaterSerial | None = None
    try:
        if args.with_flash_firmware:
            cli, port = reconnect_after_reboot(
                port,
                baud=args.baud,
                timeout=args.timeout,
                verbose=args.verbose,
                usb_identity=usb_identity,
                wait_for_reply=True,
            )
        else:
            print(f"USB {port}")
            cli = open_repeater(
                port,
                baud=args.baud,
                timeout=args.timeout,
                verbose=args.verbose,
                wait_for_reply=True,
            )
        print(f"USB {port}")
        pub = normalize_hex(parse_get_value(cli.cmd("get public.key")) or "", PUB_HEX_LEN, "public.key")
        doc, nodes = load_registry(args.nodes)
        sites = load_sites_for_book(args.nodes)
        keys = load_keys(keys_path(args.nodes))
        unit_key, existing, created = ensure_onboard_unit(doc, nodes, pub, unit=args.unit)
        print(f"unit {unit_key.upper()} ({'new' if created else 'existing'})")
        admin_pw, guest_pw, admin_src, guest_src = resolve_passwords(
            cli,
            existing,
            admin_pw=args.admin_pw,
            guest_pw=args.guest_pw,
            doc=doc,
            key=unit_key,
        )
        print(f"admin {admin_pw}  ({admin_src})")
        print(f"guest {guest_pw or '(blank)'}  ({guest_src})")

        result = onboard(
            cli,
            admin_pw=admin_pw,
            guest_pw=guest_pw,
            admin_src=admin_src,
            guest_src=guest_src,
            force=args.force,
            doc=doc,
            keys=keys,
            sites=sites,
            pubkey=pub,
            node=existing,
            key=unit_key,
        )

        do_discover = not args.no_discover and args.discover_rounds > 0
        if args.no_reboot:
            print("reboot skipped (--no-reboot) — radio applies after reboot")
        else:
            print("reboot (apply radio) …")
            dropped = send_reboot(cli)
            cli = None
            if dropped:
                print("   USB dropped")
            else:
                print("   USB still present — reboot did not take")

        if do_discover and not confirm_antenna():
            do_discover = False

        if do_discover:
            print(f"12. neighbor baseline ({args.discover_rounds}×, {args.discover_wait:g}s) …")
            if cli is None:
                cli, port = reconnect_after_reboot(
                    port,
                    baud=args.baud,
                    timeout=args.timeout,
                    verbose=args.verbose,
                    usb_identity=usb_identity,
                    wait_for_reply=True,
                )
            result["neighbors"] = discover_baseline(
                cli, rounds=args.discover_rounds, wait_s=args.discover_wait
            )

        if not args.no_write:
            if cli is None:
                cli, port = reconnect_after_reboot(
                    port,
                    baud=args.baud,
                    timeout=args.timeout,
                    verbose=args.verbose,
                    usb_identity=usb_identity,
                    wait_for_reply=True,
                )
            unit_key, created = register(
                args.nodes, result, unit=unit_key, flash_target=args.target
            )
            print(f"nodes.yaml {'created' if created else 'updated'} {unit_key.upper()}")
            acl_doc, acl_nodes = load_registry(args.nodes)
            acl_node = acl_nodes.get(unit_key) or {}
            print("ACL …")
            apply_serial_acl(cli, acl_doc, acl_node, keys, force=args.force)
            pid = stamp_fleet_ready(args.nodes, unit_key)
            if pid:
                print(f"profile stamped ({pid})")
        else:
            print("nodes.yaml not written (--no-write)")

    except (CliError, OSError) as exc:
        print(f"onboard failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if serial is not None and isinstance(exc, serial.SerialException):
            print(f"onboard failed: {exc}", file=sys.stderr)
            return 1
        raise
    finally:
        if cli is not None:
            cli.__exit__(None, None, None)
    return 0


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
