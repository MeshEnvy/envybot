"""CLI reply classification for fleet apply."""

from __future__ import annotations

import unittest

from envybot.radio import cli_admin_password_ok, cli_set_ok


class CliAdminPasswordTests(unittest.TestCase):
    def test_password_now_is_success(self) -> None:
        self.assertTrue(
            cli_admin_password_ok("password now: P5fXRoW@X40@pY", "P5fXRoW@X40@pY")
        )

    def test_password_now_mismatch_fails(self) -> None:
        self.assertFalse(cli_admin_password_ok("password now: other", "P5fXRoW@X40@pY"))

    def test_ok_fallback(self) -> None:
        self.assertTrue(cli_admin_password_ok("OK", None))

    def test_set_ok_still_requires_ok(self) -> None:
        self.assertFalse(cli_set_ok("password now: secret"))


if __name__ == "__main__":
    unittest.main()
