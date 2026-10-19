"""Detect USB role for envybot seed (companion app protocol vs repeater text CLI)."""

from __future__ import annotations

import asyncio
from typing import Literal

UsbRole = Literal["companion", "repeater", "unknown"]


async def _probe_companion_serial(port: str, *, baud: int, verbose: bool) -> bool:
    from envybot.radio import try_open_companion

    client, _err = await try_open_companion(
        transport="serial",
        verbose=verbose,
        baud=baud,
        serial_port=port,
    )
    if client is None:
        return False
    await client.disconnect()
    return True


def probe_repeater_text_cli(port: str, *, baud: int, verbose: bool) -> bool:
    from envybot.repeater_serial import CliError, probe_repeater

    try:
        probe = probe_repeater(port, baud=baud, timeout=5.0, verbose=verbose)
    except (CliError, OSError):
        return False
    return bool(probe.ver)


def detect_usb_role(port: str, *, baud: int = 115200, verbose: bool = False) -> UsbRole:
    """EnvyBot seed expects companion USB; repeater text CLI is a legacy fallback."""
    if asyncio.run(_probe_companion_serial(port, baud=baud, verbose=verbose)):
        return "companion"
    if probe_repeater_text_cli(port, baud=baud, verbose=verbose):
        return "repeater"
    return "unknown"
