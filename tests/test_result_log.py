"""Immediate success-line formatting for fleet progress."""

from __future__ import annotations

import unittest

from envybot.radio import format_binary_ok, format_cli_ok


class FormatBinaryOkTests(unittest.TestCase):
    def test_status(self) -> None:
        self.assertEqual(
            format_binary_ok(
                "GET_STATUS",
                {"uptime": 3661, "bat": 4120, "nb_recv": 10, "nb_sent": 3},
            ),
            "GET_STATUS OK (up=1.0h bat=4.12V rx=10 tx=3)",
        )

    def test_telemetry_temp_volt(self) -> None:
        self.assertEqual(
            format_binary_ok(
                "GET_TELEMETRY",
                [
                    {"type": "temperature", "value": 22.0},
                    {"type": "voltage", "value": 3.98},
                ],
            ),
            "GET_TELEMETRY OK (22.0°C, 3.98V)",
        )

    def test_acl_count(self) -> None:
        self.assertEqual(
            format_binary_ok("GET_ACL", [{}, {}]),
            "GET_ACL OK (2 entries)",
        )

    def test_neighbors_count(self) -> None:
        self.assertEqual(
            format_binary_ok(
                "GET_NEIGHBOURS",
                {"neighbours": [{"pubkey": "aa"}, {"pubkey": "bb"}]},
            ),
            "GET_NEIGHBOURS OK (2 nodes)",
        )

    def test_unknown_label(self) -> None:
        self.assertEqual(format_binary_ok("GET_FOO", {"x": 1}), "GET_FOO OK")


class FormatCliOkTests(unittest.TestCase):
    def test_ver(self) -> None:
        self.assertEqual(
            format_cli_ok("ver", "1.12.0 (Build: abc)"),
            "ver OK (1.12.0 (Build: abc))",
        )

    def test_redacts_password(self) -> None:
        self.assertEqual(
            format_cli_ok("set password secret", "password now: secret"),
            "[redacted] OK ([redacted])",
        )


if __name__ == "__main__":
    unittest.main()
