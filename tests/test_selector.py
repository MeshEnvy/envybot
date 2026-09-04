"""Unit tests for fleet selector resolution and cmd redaction."""

from __future__ import annotations

import unittest

from envybot.commands.cmd import redact_snippet, should_redact
from envybot.selector import normalize_adv_name, resolve_selector, stale_identity_pubkeys

FAKE_PUB = "a" * 64
OTHER_PUB = "b" * 64


def sample_doc() -> dict:
    return {
        "next_unit": 42,
        "nodes": {
            "me0016": {
                "unit_id": "ME0016",
                "identity_pubkey": FAKE_PUB,
                "admin_password": "secret-admin",
            },
            "me0035": {
                "unit_id": "ME0035",
                "identity_pubkey": OTHER_PUB,
                "admin_password": "other-admin",
            },
            "me0037": {
                "unit_id": "ME0037",
                "identity_pubkey": "c" * 64,
                "admin_password": "pv-admin",
            },
            "me0099": {
                "unit_id": "ME0099",
                "identity_pubkey": "d" * 64,
                "firmware_platform": "meshtastic",
            },
            "me0001": {
                "unit_id": "ME0001",
                "identity_pubkey": "e" * 64,
                "admin_password": "stale-mc-admin",
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


class StaleIdentityTests(unittest.TestCase):
    def test_drops_old_same_name_key(self) -> None:
        keep = "d" * 64
        old = "6" * 64
        other = "2" * 64
        contacts = {
            keep: {"public_key": keep, "adv_name": "ME0051"},
            old: {"public_key": old, "adv_name": "ME0051"},
            other: {"public_key": other, "adv_name": "Repeater"},
        }
        self.assertEqual(
            stale_identity_pubkeys(
                contacts, keep_pubkey=keep, names=["ME0051"], unit_id="ME0051"
            ),
            [old],
        )

    def test_matches_unit_id_prefix_on_stale_advert(self) -> None:
        keep = "a" * 64
        old = "b" * 64
        contacts = {
            keep: {"public_key": keep, "adv_name": "Ophir"},
            old: {"public_key": old, "adv_name": "ME0003 RAK4631 Repeater"},
        }
        self.assertEqual(
            stale_identity_pubkeys(
                contacts, keep_pubkey=keep, names=["Ophir"], unit_id="ME0003"
            ),
            [old],
        )

    def test_ignores_mask_name_and_keep_key(self) -> None:
        keep = "a" * 64
        mask = "c" * 64
        contacts = {
            keep: {"public_key": keep, "adv_name": "ME0051"},
            mask: {"public_key": mask, "adv_name": "Repeater"},
        }
        self.assertEqual(
            stale_identity_pubkeys(
                contacts,
                keep_pubkey=keep,
                names=["ME0051", "Repeater"],
                unit_id="ME0051",
            ),
            [],
        )


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

    def test_site_name(self) -> None:
        res = resolve_selector(self.doc, "poito", SAMPLE_SITES)
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0016")

    def test_exact_site_name(self) -> None:
        res = resolve_selector(self.doc, "PV Peak", SAMPLE_SITES)
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0037")

    def test_ambiguous_prefix(self) -> None:
        res = resolve_selector(self.doc, "pv", SAMPLE_SITES)
        self.assertIsNone(res.target)
        self.assertEqual(res.error, "ambiguous selector")
        self.assertEqual(len(res.candidates), 2)

    def test_unique_site_slug(self) -> None:
        res = resolve_selector(self.doc, "poito-peak", SAMPLE_SITES)
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0016")

    def test_meshtastic_refused(self) -> None:
        res = resolve_selector(self.doc, "me0099")
        self.assertIsNone(res.target)
        self.assertIn("not meshcore", res.error or "")

    def test_meshtastic_with_creds_refused(self) -> None:
        res = resolve_selector(self.doc, "me0001")
        self.assertIsNone(res.target)
        self.assertIn("not meshcore", res.error or "")

    def test_unknown(self) -> None:
        res = resolve_selector(self.doc, "nosuch")
        self.assertIsNone(res.target)
        self.assertIn("unknown selector", res.error or "")

    def test_unique_alias(self) -> None:
        doc = sample_doc()
        doc["nodes"]["me0041"] = {
            "unit_id": "ME0041",
            "alias": "Yuki",
            "identity_pubkey": "f" * 64,
            "admin_password": "bag-admin",
        }
        res = resolve_selector(doc, "yuki")
        self.assertIsNotNone(res.target)
        assert res.target is not None
        self.assertEqual(res.target.key, "me0041")

    def test_ambiguous_alias(self) -> None:
        doc = sample_doc()
        doc["nodes"]["me0041"] = {
            "unit_id": "ME0041",
            "alias": "Bag",
            "identity_pubkey": "f" * 64,
            "admin_password": "bag-admin",
        }
        doc["nodes"]["me0042"] = {
            "unit_id": "ME0042",
            "alias": "Bag",
            "identity_pubkey": "0" * 64,
            "admin_password": "bag-admin2",
        }
        res = resolve_selector(doc, "bag")
        self.assertIsNone(res.target)
        self.assertEqual(res.error, "ambiguous selector")


class RedactionTests(unittest.TestCase):
    def test_password_redacted(self) -> None:
        self.assertTrue(should_redact("set guest.password hunter2"))
        self.assertEqual(redact_snippet("password xyzzy"), "[redacted]")

    def test_plain_reply_preserved(self) -> None:
        self.assertFalse(should_redact("1.2.3 (Build: abc)"))
        self.assertEqual(redact_snippet("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
