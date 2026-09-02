"""CLI reply classification for fleet apply."""

from __future__ import annotations

import unittest

from envybot.radio import (
    cli_admin_password_ok,
    cli_set_ok,
    ota_self_heard_empty,
    parse_ota_self,
)


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


class ParseOtaSelfTests(unittest.TestCase):
    def test_success(self) -> None:
        raw = "self body=123456 image=123512 base_hash=aabbccddeeff0011"
        self.assertEqual(parse_ota_self(raw), "AABBCCDDEEFF0011")

    def test_success_with_bootloader_suffix(self) -> None:
        raw = (
            "self body=123456 image=123512 base_hash=0011223344556677"
            " | bootloader: apply OK (abi=1 codecs=0x4)"
        )
        self.assertEqual(parse_ota_self(raw), "0011223344556677")

    def test_no_endf(self) -> None:
        self.assertEqual(parse_ota_self("ERR no EndF (firmware lacks the trailer?)"), "")

    def test_unknown_command(self) -> None:
        self.assertEqual(parse_ota_self("Unknown command"), "")

    def test_unknown_ota_command(self) -> None:
        self.assertEqual(parse_ota_self("Unknown OTA command. Type `ota help`."), "")

    def test_unparsed(self) -> None:
        self.assertIsNone(parse_ota_self("garbage reply"))

    def test_heard_empty_helper(self) -> None:
        self.assertTrue(ota_self_heard_empty("Unknown command"))
        self.assertFalse(ota_self_heard_empty("self body=1 image=2 base_hash=0011223344556677"))


if __name__ == "__main__":
    unittest.main()
