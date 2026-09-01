"""Privacy apply due logic. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.apply import (
    apply_due_fields,
    apply_is_due,
    applicable_field_desireds,
    format_apply_plan,
    profile_id,
    profile_parts,
    radio_apply_due_fields,
)
from envybot.history import insert_apply, open_history, record_poll
from envybot.radio import format_book_coord


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = None
        self.bootloader_version = None
        self.firmware_platform = None
        self.name = kwargs.get("name")
        self.lat = kwargs.get("lat")
        self.lon = kwargs.get("lon")
        self.node_clock = None
        self.status = None
        self.telemetry = None
        self.advert_interval_min = kwargs.get("advert_interval_min")
        self.flood_advert_interval_h = None
        self.acl = None
        self.neighbors = None
        self.polled_groups = kwargs.get("polled_groups", frozenset({"name", "lat", "lon"}))


_STRONG = {
    "name": "Ophir",
    "guest_password": "GuestOneStrong1",
    "admin_password": "AdminOneStrong1",
    "identity_pubkey": "aa" * 32,
}


def _id(node, sites=None, doc=None, keys=None):
    return profile_id(node, sites, doc=doc, keys=keys)


class ProfileTests(unittest.TestCase):
    def test_id_is_versioned_hash(self) -> None:
        pid = _id({"name": "Ophir"})
        self.assertTrue(pid.startswith("v1:"))
        self.assertEqual(len(pid), 19)

    def test_guest_change_changes_hash(self) -> None:
        a = {**_STRONG}
        b = {**_STRONG, "guest_password": "GuestTwoStrong2"}
        self.assertNotEqual(_id(a), _id(b))

    def test_admin_change_changes_hash(self) -> None:
        a = {**_STRONG}
        b = {**_STRONG, "admin_password": "AdminTwoStrong2"}
        self.assertNotEqual(_id(a), _id(b))

    def test_identity_change_changes_hash(self) -> None:
        a = {**_STRONG}
        b = {**_STRONG, "identity_pubkey": "bb" * 32}
        self.assertNotEqual(_id(a), _id(b))

    def test_dutycycle_and_path_hash_change_hash(self) -> None:
        a = {**_STRONG}
        self.assertNotEqual(_id(a), _id({**_STRONG, "dutycycle": 50}))
        self.assertNotEqual(_id(a), _id({**_STRONG, "path_hash_mode": 0}))

    def test_acl_change_changes_hash(self) -> None:
        node = {**_STRONG}
        empty = _id(node, doc={"nodes": {"me0001": node}}, keys={})
        with_trust = _id(
            node,
            doc={"nodes": {"me0001": node}, "trust": {"admin": ["ben"]}},
            keys={"ben": ["dd" * 32]},
        )
        self.assertNotEqual(empty, with_trust)

    def test_admin1_does_not_change_hash(self) -> None:
        node = {**_STRONG, "admin1_pubkey": "cc" * 32}
        a = _id(node, doc={"trust": {"admin": ["ben"]}}, keys={"ben": ["aa" * 32]})
        b = _id(_STRONG, doc={"trust": {"admin": ["ben"]}}, keys={"ben": ["aa" * 32]})
        self.assertEqual(a, b)

    def test_public_gps_in_parts(self) -> None:
        node = {**_STRONG, "public": True, "unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8]}}
        parts = profile_parts(node, sites)
        self.assertTrue(parts["public"])
        self.assertEqual(parts["name"], "Ophir")
        self.assertEqual(parts["lat"], 39.5)
        self.assertEqual(parts["lon"], -119.8)
        self.assertNotEqual(_id(node, sites), _id(_STRONG))

    def test_private_mask_in_parts(self) -> None:
        parts = profile_parts(_STRONG, None)
        self.assertFalse(parts["public"])
        self.assertEqual(parts["name"], "Repeater")
        self.assertEqual(parts["lat"], 0.0)
        self.assertEqual(parts["advert"], 0)
        self.assertEqual(parts["path_hash"], 1)
        self.assertEqual(parts["dutycycle"], 100)


class DueTests(unittest.TestCase):
    def test_first_run_private_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            self.assertTrue(apply_is_due(conn, "me0001", {"name": "Ophir"}, None))

    def test_weak_guest_is_due_after_ok_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            node = {"name": "Ophir", "guest_password": "m35h3nvy"}
            insert_apply(conn, unit="me0001", field="profile", desired=_id(node), ok=True)
            self.assertTrue(apply_is_due(conn, "me0001", node, None))

    def test_after_ok_private_not_due_after_leak_heard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            desired = _id(_STRONG)
            insert_apply(conn, unit="me0001", field="profile", desired=desired, ok=True)
            self.assertFalse(apply_is_due(conn, "me0001", _STRONG, None))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(name="Ophir Hill", lat=39.5, lon=-119.8),
            )
            self.assertFalse(apply_is_due(conn, "me0001", _STRONG, None))

    def test_yaml_edit_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            insert_apply(conn, unit="me0001", field="profile", desired=_id(_STRONG), ok=True)
            edited = {**_STRONG, "guest_password": "GuestTwoStrong2"}
            self.assertTrue(apply_is_due(conn, "me0001", edited, None))

    def test_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            insert_apply(conn, unit="me0001", field="profile", desired="private", ok=True)
            self.assertTrue(apply_is_due(conn, "me0001", {}, None, force=True))

    def test_partial_field_sync_only_retries_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None)
            for field in ("lon", "advert", "path_hash"):
                insert_apply(
                    conn, unit="me0001", field=field, desired=applicable[field], ok=True
                )
            due = apply_due_fields(conn, "me0001", _STRONG, None)
            self.assertIn("name", due)
            self.assertIn("lat", due)
            self.assertNotIn("lon", due)
            self.assertNotIn("advert", due)
            self.assertNotIn("path_hash", due)

    def test_identity_only_due_when_radio_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None)
            for field, des in applicable.items():
                if field == "identity":
                    continue
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            due = apply_due_fields(conn, "me0001", _STRONG, None)
            self.assertEqual(due, ["identity"])
            self.assertEqual(radio_apply_due_fields(due), [])

    def test_format_apply_plan_shows_due_fields_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            plan = format_apply_plan(conn, "me0001", _STRONG, None)
            self.assertTrue(plan.startswith("name,") or plan.startswith("name ("))
            self.assertTrue(plan.startswith("v1:") or "(v1:" in plan)


class FormatTests(unittest.TestCase):
    def test_six_decimals(self) -> None:
        self.assertEqual(format_book_coord(39.909448), "39.909448")
        self.assertEqual(format_book_coord(-119.328847), "-119.328847")
        self.assertEqual(format_book_coord(0.0), "0.000000")


if __name__ == "__main__":
    unittest.main()
