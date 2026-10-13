"""Flash artifact discovery and USB phase logic (no hardware)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from envybot.flash_program import (
    UsbPhase,
    _newest_file,
    classify_usb,
    ensure_bootloader_port,
    flash_bootloader_package,
    resolve_bootloader_zip,
    resolve_firmware_artifacts,
    resolve_target,
)
from envybot.commands.onboard import CliError


XIAO = resolve_target("xiao-nrf52-repeater-slim")


class ArtifactDiscoveryTests(unittest.TestCase):
    def test_newest_file_by_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "a-1.zip"
            new = root / "a-2.zip"
            old.write_bytes(b"1")
            new.write_bytes(b"2")
            import os
            import time

            time.sleep(0.02)
            os.utime(new, (time.time() + 2, time.time() + 2))
            hit = _newest_file(root, "a-*.zip")
            self.assertEqual(hit, new)

    def test_resolve_bootloader_from_bench(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envyos = Path(tmp)
            bench = envyos / "build" / "main" / "bench" / "adafruit-nrf52-bootloader-0.9.2-ev2"
            bench.mkdir(parents=True)
            zpath = bench / "xiao_nrf52840_ble_bootloader-0.9.2-ev2.recovery.zip"
            zpath.write_bytes(b"zip")
            got = resolve_bootloader_zip(envyos, XIAO)
            self.assertEqual(got, zpath)

    def test_resolve_firmware_zip_and_uf2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envyos = Path(tmp)
            slug_dir = (
                envyos
                / "build"
                / "main"
                / "bench"
                / "meshcore-1.17.1-ev1"
                / "xiao-nrf52-repeater-slim"
            )
            slug_dir.mkdir(parents=True)
            zpath = slug_dir / "meshcore-xiao-nrf52-repeater-slim-1.17.1-ev1.zip"
            uf2 = slug_dir / "meshcore-xiao-nrf52-repeater-slim-1.17.1-ev1.uf2"
            zpath.write_bytes(b"z")
            uf2.write_bytes(b"u")
            got_z, got_u = resolve_firmware_artifacts(envyos, XIAO)
            self.assertEqual(got_z, zpath)
            self.assertEqual(got_u, uf2)


class UsbPhaseTests(unittest.TestCase):
    def test_classify_bootloader_vid_pid(self) -> None:
        fake = mock.Mock()
        fake.device = "/dev/cu.usbmodem1"
        fake.vid = XIAO.bl_vid
        fake.pid = XIAO.bl_pid
        with mock.patch("envybot.flash_program.list_ports") as lp:
            lp.comports.return_value = [fake]
            with mock.patch("envybot.flash_program.uf2_mount_path", return_value=None):
                phase, port = classify_usb(XIAO)
        self.assertEqual(phase, UsbPhase.BOOTLOADER)
        self.assertEqual(port, "/dev/cu.usbmodem1")

    def test_classify_prefers_cdc_when_uf2_volume_is_mounted(self) -> None:
        fake = mock.Mock()
        fake.device = "/dev/cu.usbmodem1"
        fake.vid = XIAO.bl_vid
        fake.pid = XIAO.bl_pid
        with mock.patch("envybot.flash_program.list_ports") as lp:
            lp.comports.return_value = [fake]
            with mock.patch("envybot.flash_program.uf2_mount_path", return_value=Path("/Volumes/XIAO-BOOT")):
                phase, port = classify_usb(XIAO)
        self.assertEqual(phase, UsbPhase.BOOTLOADER)
        self.assertEqual(port, "/dev/cu.usbmodem1")

    def test_uf2_only_waits_for_cdc(self) -> None:
        states = [
            (UsbPhase.UF2_VOLUME, None),
            (UsbPhase.UF2_VOLUME, None),
            (UsbPhase.BOOTLOADER, "/dev/cu.bl"),
        ]

        def poll() -> tuple[UsbPhase, str | None]:
            return states.pop(0)

        with mock.patch("envybot.flash_program.time.sleep"):
            port = ensure_bootloader_port(XIAO, port=None, wait_s=30.0, poll=poll)
        self.assertEqual(port, "/dev/cu.bl")

    def test_ensure_bootloader_skips_touch_when_already_bl(self) -> None:
        calls: list[list[str]] = []

        def runner(cmd: list[str]) -> None:
            calls.append(cmd)

        def poll() -> tuple[UsbPhase, str | None]:
            return UsbPhase.BOOTLOADER, "/dev/cu.bl"

        with tempfile.NamedTemporaryFile(suffix=".zip") as zf:
            path = Path(zf.name)
            path.write_bytes(b"x")
            flash_bootloader_package(
                path,
                XIAO,
                runner=runner,
                poll=poll,
            )
        self.assertEqual(len(calls), 1)
        self.assertIn("dfu", calls[0])
        self.assertNotIn("--touch", calls[0])

    def test_touch_then_bootloader_poll(self) -> None:
        touched: list[str] = []
        states = [
            (UsbPhase.APP, "/dev/cu.app"),
            (UsbPhase.NONE, None),
            (UsbPhase.BOOTLOADER, "/dev/cu.bl"),
        ]

        def poll() -> tuple[UsbPhase, str | None]:
            return states.pop(0) if states else (UsbPhase.BOOTLOADER, "/dev/cu.bl")

        def touch(port: str) -> None:
            touched.append(port)

        runner_calls: list[list[str]] = []

        def runner(cmd: list[str]) -> None:
            runner_calls.append(cmd)

        with mock.patch("envybot.flash_program.touch_1200", side_effect=touch):
            with mock.patch("envybot.flash_program.wait_port_gone", return_value=True):
                port = ensure_bootloader_port(
                    XIAO,
                    port=None,
                    wait_s=2.0,
                    poll=poll,
                )
        self.assertEqual(port, "/dev/cu.bl")
        self.assertEqual(touched, ["/dev/cu.app"])


class OnboardFlashOrderTests(unittest.TestCase):
    def test_onboard_calls_bootloader_then_firmware_before_pick_port(self) -> None:
        order: list[str] = []

        def fake_bl(*_a: object, **_k: object) -> str:
            order.append("bootloader")
            return "p"

        def fake_fw(*_a: object, **_k: object) -> str | None:
            order.append("firmware")
            return "p"

        def pick_port_ok(*_a: object, **_k: object) -> str:
            order.append("pick_port")
            return "/dev/cu.x"

        with mock.patch(
            "envybot.flash_program.flash_bootloader_package",
            side_effect=fake_bl,
        ), mock.patch(
            "envybot.flash_program.flash_firmware_package",
            side_effect=fake_fw,
        ), mock.patch(
            "envybot.flash_program.resolve_bootloader_zip",
            return_value=Path("/tmp/bl.zip"),
        ), mock.patch(
            "envybot.flash_program.resolve_firmware_artifacts",
            return_value=(Path("/tmp/fw.zip"), None),
        ), mock.patch(
            "envybot.commands.onboard.pick_port",
            side_effect=pick_port_ok,
        ), mock.patch(
            "envybot.commands.onboard.open_repeater",
            side_effect=CliError("stop"),
        ), mock.patch("envybot.commands.onboard.time.sleep"):
            from envybot.commands import onboard

            rc = onboard.main(
                [
                    "--with-flash-bootloader",
                    "--with-flash-firmware",
                    "--target",
                    "xiao-nrf52-repeater-slim",
                ]
            )
        self.assertEqual(rc, 1)
        self.assertEqual(order, ["bootloader", "firmware", "pick_port"])
