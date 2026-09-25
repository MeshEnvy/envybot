"""ACL table rows for fleet profile."""

from __future__ import annotations

import unittest

from envybot.keys_doc import PERM_ACL_ADMIN, PERM_ACL_RO, AclGrant
from envybot.web.acl_view import build_acl_table_rows
from envybot.web.profile_fields import build_profile_rows


class AclTableRowsTest(unittest.TestCase):
    def test_synced(self) -> None:
        pk = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        want = [AclGrant(pubkey=pk, perm=PERM_ACL_ADMIN, person="ben", role="admin")]
        heard = [{"key": pk, "perm": PERM_ACL_ADMIN}]
        rows = build_acl_table_rows(want, heard, {"ben": [pk]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "good")
        self.assertEqual(rows[0]["prefix4"], "56a3")
        self.assertEqual(rows[0]["person"], "ben")

    def test_missing_on_radio(self) -> None:
        pk = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        want = [AclGrant(pubkey=pk, perm=PERM_ACL_ADMIN, person="ben", role="admin")]
        heard: list[dict] = []
        rows = build_acl_table_rows(want, heard, {})
        self.assertEqual(rows[0]["status"], "dirty")

    def test_no_heard_snapshot(self) -> None:
        pk = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        want = [AclGrant(pubkey=pk, perm=PERM_ACL_ADMIN, person="ben", role="admin")]
        rows = build_acl_table_rows(want, None, {})
        self.assertEqual(rows[0]["status"], "dirty")

    def test_extra_on_radio(self) -> None:
        pk = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        heard = [{"key": pk, "perm": PERM_ACL_RO}]
        rows = build_acl_table_rows([], heard, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "bad")
        self.assertEqual(rows[0]["person"], "nobody")
        self.assertEqual(rows[0]["role"], "guest")

    def test_wrong_perm_dirty(self) -> None:
        pk = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        want = [AclGrant(pubkey=pk, perm=PERM_ACL_ADMIN, person="ben", role="admin")]
        heard = [{"key": pk, "perm": PERM_ACL_RO}]
        rows = build_acl_table_rows(want, heard, {})
        self.assertEqual(rows[0]["status"], "dirty")

    def test_multi_key_same_person(self) -> None:
        pk1 = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        pk2 = "5a081655c143947b11bab00ed26e4307d04483b53fe7a300acb5646c5fa1c690"
        want = [
            AclGrant(pubkey=pk1, perm=PERM_ACL_ADMIN, person="ben", role="admin"),
            AclGrant(pubkey=pk2, perm=PERM_ACL_ADMIN, person="ben", role="admin"),
        ]
        heard = [{"key": pk1, "perm": PERM_ACL_ADMIN}, {"key": pk2, "perm": PERM_ACL_ADMIN}]
        rows = build_acl_table_rows(want, heard, {"ben": [pk1, pk2]})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == "good" for r in rows))

    def test_profile_row_acl_table(self) -> None:
        pk = "56a3a6b28a13d9261bd3a862c8291474a0ce8d90eca28850f33cf453e3840c34"
        doc = {"trust": {"admin": ["ben"]}}
        node: dict = {}
        heard = [{"key": pk, "perm": PERM_ACL_ADMIN}]
        profile = build_profile_rows(
            node,
            None,
            doc=doc,
            keys={"ben": [pk]},
            key="me0001",
            heard_acl=heard,
            acl_heard_at=1_700_000_000,
        )
        acl = next(r for r in profile if r["id"] == "acl")
        self.assertEqual(acl["kind"], "acl_table")
        self.assertFalse(acl["editable"])
        self.assertEqual(len(acl["acl_table"]), 1)
        self.assertEqual(acl["acl_table"][0]["status"], "good")
        self.assertEqual(acl["acl_heard_at"], 1_700_000_000)


if __name__ == "__main__":
    unittest.main()
