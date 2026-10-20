"""Shared USB onboard / re-onboard orchestration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Literal

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]

from envybot.bench_flash import BenchFlashOptions, run_bench_flash
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import load_sites_for_book
from envybot.onboard_book import (
    PUB_HEX_LEN,
    ensure_onboard_unit,
    ensure_reonboard_unit,
    generate_identity_secret,
    load_registry,
    normalize_hex,
    register,
    resolve_passwords,
    should_mint_fleet_identity,
)
from envybot.onboard_serial_apply import (
    apply_serial_acl,
    mint_identity_on_usb,
    restore_book_identity_on_usb,
    usb_identity_matches_book,
)
from envybot.onboard_usb import (
    BAUD,
    open_repeater,
    pick_port,
    reconnect_after_reboot,
    send_reboot,
)
from envybot.radio import parse_get_value
from envybot.repeater_serial import CliError, DEFAULT_TIMEOUT, RepeaterSerial

OnboardMode = Literal["onboard", "reonboard"]

DISCOVER_ROUNDS = 3
DISCOVER_WAIT_S = 12.0


def _identity_reboot(
    cli: RepeaterSerial | None,
    port: str,
    *,
    baud: int,
    timeout: float,
    verbose: bool,
    usb_identity: str | None,
    label: str,
) -> tuple[RepeaterSerial, str]:
    print(f"   reboot ({label}) …")
    if cli is not None:
        dropped = send_reboot(cli)
        cli = None
        if dropped:
            print("   USB dropped")
        else:
            print("   USB still present — reboot did not take")
    cli, port = reconnect_after_reboot(
        port,
        baud=baud,
        timeout=timeout,
        verbose=verbose,
        usb_identity=usb_identity,
        wait_for_reply=True,
    )
    return cli, port


def run_usb_onboard_session(
    args: argparse.Namespace,
    *,
    mode: OnboardMode,
    onboard_fn,
    discover_baseline,
    confirm_antenna,
    stamp_fleet_ready,
) -> int:
    """Run CLI SET sequence, optional discover, yaml + ACL. ``onboard_fn`` is ``onboard()``."""
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
    fail_label = "reonboard failed" if mode == "reonboard" else "onboard failed"
    profile_force = bool(getattr(args, "force", False))
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
        pub = normalize_hex(
            parse_get_value(cli.cmd("get public.key")) or "",
            PUB_HEX_LEN,
            "public.key",
        )
        doc, nodes = load_registry(args.nodes)
        sites = load_sites_for_book(args.nodes)
        keys = load_keys(keys_path(args.nodes))

        if mode == "reonboard":
            unit_key, existing = ensure_reonboard_unit(nodes, unit=args.unit)
            created = False
        else:
            unit_key, existing, created = ensure_onboard_unit(
                doc,
                nodes,
                pub,
                unit=args.unit,
            )
        print(f"unit {unit_key.upper()} ({'new' if created else 'existing'})")

        if mode == "reonboard":
            assert existing is not None
            print("0. restore book identity …")
            if restore_book_identity_on_usb(cli, existing):
                cli, port = _identity_reboot(
                    cli,
                    port,
                    baud=args.baud,
                    timeout=args.timeout,
                    verbose=args.verbose,
                    usb_identity=usb_identity,
                    label="apply identity",
                )
            if not usb_identity_matches_book(cli, existing):
                raise CliError(
                    "book identity restore failed (public.key still differs from yaml)"
                )
            pub = normalize_hex(
                parse_get_value(cli.cmd("get public.key")) or "",
                PUB_HEX_LEN,
                "public.key",
            )
            print(f"   pub {pub}")
        elif should_mint_fleet_identity(existing, pub, created=created):
            secret = generate_identity_secret()
            print("0. mint fleet identity …")
            if mint_identity_on_usb(cli, secret):
                cli, port = _identity_reboot(
                    cli,
                    port,
                    baud=args.baud,
                    timeout=args.timeout,
                    verbose=args.verbose,
                    usb_identity=usb_identity,
                    label="apply identity",
                )
            pub = normalize_hex(
                parse_get_value(cli.cmd("get public.key")) or "",
                PUB_HEX_LEN,
                "public.key",
            )
            print(f"   pub {pub}")

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

        result = onboard_fn(
            cli,
            admin_pw=admin_pw,
            guest_pw=guest_pw,
            admin_src=admin_src,
            guest_src=guest_src,
            force=profile_force,
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
                args.nodes,
                result,
                unit=unit_key,
                flash_target=args.target,
            )
            print(f"nodes.yaml {'created' if created else 'updated'} {unit_key.upper()}")
            acl_doc, acl_nodes = load_registry(args.nodes)
            acl_node = acl_nodes.get(unit_key) or {}
            print("ACL …")
            apply_serial_acl(cli, acl_doc, acl_node, keys, force=profile_force)
            pid = stamp_fleet_ready(args.nodes, unit_key)
            if pid:
                print(f"profile stamped ({pid})")
        else:
            print("nodes.yaml not written (--no-write)")

    except (CliError, OSError) as exc:
        print(f"{fail_label}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if serial is not None and isinstance(exc, serial.SerialException):
            print(f"{fail_label}: {exc}", file=sys.stderr)
            return 1
        raise
    finally:
        if cli is not None:
            cli.__exit__(None, None, None)
    return 0


def add_common_usb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("port", nargs="?", help="USB modem path (or usbmodemNNNN). Omit to autodetect.")
    parser.add_argument("--admin-pw", help="Admin password (generated if omitted; yaml reused on re-run)")
    parser.add_argument("--guest-pw", help="Guest password (generated if omitted; '' for blank)")
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    parser.add_argument("--no-write", action="store_true", help="Do not update nodes.yaml")
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
