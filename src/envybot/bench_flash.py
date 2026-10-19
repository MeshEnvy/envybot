"""Shared EnvyOS bench USB flash steps for ``flash`` and ``onboard --with-flash-*``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from envybot.flash_program import (
    FlashTarget,
    default_envyos_root,
    flash_bootloader_package,
    flash_firmware_package,
    flash_usb_poll,
    pick_flash_target,
    port_usb_identity,
    prepare_serial_port,
    resolve_bootloader_zip,
    resolve_firmware_artifacts,
    settle_after_dfu,
)
from envybot.onboard_usb import prompt_reboot_after_flash


@dataclass
class BenchFlashOptions:
    target: str | None
    envyos: Path | None
    flash_wait: float
    with_bootloader: bool
    with_firmware: bool
    no_flash_pause: bool
    verbose: bool
    baud: int = 115200


def run_bench_flash(
    port: str,
    opts: BenchFlashOptions,
    *,
    on_bootloader_only_exit: Callable[[], None] | None = None,
) -> tuple[str, str | None]:
    """Flash bootloader and/or firmware. Returns (port, usb_identity)."""
    usb_identity = port_usb_identity(port)
    spec = pick_flash_target(opts.target)
    port = prepare_serial_port(port, usb_identity, baud=opts.baud)
    poll = flash_usb_poll(spec, port)
    envyos = (opts.envyos or default_envyos_root()).resolve()

    if opts.with_bootloader:
        bl_zip = resolve_bootloader_zip(envyos, spec)
        port = flash_bootloader_package(
            bl_zip,
            spec,
            port=port,
            wait_s=opts.flash_wait,
            poll=poll,
            verbose=opts.verbose,
        )
        if not opts.no_flash_pause:
            prompt_reboot_after_flash("Bootloader")
        if opts.with_firmware:
            port = settle_after_dfu(
                spec,
                port,
                usb_identity=usb_identity,
                wait_s=opts.flash_wait,
                poll=flash_usb_poll(spec, port),
                verbose=opts.verbose,
            )

    if opts.with_firmware:
        fw_zip, uf2 = resolve_firmware_artifacts(envyos, spec)
        flash_firmware_package(
            fw_zip,
            spec,
            uf2=uf2,
            port=port,
            wait_s=opts.flash_wait,
            poll=flash_usb_poll(spec, port),
            verbose=opts.verbose,
        )
        if not opts.no_flash_pause:
            prompt_reboot_after_flash("Firmware")
    elif opts.with_bootloader:
        if on_bootloader_only_exit:
            on_bootloader_only_exit()
        else:
            print(
                "Bootloader flash wipes the app. Re-run with --with-flash-firmware "
                "to install MeshCore, then onboard.",
                file=sys.stderr,
            )
        raise SystemExit(0)

    return port, usb_identity


def flash_target_spec(slug: str | None) -> FlashTarget:
    return pick_flash_target(slug)
