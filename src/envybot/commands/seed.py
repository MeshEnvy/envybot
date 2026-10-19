#!/usr/bin/env python3
"""USB-serial OTA seeder — relay a folder of `.mota` over mota-seeder framing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from envybot.commands.onboard import resolve_port_arg
from envybot.radio import serial_port_candidates
from envybot.seeder import (
    DEFAULT_ANNOUNCE_INTERVAL_S,
    DEFAULT_STATUS_INTERVAL_S,
    SEEDER_DUTYCYCLE_PCT,
    open_serial_port,
    run_seeder,
)


def pick_serial_port(hint: str | None) -> str:
    if hint:
        resolved = resolve_port_arg(hint)
        if resolved:
            return resolved
        raise SystemExit(f"envybot seed: serial device not found: {hint}")

    ports = serial_port_candidates()
    if not ports:
        raise SystemExit("envybot seed: no USB serial ports found")

    last_err: str | None = None
    for port in ports:
        try:
            ser = open_serial_port(port, 115200)
            ser.close()
            return port
        except OSError as exc:
            last_err = str(exc)
            continue

    msg = last_err or "could not open any port"
    raise SystemExit(f"envybot seed: {msg}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="envybot seed",
        description="Serve a folder of .mota files to an OTA-capable repeater over USB serial.",
    )
    parser.add_argument(
        "dir",
        type=Path,
        help="Directory containing .mota files to advertise",
    )
    parser.add_argument(
        "port",
        nargs="?",
        default=None,
        help="USB serial device (optional; autodetect if omitted)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="GLOB",
        help="Include only filenames matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan subdirectories for .mota files",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Serial baud rate (default 115200)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log seeder protocol requests",
    )
    parser.add_argument(
        "--no-enable",
        action="store_true",
        help="Do not send `ota folder on` at start",
    )
    parser.add_argument(
        "--announce-interval",
        type=float,
        default=DEFAULT_ANNOUNCE_INTERVAL_S,
        metavar="SEC",
        help="Re-send `ota announce` on the tag while seeding (0=only after folder on; default 60)",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL_S,
        metavar="SEC",
        help="Poll tag `ota folder` / `ota stats` over USB (0=off; default 45)",
    )
    parser.add_argument(
        "--dutycycle",
        type=int,
        default=SEEDER_DUTYCYCLE_PCT,
        metavar="PCT",
        help=f"Tag airtime cap via `set dutycycle` while seeding (1–100; default {SEEDER_DUTYCYCLE_PCT})",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.dutycycle <= 100:
        print("envybot seed: --dutycycle must be 1–100", file=sys.stderr)
        return 1

    mota_dir = args.dir.expanduser().resolve()
    if not mota_dir.is_dir():
        print(f"envybot seed: not a directory: {mota_dir}", file=sys.stderr)
        return 1

    only = args.only or None
    port = pick_serial_port(args.port)

    return run_seeder(
        mota_dir,
        port,
        baud=args.baud,
        recursive=args.recursive,
        only_globs=only,
        verbose=args.verbose,
        no_enable=args.no_enable,
        announce_interval_s=max(0.0, args.announce_interval),
        status_interval_s=max(0.0, args.status_interval),
        dutycycle_pct=args.dutycycle,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
