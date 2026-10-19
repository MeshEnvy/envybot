"""Companion USB setup for envybot seed (CMD_RUN_CLI_COMMAND + raw MS mota-seeder)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from envybot.radio import parse_get_value


def _fmt_self_info(info: dict[str, Any]) -> list[str]:
    name = info.get("name") or "?"
    pubkey = info.get("public_key") or "?"
    freq = info.get("radio_freq")
    bw = info.get("radio_bw")
    sf = info.get("radio_sf")
    cr = info.get("radio_cr")
    radio = (
        f"{freq} MHz, BW {bw} kHz, SF{sf}, CR{cr}"
        if freq is not None
        else "?"
    )
    return [
        f"  name: {name}",
        f"  public.key: {pubkey}",
        f"  radio: {radio}",
        f"  tx_power: {info.get('tx_power', '?')} dBm",
    ]


async def _run_cli(client: Any, cmd: str, *, on_host: Callable[[str], None]) -> str | None:
    from meshcore.events import EventType

    ev = await client.commands.run_cli_command(cmd)
    if ev.type == EventType.ERROR:
        on_host(f"cli `{cmd}` failed: {ev.payload}")
        return None
    if ev.type == EventType.CLI_REPLY:
        text = str(ev.payload.get("text") or "").strip()
        on_host(f"cli `{cmd}`: {text[:200]}")
        return text
    on_host(f"cli `{cmd}`: unexpected {ev.type}")
    return None


async def _companion_setup_async(
    port: str,
    *,
    baud: int,
    dutycycle_pct: int,
    enable_folder: bool,
    verbose: bool,
    on_host: Callable[[str], None],
) -> None:
    from meshcore import MeshCore

    client = await MeshCore.create_serial(
        port,
        baudrate=baud,
        auto_reconnect=False,
        max_reconnect_attempts=0,
        debug=verbose,
    )
    if client is None:
        raise RuntimeError("companion USB: no appstart / SELF_INFO on this port")

    info = client.self_info or {}
    on_host("seeder tag (companion USB):")
    for line in _fmt_self_info(info):
        on_host(line)

    await _run_cli(client, f"set dutycycle {dutycycle_pct}", on_host=on_host)

    for cmd in (
        "get radio",
        "get tx",
        "get dutycycle",
        "get radio.rxgain",
        "get radio.fem.rxgain",
        "get radio.fem.txgain",
    ):
        raw = await _run_cli(client, cmd, on_host=on_host)
        if raw and parse_get_value(raw):
            on_host(f"  {cmd.split(maxsplit=1)[-1]}: {parse_get_value(raw)}")

    if enable_folder:
        await _run_cli(client, "ota folder off", on_host=on_host)
        await _run_cli(client, "ota folder on", on_host=on_host)
        # `ota folder on` already announces. A follow-up `ota announce` is a companion
        # frame sent after the tag has stopped reading app frames (folder_active).

    await client.disconnect()
    await asyncio.sleep(0.35)


async def _companion_folder_off_async(port: str, *, baud: int, verbose: bool) -> None:
    from meshcore import MeshCore

    try:
        client = await MeshCore.create_serial(
            port,
            baudrate=baud,
            auto_reconnect=False,
            max_reconnect_attempts=0,
            debug=verbose,
        )
        if client is None:
            return
        await client.commands.run_cli_command("ota folder off")
        await client.disconnect()
    except Exception:
        pass


def companion_setup(
    port: str,
    *,
    baud: int,
    dutycycle_pct: int,
    enable_folder: bool,
    verbose: bool,
    on_host: Callable[[str], None],
) -> None:
    asyncio.run(
        _companion_setup_async(
            port,
            baud=baud,
            dutycycle_pct=dutycycle_pct,
            enable_folder=enable_folder,
            verbose=verbose,
            on_host=on_host,
        )
    )


def companion_folder_off(port: str, *, baud: int, verbose: bool) -> None:
    asyncio.run(_companion_folder_off_async(port, baud=baud, verbose=verbose))
