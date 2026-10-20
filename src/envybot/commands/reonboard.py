#!/usr/bin/env python3
"""USB-serial re-onboard: push an existing book row onto the radio under test."""

from __future__ import annotations

import argparse

from envybot.commands.onboard import (
    confirm_antenna,
    discover_baseline,
    onboard,
    stamp_fleet_ready,
)
from envybot.onboard_usb_session import add_common_usb_args, run_usb_onboard_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_usb_args(parser)
    parser.add_argument(
        "--unit",
        metavar="me0041",
        required=True,
        help="Book row to restore onto USB (identity, passwords, profile)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-SET profile fields even when GET already matches",
    )
    args = parser.parse_args(argv)
    return run_usb_onboard_session(
        args,
        mode="reonboard",
        onboard_fn=onboard,
        discover_baseline=discover_baseline,
        confirm_antenna=confirm_antenna,
        stamp_fleet_ready=stamp_fleet_ready,
    )


if __name__ == "__main__":
    raise SystemExit(main())
