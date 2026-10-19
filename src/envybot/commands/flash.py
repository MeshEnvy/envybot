#!/usr/bin/env python3
"""Flash EnvyOS bench bootloader or firmware over USB (nRF52 OTAFIX)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from envybot.commands.onboard import pick_port
from envybot.flash_program import (
    DEFAULT_WAIT_S,
    flash_bootloader_package,
    flash_firmware_package,
    resolve_bootloader_zip,
    resolve_firmware_artifacts,
    pick_flash_target,
    default_envyos_root,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--target",
            metavar="SLUG",
            help="EnvyOS target slug (prompt on TTY when omitted)",
        )
        p.add_argument(
            "port",
            nargs="?",
            help="USB serial path (optional; VID/PID autodetect when omitted)",
        )
        p.add_argument(
            "--envyos",
            type=Path,
            default=None,
            help="EnvyOS repo root (default: ../envyos or ENVYOS_ROOT)",
        )
        p.add_argument(
            "--wait",
            type=float,
            default=DEFAULT_WAIT_S,
            metavar="SEC",
            help=f"Wait for bootloader USB (default {DEFAULT_WAIT_S:g})",
        )
        p.add_argument("--verbose", action="store_true")

    bl = sub.add_parser("bootloader", help="Flash SoftDevice+bootloader recovery zip")
    add_common(bl)
    bl.add_argument(
        "--bootloader",
        type=Path,
        metavar="ZIP",
        help="Recovery zip (default: newest bench adafruit-nrf52-bootloader)",
    )

    fw = sub.add_parser("firmware", help="Flash MeshCore firmware zip (or UF2 when volume mounted)")
    add_common(fw)
    fw.add_argument("--firmware", type=Path, metavar="ZIP", help="Firmware dfu zip")
    fw.add_argument("--uf2", type=Path, metavar="UF2", help="Optional UF2 for drag-drop path")

    args = parser.parse_args(argv)
    port = pick_port(args.port, baud=115200, timeout=5.0, verbose=args.verbose)
    print(f"USB {port}")
    spec = pick_flash_target(args.target)
    envyos = (args.envyos or default_envyos_root()).resolve()

    try:
        if args.kind == "bootloader":
            package = resolve_bootloader_zip(
                envyos, spec, override=args.bootloader
            )
            flash_bootloader_package(
                package,
                spec,
                port=port,
                wait_s=args.wait,
                verbose=args.verbose,
            )
        else:
            package, uf2 = resolve_firmware_artifacts(
                envyos,
                spec,
                zip_override=args.firmware,
                uf2_override=args.uf2,
            )
            flash_firmware_package(
                package,
                spec,
                uf2=uf2,
                port=port,
                wait_s=args.wait,
                verbose=args.verbose,
            )
    except subprocess.CalledProcessError as exc:
        print(f"flash failed: {exc}", file=sys.stderr)
        return exc.returncode or 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
