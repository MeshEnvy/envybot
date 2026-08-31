"""Repeater USB CLI framing. No radio."""

from __future__ import annotations

import unittest

from envybot.commands.onboard import RepeaterSerial
from envybot.keys_doc import parse_serial_acl

BEN = "aa" * 32


class ExtractReplyTests(unittest.TestCase):
    def test_arrow_reply(self) -> None:
        buf = "ver\n  -> 1.17.1.1 (Build: 30Aug2026)\n"
        self.assertEqual(
            RepeaterSerial._extract_reply(buf),
            "1.17.1.1 (Build: 30Aug2026)",
        )
        self.assertIsNotNone(RepeaterSerial._reply_start(buf))

    def test_acl_dump_without_arrow(self) -> None:
        buf = f"get acl\nACL:\n03 {BEN}\n"
        reply = RepeaterSerial._extract_reply(buf)
        self.assertIsNotNone(reply)
        self.assertTrue(reply.startswith("ACL:"))
        rows = parse_serial_acl(reply)
        self.assertEqual(rows[0]["perm"], 3)
        self.assertEqual(rows[0]["key"], BEN)

    def test_empty_acl_without_arrow(self) -> None:
        buf = "get acl\nACL:\n"
        reply = RepeaterSerial._extract_reply(buf)
        self.assertEqual(reply, "ACL:")
        self.assertEqual(parse_serial_acl(reply), [])

    def test_no_reply(self) -> None:
        self.assertIsNone(RepeaterSerial._extract_reply("get acl\n"))
        self.assertIsNone(RepeaterSerial._reply_start("get acl\n"))
