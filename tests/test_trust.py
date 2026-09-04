"""Trust contact rows: bag/bench units and name fallback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from meshcore import EventType
from ruamel.yaml import YAML

from envybot.commands.trust import (
    _push_contact_rows,
    build_trust_rows,
    contact_adv_name,
    contact_needs_replace,
    persist_trust_policy,
)
from envybot.keys_doc import TrustPolicy, load_keys
from envybot.nodes_doc import load_nodes_doc
from envybot.radio import (
    CONTACT_FLAG_FAVORITE,
    CONTACT_TYPE_REPEATER,
    PollLog,
    RouterTarget,
    forget_replaced_identities,
    sync_fleet_contacts,
)


PUB_A = "a" * 64
PUB_B = "b" * 64
ADMIN = "AdminOneStrong1"


class ContactAdvNameTests(unittest.TestCase):
    def test_site_name_when_bound(self) -> None:
        node = {"unit_id": "ME0003"}
        sites = {"ophir-hill": {"name": "Ophir", "node": "me0003"}}
        self.assertEqual(contact_adv_name(node, "ME0003", sites, key="me0003"), "Ophir")

    def test_missing_site_uses_unit_id(self) -> None:
        self.assertEqual(contact_adv_name({}, "ME0041", {}), "ME0041")

    def test_blank_site_uses_unit_id(self) -> None:
        self.assertEqual(contact_adv_name({"unit_id": "ME0041"}, "ME0041"), "ME0041")


class ContactNeedsReplaceTests(unittest.TestCase):
    def test_old_advert_name_is_stale(self) -> None:
        existing = {"adv_name": "ME0003 RAK4631 Repeater", "adv_lat": 39.3, "adv_lon": -119.6}
        stub = {"adv_name": "Ophir", "adv_lat": 39.3, "adv_lon": -119.6}
        self.assertTrue(contact_needs_replace(existing, stub))

    def test_region_suffix_matches_site_name(self) -> None:
        existing = {"adv_name": "Ophir {meshenvy.org}", "adv_lat": 0, "adv_lon": 0}
        stub = {"adv_name": "Ophir", "adv_lat": 0, "adv_lon": 0}
        self.assertFalse(contact_needs_replace(existing, stub))

    def test_moved_site_is_stale(self) -> None:
        existing = {"adv_name": "Ophir", "adv_lat": 39.3, "adv_lon": -119.6}
        stub = {"adv_name": "Ophir", "adv_lat": 39.4, "adv_lon": -119.6}
        self.assertTrue(contact_needs_replace(existing, stub))


class BuildTrustRowsTests(unittest.TestCase):
    def test_site_name_or_unit_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            yaml = YAML()
            with (book / "sites.yaml").open("w", encoding="utf-8") as fh:
                yaml.dump(
                    {"sites": {"ophir-hill": {"name": "Ophir", "loc": [39.3, -119.6], "node": "me0003"}}},
                    fh,
                )
            with nodes_path.open("w", encoding="utf-8") as fh:
                yaml.dump(
                    {
                        "next_unit": 42,
                        "nodes": {
                            "me0003": {
                                "unit_id": "ME0003",
                                "identity_pubkey": PUB_A,
                                "admin_password": ADMIN,
                                "firmware_platform": "meshcore",
                            },
                            "me0041": {
                                "unit_id": "ME0041",
                                "identity_pubkey": PUB_B,
                                "admin_password": "AdminTwoStrong2",
                                "firmware_platform": "meshcore",
                            },
                        },
                    },
                    fh,
                )
            rows = {r["unit_id"]: r for r in build_trust_rows(nodes_path)}
            self.assertEqual(rows["ME0003"]["adv_name"], "Ophir")
            self.assertEqual(rows["ME0041"]["adv_name"], "ME0041")
            self.assertIsNone(rows["ME0041"]["site"])
            self.assertEqual(rows["ME0041"]["adv_lat"], 0.0)
            self.assertEqual(rows["ME0041"]["adv_lon"], 0.0)


class PersistPolicyTests(unittest.TestCase):
    def test_writes_keys_and_book_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            yaml = YAML()
            with nodes_path.open("w", encoding="utf-8") as fh:
                yaml.dump({"next_unit": 2, "nodes": {"me0001": {"unit_id": "ME0001"}}}, fh)
            added, _ = persist_trust_policy(
                nodes_path,
                TrustPolicy(person="ben", fleet_role="admin", overrides=()),
                PUB_A,
            )
            self.assertTrue(added)
            self.assertEqual(load_keys(book / "keys.yaml")["ben"], [PUB_A])
            doc = load_nodes_doc(nodes_path)
            self.assertEqual(doc["trust"]["admin"], ["ben"])

    def test_guest_override_on_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            yaml = YAML()
            with (book / "sites.yaml").open("w", encoding="utf-8") as fh:
                yaml.dump({"sites": {}}, fh)
            with nodes_path.open("w", encoding="utf-8") as fh:
                yaml.dump(
                    {
                        "next_unit": 2,
                        "trust": {"admin": ["ben"], "guest": []},
                        "nodes": {
                            "me0001": {
                                "unit_id": "ME0001",
                                "identity_pubkey": PUB_A,
                                "admin_password": ADMIN,
                                "firmware_platform": "meshcore",
                            }
                        },
                    },
                    fh,
                )
            persist_trust_policy(
                nodes_path,
                TrustPolicy(
                    person="ben",
                    fleet_role="admin",
                    overrides=(("me0001", "guest"),),
                ),
                PUB_A,
            )
            node = load_nodes_doc(nodes_path)["nodes"]["me0001"]
            self.assertEqual(node["trust"]["admin"], [])
            self.assertEqual(node["trust"]["guest"], ["ben"])


KEEP = "d" * 64
OLD = "6" * 64
MASK = "2" * 64


def _contact(pk: str, name: str) -> dict:
    return {
        "public_key": pk,
        "adv_name": name,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
        "flags": CONTACT_FLAG_FAVORITE,
        "type": CONTACT_TYPE_REPEATER,
    }


def _client(contacts: dict[str, dict]) -> MagicMock:
    client = MagicMock()
    client.contacts = contacts
    client.auto_update_contacts = False

    def lookup(prefix: str):
        p = prefix.lower()
        for key, row in client.contacts.items():
            pk = str((row or {}).get("public_key") or key).lower()
            if pk.startswith(p):
                return row
        return None

    client.get_contact_by_key_prefix.side_effect = lookup
    ok = SimpleNamespace(type=EventType.OK, payload=None)
    client.commands.remove_contact = AsyncMock(return_value=ok)
    client.commands.add_contact = AsyncMock(return_value=ok)
    client.commands.change_contact_flags = AsyncMock(return_value=ok)
    client.ensure_contacts = AsyncMock()
    client.commands._mesh_request_lock = _NullLock()
    return client


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ForgetReplacedIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_old_me0051_keeps_repeater(self) -> None:
        client = _client(
            {
                KEEP: _contact(KEEP, "ME0051"),
                OLD: _contact(OLD, "ME0051"),
                MASK: _contact(MASK, "Repeater"),
            }
        )
        n = await forget_replaced_identities(
            client,
            keep_pubkey=KEEP,
            names=["ME0051"],
            unit_id="ME0051",
            log=PollLog(progress=False),
        )
        self.assertEqual(n, 1)
        client.commands.remove_contact.assert_awaited_once_with(OLD)
        self.assertIn(KEEP, client.contacts)
        self.assertNotIn(OLD, client.contacts)
        self.assertIn(MASK, client.contacts)

    async def test_push_drops_old_even_when_current_is_current(self) -> None:
        client = _client(
            {
                KEEP: _contact(KEEP, "ME0051"),
                OLD: _contact(OLD, "ME0051"),
                MASK: _contact(MASK, "Repeater"),
            }
        )
        rows = [
            {
                "public_key": KEEP,
                "type": CONTACT_TYPE_REPEATER,
                "flags": CONTACT_FLAG_FAVORITE,
                "adv_name": "ME0051",
                "adv_lat": 0.0,
                "adv_lon": 0.0,
                "unit_id": "ME0051",
                "site": None,
            }
        ]
        n = await _push_contact_rows(client, rows, log=PollLog(progress=False), now=1)
        self.assertEqual(n, 1)
        client.commands.remove_contact.assert_awaited_once_with(OLD)
        client.commands.add_contact.assert_not_awaited()
        self.assertNotIn(OLD, client.contacts)
        self.assertIn(MASK, client.contacts)

    async def test_fleet_sync_drops_old_identity(self) -> None:
        client = _client(
            {
                KEEP: _contact(KEEP, "ME0051 ME0051"),
                OLD: _contact(OLD, "ME0051"),
                MASK: _contact(MASK, "Repeater"),
            }
        )
        target = RouterTarget(
            key="me0051",
            unit_id="ME0051",
            name="ME0051",
            site=None,
            pubkey_hex=KEEP,
            admin_password="pw",
        )
        await sync_fleet_contacts(client, [target], log=PollLog(progress=False))
        client.commands.remove_contact.assert_awaited_once_with(OLD)
        self.assertNotIn(OLD, client.contacts)
        self.assertIn(MASK, client.contacts)


if __name__ == "__main__":
    unittest.main()
