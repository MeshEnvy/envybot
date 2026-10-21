"""USB adopt blank book fields before reonboard."""

from __future__ import annotations

import unittest
from typing import Any

from envybot.keys_doc import PERM_ACL_ADMIN
from envybot.onboard_adopt import (
    adopt_radio_blanks,
    adopt_trust_from_heard_acl,
    verify_reonboard_usb_pubkey,
)
from envybot.onboard_book import ensure_reonboard_unit
from envybot.repeater_serial import CliError


class FakeCli:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.sent: list[str] = []

    def cmd(self, line: str) -> str:
        self.sent.append(line)
        if line in self.replies:
            return self.replies[line]
        for key, val in self.replies.items():
            if line.startswith(key.split()[0]) and key in line:
                return val
        return ""


PUB = "a" * 64
SECRET = "d" * 128
BEN = "b" * 64
OTHER = "c" * 64


class EnsureReonboardUnitTests(unittest.TestCase):
    def test_allows_missing_secret(self) -> None:
        nodes = {"me0024": {"unit_id": "ME0024", "identity_pubkey": PUB}}
        key, node = ensure_reonboard_unit(nodes, unit="me0024")
        self.assertEqual(key, "me0024")

    def test_still_requires_unit(self) -> None:
        with self.assertRaises(SystemExit):
            ensure_reonboard_unit({}, unit=None)


class VerifyPubkeyTests(unittest.TestCase):
    def test_mismatch_raises(self) -> None:
        node = {"identity_pubkey": PUB}
        with self.assertRaises(CliError):
            verify_reonboard_usb_pubkey(node, OTHER)


class AdoptTrustTests(unittest.TestCase):
    def test_merges_book_admin_and_heard_person(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        node: dict[str, Any] = {}
        keys = {"ben": [BEN], "alice": [OTHER]}
        heard = [{"key": OTHER, "perm": PERM_ACL_ADMIN}]
        changed, unknown = adopt_trust_from_heard_acl(doc, node, keys, heard)
        self.assertTrue(changed)
        self.assertEqual(unknown, [])
        self.assertEqual(node["trust"]["admin"], ["ben", "alice"])

    def test_unknown_prefix_reported(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        node: dict[str, Any] = {}
        keys = {"ben": [BEN]}
        unknown_pk = "f" * 64
        heard = [{"key": unknown_pk, "perm": PERM_ACL_ADMIN}]
        changed, unknown = adopt_trust_from_heard_acl(doc, node, keys, heard)
        self.assertFalse(changed)
        self.assertEqual(len(unknown), 1)


class AdoptRadioBlanksTests(unittest.TestCase):
    def test_adopts_secret_when_pubkey_matches(self) -> None:
        doc: dict[str, Any] = {"trust": {"admin": ["ben"], "guest": []}}
        node: dict[str, Any] = {"identity_pubkey": PUB, "admin_password": "AdminOneStrong1"}
        keys = {"ben": [BEN]}
        cli = FakeCli(
            {
                "get prv.key": f"> {SECRET}",
                "get public.key": f"> {PUB}",
                "get acl": "ACL:\n",
            }
        )
        adopted = adopt_radio_blanks(cli, doc, node, keys, usb_pubkey=PUB)
        self.assertIn("identity_secret", adopted)
        self.assertEqual(node["identity_secret"], SECRET)

    def test_keeps_explicit_blank_guest(self) -> None:
        doc = {"trust": {"admin": [], "guest": []}}
        node = {
            "identity_pubkey": PUB,
            "identity_secret": SECRET,
            "guest_password": "",
            "admin_password": "AdminOneStrong1",
        }
        keys: dict[str, list[str]] = {}
        cli = FakeCli({"get acl": "ACL:\n"})
        adopted = adopt_radio_blanks(cli, doc, node, keys, usb_pubkey=PUB)
        self.assertNotIn("guest_password", adopted)

    def test_adopts_absent_guest_key(self) -> None:
        doc = {"trust": {"admin": [], "guest": []}}
        node = {
            "identity_pubkey": PUB,
            "identity_secret": SECRET,
            "admin_password": "AdminOneStrong1",
        }
        keys: dict[str, list[str]] = {}
        cli = FakeCli(
            {
                "get guest.password": "> secretguest",
                "get acl": "ACL:\n",
            }
        )
        adopted = adopt_radio_blanks(cli, doc, node, keys, usb_pubkey=PUB)
        self.assertIn("guest_password", adopted)
        self.assertEqual(node["guest_password"], "secretguest")
