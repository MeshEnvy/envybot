"""Decide whether reonboard should flash bench bootloader/firmware."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from envybot.bench_versions import (
    bootloader_label_from_zip,
    device_meets_bench,
    firmware_label_from_zip,
    firmware_stamp_for_compare,
)
from envybot.flash_program import FlashTarget, resolve_bootloader_zip, resolve_firmware_artifacts


@dataclass(frozen=True)
class ReonboardFlashPlan:
    flash_bootloader: bool
    flash_firmware: bool
    bench_bootloader: str | None
    bench_firmware: str | None
    device_bootloader: str | None
    device_firmware: str | None


def plan_reonboard_flash(
    *,
    device_firmware: str | None,
    device_bootloader: str | None,
    envyos: Path,
    spec: FlashTarget,
) -> tuple[ReonboardFlashPlan, Path, Path]:
    bl_zip = resolve_bootloader_zip(envyos, spec)
    fw_zip, _uf2 = resolve_firmware_artifacts(envyos, spec)
    bl_label = bootloader_label_from_zip(bl_zip)
    fw_label = firmware_label_from_zip(fw_zip, firmware_prefix=spec.firmware_prefix)
    fw_compare = firmware_stamp_for_compare(fw_label) if fw_label else (fw_label or "")

    bl_ok = device_meets_bench(device_bootloader, bl_label or "")
    fw_ok = device_meets_bench(device_firmware, fw_compare)
    flash_bl = not bl_ok
    flash_fw = flash_bl or not fw_ok

    plan = ReonboardFlashPlan(
        flash_bootloader=flash_bl,
        flash_firmware=flash_fw,
        bench_bootloader=bl_label,
        bench_firmware=fw_compare or fw_label,
        device_bootloader=device_bootloader,
        device_firmware=device_firmware,
    )
    return plan, bl_zip, fw_zip
