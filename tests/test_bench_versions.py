"""Bench zip version compare for reonboard flash."""

from __future__ import annotations

import unittest
from pathlib import Path

from envybot.bench_versions import (
    bench_version_tuple,
    bootloader_label_from_zip,
    device_meets_bench,
    firmware_label_from_zip,
    firmware_stamp_for_compare,
    version_tuple_at_least,
)
from envybot.reonboard_flash import plan_reonboard_flash
from envybot.flash_program import TARGETS


class BenchVersionTests(unittest.TestCase):
    def test_firmware_stamp_ev_suffix(self) -> None:
        self.assertEqual(firmware_stamp_for_compare("1.18.0-ev3"), "1.18.0.3")

    def test_device_meets_bench_firmware(self) -> None:
        self.assertTrue(device_meets_bench("1.18.0.3 (Build: x)", "1.18.0-ev3"))
        self.assertFalse(device_meets_bench("1.17.0.1", "1.18.0-ev3"))

    def test_device_meets_bench_bootloader(self) -> None:
        self.assertTrue(device_meets_bench("0.9.2-ev2", "0.9.2-ev2"))
        self.assertFalse(device_meets_bench("0.9.2-ev1", "0.9.2-ev2"))

    def test_unparsed_device_is_behind(self) -> None:
        self.assertFalse(device_meets_bench("ShortTurbo", "1.18.0-ev3"))

    def test_bootloader_label_from_zip(self) -> None:
        path = Path("rak4631_bootloader-0.9.2-ev2.recovery.zip")
        self.assertEqual(bootloader_label_from_zip(path), "0.9.2-ev2")

    def test_firmware_label_from_zip(self) -> None:
        prefix = "meshcore-rak4631-repeater-slim-"
        path = Path(prefix + "1.18.0-ev3.zip")
        self.assertEqual(firmware_label_from_zip(path, firmware_prefix=prefix), "1.18.0-ev3")

    def test_version_tuple_at_least(self) -> None:
        self.assertTrue(version_tuple_at_least((1, 18, 0, 3), (1, 18, 0, 2)))
        have = bench_version_tuple("1.18.0.3")
        need = bench_version_tuple("1.18.0-ev3")
        assert have is not None and need is not None
        self.assertTrue(version_tuple_at_least(have, need))


class ReonboardFlashPlanTests(unittest.TestCase):
    def test_plan_when_current_skips_flash(self) -> None:
        import tempfile

        spec = TARGETS["rak4631-repeater-slim"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bl_bench = root / "build" / "main" / "bench" / "adafruit-nrf52-bootloader-0.9.2-ev2"
            fw_bench = root / "build" / "main" / "bench" / "meshcore-1.18.0-ev3"
            bl_bench.mkdir(parents=True)
            fw_dir = fw_bench / spec.slug
            fw_dir.mkdir(parents=True)
            (bl_bench / "rak4631_bootloader-0.9.2-ev2.recovery.zip").write_bytes(b"x")
            (fw_dir / "meshcore-rak4631-repeater-slim-1.18.0-ev3.zip").write_bytes(b"y")
            plan, _, _ = plan_reonboard_flash(
                device_firmware="1.18.0.3",
                device_bootloader="0.9.2-ev2",
                envyos=root,
                spec=spec,
            )
            self.assertFalse(plan.flash_bootloader)
            self.assertFalse(plan.flash_firmware)

    def test_plan_bootloader_behind_flashes_both(self) -> None:
        import tempfile

        spec = TARGETS["rak4631-repeater-slim"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bl_bench = root / "build" / "main" / "bench" / "adafruit-nrf52-bootloader-0.9.2-ev2"
            fw_bench = root / "build" / "main" / "bench" / "meshcore-1.18.0-ev3"
            bl_bench.mkdir(parents=True)
            fw_dir = fw_bench / spec.slug
            fw_dir.mkdir(parents=True)
            (bl_bench / "rak4631_bootloader-0.9.2-ev2.recovery.zip").write_bytes(b"x")
            (fw_dir / "meshcore-rak4631-repeater-slim-1.18.0-ev3.zip").write_bytes(b"y")
            plan, _, _ = plan_reonboard_flash(
                device_firmware="1.18.0.3",
                device_bootloader="0.9.2-ev1",
                envyos=root,
                spec=spec,
            )
            self.assertTrue(plan.flash_bootloader)
            self.assertTrue(plan.flash_firmware)

    def test_plan_firmware_only_behind(self) -> None:
        import tempfile

        spec = TARGETS["rak4631-repeater-slim"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bl_bench = root / "build" / "main" / "bench" / "adafruit-nrf52-bootloader-0.9.2-ev2"
            fw_bench = root / "build" / "main" / "bench" / "meshcore-1.18.0-ev3"
            bl_bench.mkdir(parents=True)
            fw_dir = fw_bench / spec.slug
            fw_dir.mkdir(parents=True)
            (bl_bench / "rak4631_bootloader-0.9.2-ev2.recovery.zip").write_bytes(b"x")
            (fw_dir / "meshcore-rak4631-repeater-slim-1.18.0-ev3.zip").write_bytes(b"y")
            plan, _, _ = plan_reonboard_flash(
                device_firmware="1.17.0.1",
                device_bootloader="0.9.2-ev2",
                envyos=root,
                spec=spec,
            )
            self.assertFalse(plan.flash_bootloader)
            self.assertTrue(plan.flash_firmware)
