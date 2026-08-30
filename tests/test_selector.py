"""Unit tests for fleet selector resolution and cmd redaction."""

from __future__ import annotations

import unittest

from envybot.commands.cmd import redact_snippet, should_redact
from envybot.selector import normalize_adv_name, resolve_selector

FAKE_PUB = "a" * 64
OTHER_PUB = "b" * 64


def sample_doc() -> dict:
    return {
        "next_unit": 42,
        "nodes": {
            "me0016": {
                "unit_id": "ME0016",
                "name": "Poito {meshenvy.org}",
                "identity_pubkey": FAKE_PUB,
                "admin_password": "secret-admin",
            },
            "me0035": {
                "unit_id": "ME0035",
                "name": "PV South",
                "identity_pubkey": OTHER_PUB,
                "admin_password": "other-admin",
            },
            "me0037": {
                "unit_id": "ME0037",
                "name": "PV Peak",
                "identity_pubkey": "c" * 64,
                "admin_password": "pv-admin",
            },
            "me0099": {
                "unit_id": "ME0099",
                "name": "Meshtastic Tag",
                "identity_pubkey": "d" * 64,
                "firmware_platform": "meshtastic",
            },
        },
    }


SAMPLE_SITES = {
    "poito-peak": {"node": "me0016", "name": "Poito"},
    "pv-south": {"node": "me0035", "name": "PV South"},
    "pv-peak": {"node": "me0037", "name": "PV Peak"},
}


class NormalizeAdvNameTests(unittest.TestCase):
    def test_strips_braces_and_pipe(self) -> None:
        self.assertEqual(normalize_adv_name("Poito {meshenvy.org}"), "poito")
        self.assertEqual(normalize_adv_name("Foo | bar"), "foo")


class ResolveSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = sample_doc()

    def test_unit_key(self) -> None:
        res = resolve_selector(self.doc, "me0016")
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.unit_id, "ME0016")

    def test_unit_id(self) -> None:
        res = resolve_selector(self.doc, "ME0016")
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0016")

    def test_normalized_name(self) -> None:
        res = resolve_selector(self.doc, "poito")
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0016")

    def test_exact_name(self) -> None:
        res = resolve_selector(self.doc, "PV Peak")
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0037")

    def test_ambiguous_prefix(self) -> None:
        res = resolve_selector(self.doc, "pv")
        self.assertIsNone(res.target)
        self.assertEqual(res.error, "ambiguous selector")
        self.assertEqual(len(res.candidates), 2)

    def test_unique_site(self) -> None:
        res = resolve_selector(self.doc, "poito-peak", SAMPLE_SITES)
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0016")

    def test_meshtastic_refused(self) -> None:
        res = resolve_selector(self.doc, "me0099")
        self.assertIsNone(res.target)
        self.assertIn("no admin password", res.error or "")

    def test_unknown(self) -> None:
        res = resolve_selector(self.doc, "nosuch")
        self.assertIsNone(res.target)
        self.assertIn("unknown selector", res.error or "")


class RedactionTests(unittest.TestCase):
    def test_password_redacted(self) -> None:
        self.assertTrue(should_redact("set guest.password hunter2"))
        self.assertEqual(redact_snippet("password xyzzy"), "[redacted]")

    def test_plain_reply_preserved(self) -> None:
        self.assertFalse(should_redact("1.2.3 (Build: abc)"))
        self.assertEqual(redact_snippet("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
