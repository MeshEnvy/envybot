"""Companion out_path formatting and path logging before mesh sends."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from envybot.radio import RouterTarget, contact_out_path_label, log_contact_path, PollLog


class ContactOutPathTests(unittest.TestCase):
    def test_two_byte_hops(self) -> None:
        label = contact_out_path_label(
            {
                "out_path_len": 2,
                "out_path_hash_mode": 1,
                "out_path": "a1b2c3d4",
            }
        )
        self.assertEqual(label, "a1b2 c3d4")

    def test_flood_contact_returns_none(self) -> None:
        self.assertIsNone(
            contact_out_path_label(
                {"out_path_len": -1, "out_path_hash_mode": -1, "out_path": ""}
            )
        )
        self.assertIsNone(contact_out_path_label(None))

    def test_log_direct_path(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": 2,
            "out_path_hash_mode": 1,
            "out_path": "266ab3b3",
        }
        target = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="test",
            site=None,
            pubkey_hex="b2f84713d830" + "0" * 52,
            admin_password="pw",
        )
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, target, log=CaptureLog())
        self.assertEqual(lines, ["path: 266a b3b3"])

    def test_log_flood_when_no_path(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": -1,
            "out_path_hash_mode": -1,
            "out_path": "",
        }
        target = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="test",
            site=None,
            pubkey_hex="b2f84713d830" + "0" * 52,
            admin_password="pw",
        )
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, target, log=CaptureLog())
        self.assertEqual(lines, ["path: flood"])


if __name__ == "__main__":
    unittest.main()
