"""USB port autodetect for onboard — probe then prompt."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from envybot.onboard_usb import pick_port
from envybot.repeater_serial import PortProbe


class PickPortTests(unittest.TestCase):
    def test_explicit_port_skips_probe(self) -> None:
        with patch("envybot.onboard_usb.resolve_port_arg", return_value="/dev/cu.x"), patch(
            "envybot.flash_program.ensure_serial_port_available",
            side_effect=lambda p, **_: p,
        ):
            self.assertEqual(
                pick_port("cu.x", baud=115200, timeout=5.0, verbose=False),
                "/dev/cu.x",
            )

    def test_single_port_still_prompts_on_tty(self) -> None:
        ports = ["/dev/cu.a"]
        with patch("envybot.onboard_usb.serial_port_candidates", return_value=ports), patch(
            "envybot.onboard_usb.probe_repeater",
            return_value=PortProbe(ver="MeshCore 1.17"),
        ), patch(
            "envybot.flash_program.ensure_serial_port_available",
            side_effect=lambda p, **_: p,
        ), patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", return_value="1"
        ):
            got = pick_port(None, baud=115200, timeout=5.0, verbose=False)
        self.assertEqual(got, "/dev/cu.a")

    def test_multiple_ports_one_hit_still_prompts(self) -> None:
        ports = ["/dev/cu.a", "/dev/cu.b"]

        def probe(port: str, **_k: object) -> PortProbe:
            return PortProbe(ver="ver") if port == "/dev/cu.a" else PortProbe(note="skip")

        with patch("envybot.onboard_usb.serial_port_candidates", return_value=ports), patch(
            "envybot.onboard_usb.probe_repeater", side_effect=probe
        ), patch(
            "envybot.flash_program.ensure_serial_port_available",
            side_effect=lambda p, **_: p,
        ), patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", return_value="1"
        ):
            got = pick_port(None, baud=115200, timeout=5.0, verbose=False)
        self.assertEqual(got, "/dev/cu.a")

    def test_rejects_busy_port_pick(self) -> None:
        ports = ["/dev/cu.a", "/dev/cu.b"]
        probes = {
            "/dev/cu.a": PortProbe(ver="1.0"),
            "/dev/cu.b": PortProbe(note="port busy (close other serial sessions)"),
        }

        def probe(port: str, **_k: object) -> PortProbe:
            return probes[port]

        with patch("envybot.onboard_usb.serial_port_candidates", return_value=ports), patch(
            "envybot.onboard_usb.probe_repeater", side_effect=probe
        ), patch(
            "envybot.flash_program.ensure_serial_port_available", side_effect=lambda p, **_: p
        ), patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", side_effect=["2", "1"]
        ):
            got = pick_port(None, baud=115200, timeout=5.0, verbose=False)
        self.assertEqual(got, "/dev/cu.a")

    def test_non_tty_lists_all_probed_ports(self) -> None:
        ports = ["/dev/cu.a", "/dev/cu.b"]
        with patch("envybot.onboard_usb.serial_port_candidates", return_value=ports), patch(
            "envybot.onboard_usb.probe_repeater",
            return_value=PortProbe(note="no repeater CLI on `ver`"),
        ), patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                pick_port(None, baud=115200, timeout=5.0, verbose=False)
        msg = str(ctx.exception)
        self.assertIn("Pass the USB path", msg)
        self.assertIn("/dev/cu.a", msg)
        self.assertIn("/dev/cu.b", msg)


if __name__ == "__main__":
    unittest.main()
