"""keys.yaml resolve, inherit, ACL plan, trust CLI parse. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.keys_doc import (
    PERM_ACL_ADMIN,
    PERM_ACL_DROP,
    PERM_ACL_RO,
    TrustError,
    UnknownPerson,
    add_book_role,
    apply_node_override,
    companion_in_desired_acl,
    load_keys,
    parse_serial_acl,
    parse_trust_policy,
    person_has_key,
    plan_acl_ops,
    remember_person_key,
    resolve_node_acl,
    write_keys,
)

BEN = "aa" * 32
BEN2 = "ab" * 32
BILL = "bb" * 32
EXTRA = "cc" * 32


class RememberTests(unittest.TestCase):
    def test_add_and_idempotent(self) -> None:
        keys: dict[str, list[str]] = {}
        self.assertTrue(remember_person_key(keys, "Ben", BEN))
        self.assertFalse(remember_person_key(keys, "ben", BEN))
        self.assertEqual(keys["ben"], [BEN])

    def test_reject_short_hex(self) -> None:
        with self.assertRaises(TrustError):
            remember_person_key({}, "ben", "aa" * 12)


class ResolveTests(unittest.TestCase):
    def test_inherit_book_admin(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        keys = {"ben": [BEN, BEN2]}
        grants = resolve_node_acl(doc, {}, keys)
        self.assertEqual([g.pubkey for g in grants], [BEN, BEN2])
        self.assertTrue(all(g.perm == PERM_ACL_ADMIN for g in grants))

    def test_node_guest_keeps_book_admin(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        node = {"trust": {"guest": ["bill"]}}
        keys = {"ben": [BEN], "bill": [BILL]}
        grants = resolve_node_acl(doc, node, keys)
        by_pk = {g.pubkey: g for g in grants}
        self.assertEqual(by_pk[BEN].perm, PERM_ACL_ADMIN)
        self.assertEqual(by_pk[BILL].perm, PERM_ACL_RO)

    def test_empty_admin_clears(self) -> None:
        doc = {"trust": {"admin": ["ben"]}}
        node = {"trust": {"admin": []}}
        self.assertEqual(resolve_node_acl(doc, node, {"ben": [BEN]}), [])

    def test_unknown_person_raises(self) -> None:
        doc = {"trust": {"admin": ["ghost"]}}
        with self.assertRaises(UnknownPerson):
            resolve_node_acl(doc, {}, {})

    def test_admin1_ignored(self) -> None:
        doc = {"trust": {"admin": ["ben"]}}
        node = {"admin1_pubkey": EXTRA}
        grants = resolve_node_acl(doc, node, {"ben": [BEN]})
        self.assertEqual([g.pubkey for g in grants], [BEN])

    def test_companions_ignored(self) -> None:
        doc = {"trust": {"companions": [{"pubkey": EXTRA}], "admin": ["ben"]}}
        grants = resolve_node_acl(doc, {}, {"ben": [BEN]})
        self.assertEqual([g.pubkey for g in grants], [BEN])


class CompanionSkipTests(unittest.TestCase):
    def test_admin_key_matches(self) -> None:
        doc = {"trust": {"admin": ["ben"]}}
        keys = {"ben": [BEN]}
        self.assertTrue(companion_in_desired_acl(doc, {}, BEN, keys))
        self.assertTrue(companion_in_desired_acl(doc, {}, BEN[:12], keys))
        self.assertFalse(companion_in_desired_acl(doc, {}, BILL, keys))

    def test_guest_does_not_skip(self) -> None:
        doc = {"trust": {"guest": ["bill"]}}
        keys = {"bill": [BILL]}
        self.assertFalse(companion_in_desired_acl(doc, {}, BILL, keys))


class PlanTests(unittest.TestCase):
    def test_set_and_drop(self) -> None:
        from envybot.keys_doc import AclGrant

        want = [AclGrant(BEN, PERM_ACL_ADMIN, "ben", "admin")]
        heard = [{"key": EXTRA[:16], "perm": 3}]
        ops = plan_acl_ops(want, heard)
        self.assertEqual(ops[0].key, BEN)
        self.assertEqual(ops[0].perm, PERM_ACL_ADMIN)
        self.assertEqual(ops[1].perm, PERM_ACL_DROP)
        self.assertTrue(ops[1].key.startswith(EXTRA[:12]))

    def test_skip_matching_heard(self) -> None:
        from envybot.keys_doc import AclGrant

        want = [AclGrant(BEN, PERM_ACL_ADMIN, "ben", "admin")]
        heard = [{"key": BEN, "perm": PERM_ACL_ADMIN}]
        self.assertEqual(plan_acl_ops(want, heard), [])

    def test_empty_policy_no_ops(self) -> None:
        self.assertEqual(plan_acl_ops([], [{"key": EXTRA, "perm": 3}])[0].perm, PERM_ACL_DROP)


class SerialAclTests(unittest.TestCase):
    def test_parse_lines(self) -> None:
        text = f"ACL:\n03 {BEN}\n01 {BILL}\n"
        rows = parse_serial_acl(text)
        self.assertEqual(rows[0]["perm"], 3)
        self.assertEqual(rows[0]["key"], BEN)
        self.assertEqual(rows[1]["perm"], 1)


class PolicyParseTests(unittest.TestCase):
    def test_ben_default_admin(self) -> None:
        p = parse_trust_policy(["ben"])
        self.assertEqual(p.person, "ben")
        self.assertEqual(p.fleet_role, "admin")
        self.assertEqual(p.overrides, ())

    def test_bill_guest(self) -> None:
        p = parse_trust_policy(["bill:guest"])
        self.assertEqual((p.person, p.fleet_role), ("bill", "guest"))

    def test_override(self) -> None:
        p = parse_trust_policy(["ben", "me0016:guest"])
        self.assertEqual(p.overrides, (("me0016", "guest"),))

    def test_bad_override(self) -> None:
        with self.assertRaises(TrustError):
            parse_trust_policy(["ben", "me0016"])


class BookWriteTests(unittest.TestCase):
    def test_add_book_role_moves_sides(self) -> None:
        doc: dict = {"trust": {"admin": ["ben"], "guest": ["bill"]}}
        add_book_role(doc, "bill", "admin")
        self.assertIn("bill", doc["trust"]["admin"])
        self.assertNotIn("bill", doc["trust"]["guest"])

    def test_guest_override_drops_admin(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        node: dict = {}
        self.assertTrue(apply_node_override(doc, node, "ben", "guest"))
        self.assertEqual(node["trust"]["admin"], [])
        self.assertEqual(node["trust"]["guest"], ["ben"])

    def test_matching_inherit_writes_nothing(self) -> None:
        doc = {"trust": {"admin": ["ben"], "guest": []}}
        node: dict = {}
        self.assertFalse(apply_node_override(doc, node, "ben", "admin"))
        self.assertNotIn("trust", node)


class KeysFileTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.yaml"
            keys = {"ben": [BEN]}
            write_keys(path, keys)
            loaded = load_keys(path)
            self.assertTrue(person_has_key(loaded, "ben", BEN))


if __name__ == "__main__":
    unittest.main()
