"""Book ACL login skip and clock CLI parse. No radio."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from envybot.nodes_doc import companion_in_desired_acl
from envybot.radio import parse_clock_cli


class AclSkipTests(unittest.TestCase):
    def test_companion_on_trust_list(self) -> None:
        pk = "aa" * 32
        doc = {"trust": {"companions": [{"pubkey": pk}]}}
        self.assertTrue(companion_in_desired_acl(doc, {}, pk))
        self.assertTrue(companion_in_desired_acl(doc, {}, pk[:12]))

    def test_companion_on_admin1(self) -> None:
        pk = "bb" * 32
        node = {"admin1_pubkey": pk}
        self.assertTrue(companion_in_desired_acl({}, node, pk))

    def test_unknown_companion(self) -> None:
        doc = {"trust": {"companions": [{"pubkey": "aa" * 32}]}}
        self.assertFalse(companion_in_desired_acl(doc, {}, "cc" * 32))
        self.assertFalse(companion_in_desired_acl(doc, {}, None))
        self.assertFalse(companion_in_desired_acl({}, {}, "aa" * 32))


class ClockParseTests(unittest.TestCase):
    def test_commoncli_clock(self) -> None:
        ts = parse_clock_cli("14:05 - 30/8/2026 UTC")
        self.assertEqual(ts, int(datetime(2026, 8, 30, 14, 5, tzinfo=timezone.utc).timestamp()))

    def test_junk(self) -> None:
        self.assertIsNone(parse_clock_cli(None))
        self.assertIsNone(parse_clock_cli("OK"))
        self.assertIsNone(parse_clock_cli("25:99 - 1/1/2026 UTC"))


if __name__ == "__main__":
    unittest.main()
