"""Privacy apply due logic. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.apply import (
    apply_due_fields,
    apply_is_due,
    applicable_field_desireds,
    desired_agc_reset_interval,
    desired_fem_rxgain,
    desired_ota_autofetch,
    desired_powersaving,
    desired_rxgain,
    format_apply_plan,
    node_board_token,
    profile_id,
    profile_parts,
    radio_apply_due_fields,
    rxgain_apply_enabled,
    stamp_profile_after_onboard,
)
from envybot.history import insert_apply, last_ok_apply, open_history, record_poll
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
    "guest_password": "GuestOneStrong1",
    "admin_password": "AdminOneStrong1",
    "identity_pubkey": "aa" * 32,
}


def _id(node, sites=None, doc=None, keys=None):
    return profile_id(node, sites, doc=doc, keys=keys)


class ProfileTests(unittest.TestCase):
    def test_id_is_versioned_hash(self) -> None:
        pid = _id(_STRONG)
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
        self.assertNotEqual(_id(a), _id({**_STRONG, "dutycycle": 100}))
        self.assertNotEqual(_id(a), _id({**_STRONG, "path_hash_mode": 0}))

    def test_ota_autofetch_change_hash(self) -> None:
        a = {**_STRONG}
        self.assertNotEqual(_id(a), _id({**_STRONG, "ota_autofetch": "any"}))

    def test_desired_ota_autofetch_defaults_off(self) -> None:
        self.assertEqual(desired_ota_autofetch({}), "off")
        self.assertEqual(desired_ota_autofetch(_STRONG), "off")
        self.assertEqual(desired_ota_autofetch({**_STRONG, "ota_autofetch": "signed"}), "signed")
        self.assertEqual(desired_ota_autofetch({**_STRONG, "ota_autofetch": "bogus"}), "off")

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

    def test_site_bound_gps_in_parts(self) -> None:
        node = {**_STRONG, "unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8], "name": "Ophir", "advert_name": "Ophir"}}
        doc = {
            "public_advert": {
                "name_suffix": " {lora.sh}",
                "location_accuracy_mi": 1.5,
                "location_salt": "test-book-salt-not-for-production",
                "owner_info": "MeshEnvy NCC",
            }
        }
        parts = profile_parts(node, sites, doc=doc, key="me0003")
        self.assertEqual(parts["name"], "Ophir {lora.sh}")
        self.assertNotEqual(parts["lat"], 39.5)
        self.assertEqual(parts["owner"], "MeshEnvy NCC")
        self.assertTrue(parts["repeat"])
        self.assertEqual(parts["advert"], 0)
        self.assertEqual(parts["flood"], 12)
        self.assertNotEqual(_id(node, sites, doc=doc), _id(_STRONG))

    def test_exact_coords_when_accuracy_zero(self) -> None:
        node = {**_STRONG, "unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8], "name": "Ophir", "advert_name": "Ophir"}}
        doc = {"public_advert": {"location_accuracy_mi": 0}}
        parts = profile_parts(node, sites, doc=doc, key="me0003")
        self.assertEqual(parts["lat"], 39.5)
        self.assertEqual(parts["lon"], -119.8)

    def test_unbound_bench_in_parts(self) -> None:
        parts = profile_parts(_STRONG, None, key="me0001")
        self.assertEqual(parts["name"], "ME0001")
        self.assertEqual(parts["lat"], 0.0)
        self.assertEqual(parts["advert"], 0)
        self.assertEqual(parts["path_hash"], 1)
        self.assertEqual(parts["dutycycle"], 50)
        self.assertEqual(parts["ota_autofetch"], "off")
        self.assertTrue(parts["fem_rxgain"])
        self.assertEqual(parts["agc_reset_interval"], 4)

    def test_ota_autofetch_in_parts(self) -> None:
        parts = profile_parts({**_STRONG, "ota_autofetch": "any"}, None)
        self.assertEqual(parts["ota_autofetch"], "any")

    def test_optional_power_prefs_omitted_until_set(self) -> None:
        parts = profile_parts(_STRONG, None)
        self.assertNotIn("powersaving", parts)
        self.assertNotIn("rxgain", parts)
        self.assertTrue(parts["fem_rxgain"])
        self.assertEqual(parts["agc_reset_interval"], 4)
        self.assertEqual(_id(_STRONG), _id({**_STRONG}))
        self.assertNotEqual(_id(_STRONG), _id({**_STRONG, "powersaving": True}))
        self.assertNotEqual(_id(_STRONG), _id({**_STRONG, "fem_rxgain": False}))
        self.assertNotEqual(_id(_STRONG), _id({**_STRONG, "agc_reset_interval": 8}))
        self.assertEqual(_id(_STRONG), _id({**_STRONG, "rxgain": False}))
        self.assertNotEqual(
            _id(_STRONG),
            _id({**_STRONG, "board": "heltec-t096", "rxgain": False}),
        )

    def test_desired_rxgain_alias(self) -> None:
        self.assertIsNone(desired_rxgain({}))
        self.assertFalse(desired_rxgain({**_STRONG, "rxgain": False}))
        self.assertFalse(desired_rxgain({**_STRONG, "radio.rxgain": "off"}))
        self.assertTrue(desired_rxgain({**_STRONG, "rxgain": "on"}))

    def test_desired_powersaving_optional(self) -> None:
        self.assertIsNone(desired_powersaving({}))
        self.assertIsNone(desired_powersaving(_STRONG))
        self.assertTrue(desired_powersaving({**_STRONG, "powersaving": True}))
        self.assertTrue(desired_powersaving({**_STRONG, "powersaving": "on"}))
        self.assertFalse(desired_powersaving({**_STRONG, "powersaving": "off"}))

    def test_desired_fem_rxgain_alias(self) -> None:
        self.assertTrue(desired_fem_rxgain({}))
        self.assertFalse(desired_fem_rxgain({**_STRONG, "fem_rxgain": False}))
        self.assertFalse(desired_fem_rxgain({**_STRONG, "radio.fem.rxgain": "off"}))
        self.assertTrue(desired_fem_rxgain({**_STRONG, "fem_rxgain": "on"}))

    def test_desired_agc_reset_interval(self) -> None:
        self.assertEqual(desired_agc_reset_interval({}), 4)
        self.assertEqual(desired_agc_reset_interval({**_STRONG, "agc_reset_interval": 8}), 8)
        self.assertEqual(desired_agc_reset_interval({**_STRONG, "agc_reset_interval": 5}), 4)
        self.assertEqual(desired_agc_reset_interval({**_STRONG, "agc_reset_interval": "bogus"}), 4)


class BoardTokenTests(unittest.TestCase):
    def test_reads_board_only(self) -> None:
        self.assertEqual(node_board_token({"board": "Heltec T096"}), "heltec-t096")
        self.assertEqual(node_board_token({"board": "rak4631"}), "rak4631")
        self.assertEqual(node_board_token({}), "")
        self.assertEqual(node_board_token({"hardware": "heltec-t096"}), "")


class RxgainGuardTests(unittest.TestCase):
    def test_rxgain_ignored_without_t096_board(self) -> None:
        node = {**_STRONG, "rxgain": False}
        self.assertIsNone(rxgain_apply_enabled(node))
        self.assertNotIn("rxgain", applicable_field_desireds(node, None))

    def test_rxgain_off_needs_board(self) -> None:
        node = {**_STRONG, "board": "heltec-t096", "rxgain": False}
        self.assertFalse(rxgain_apply_enabled(node))
        self.assertEqual(applicable_field_desireds(node, None)["rxgain"], "False")


class DueTests(unittest.TestCase):
    def test_first_run_private_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            self.assertTrue(apply_is_due(conn, "me0001", _STRONG, None))

    def test_weak_guest_is_due_after_ok_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            node = {**_STRONG, "guest_password": "m35h3nvy"}
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                if field == "guest":
                    continue
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            self.assertTrue(apply_is_due(conn, "me0001", node, None))

    def test_after_ok_private_not_due_after_leak_heard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
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
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            edited = {**_STRONG, "guest_password": "GuestTwoStrong2"}
            self.assertTrue(apply_is_due(conn, "me0001", edited, None))

    def test_onboard_stamp_clears_private_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            doc = {"nodes": {"me0001": _STRONG}, "trust": {"admin": ["ben"]}}
            keys = {"ben": ["dd" * 32]}
            self.assertTrue(apply_is_due(conn, "me0001", _STRONG, None, doc=doc, keys=keys))
            pid = stamp_profile_after_onboard(
                conn, "me0001", _STRONG, None, doc=doc, keys=keys
            )
            self.assertIsNotNone(pid)
            self.assertTrue(pid.startswith("v1:"))
            self.assertFalse(apply_is_due(conn, "me0001", _STRONG, None, doc=doc, keys=keys))
            self.assertEqual(
                apply_due_fields(conn, "me0001", _STRONG, None, doc=doc, keys=keys),
                [],
            )

    def test_onboard_stamp_includes_ota_autofetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            doc = {"nodes": {"me0001": _STRONG}, "trust": {"admin": ["ben"]}}
            keys = {"ben": ["dd" * 32]}
            stamp_profile_after_onboard(
                conn, "me0001", _STRONG, None, doc=doc, keys=keys
            )
            applicable = applicable_field_desireds(_STRONG, None, doc=doc, keys=keys, key="me0001")
            self.assertEqual(
                last_ok_apply(conn, "me0001", "ota_autofetch"),
                applicable["ota_autofetch"],
            )
            self.assertEqual(applicable["ota_autofetch"], "off")

    def test_ota_autofetch_due_when_other_fields_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                if field == "ota_autofetch":
                    continue
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            due = apply_due_fields(conn, "me0001", _STRONG, None)
            self.assertEqual(due, ["ota_autofetch"])

    def test_power_prefs_due_when_other_fields_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            node = {**_STRONG, "powersaving": True, "fem_rxgain": False}
            applicable = applicable_field_desireds(node, None, key="me0001")
            for field, des in applicable.items():
                if field in ("powersaving", "fem_rxgain"):
                    continue
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            due = apply_due_fields(conn, "me0001", node, None)
            self.assertEqual(due, ["fem_rxgain", "powersaving"])
            self.assertNotIn("powersaving", applicable_field_desireds(_STRONG, None))

    def test_onboard_stamp_after_site_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            node = {**_STRONG, "unit_id": "ME0001"}
            sites = {"ophir": {"node": "me0001", "loc": [39.5, -119.8], "advert_name": "Ophir"}}
            pid = stamp_profile_after_onboard(conn, "me0001", node, sites)
            self.assertTrue(pid and pid.startswith("v1:"))
            self.assertFalse(apply_is_due(conn, "me0001", node, sites))

    def test_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            self.assertTrue(apply_is_due(conn, "me0001", _STRONG, None, force=True))

    def test_partial_field_sync_only_retries_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
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
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
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
            need, skip = format_apply_plan(conn, "me0001", _STRONG, None)
            self.assertTrue(
                need.startswith("fem_rxgain,")
                or need.startswith("fem_rxgain (")
                or need.startswith("agc_reset_interval,")
            )
            self.assertTrue(need.startswith("v1:") or "(v1:" in need)
            self.assertEqual(skip, "none")

    def test_format_apply_plan_splits_synced_from_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(_STRONG, None, key="me0001")
            for field, des in applicable.items():
                if field in {"name", "lat", "lon"}:
                    insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            need, skip = format_apply_plan(conn, "me0001", _STRONG, None)
            self.assertIn("advert", need)
            self.assertNotIn("name,", need)
            self.assertTrue(need.startswith("advert") or ", advert" in need)
            self.assertEqual(skip, "name, lat, lon (synced)")


class ReconcileTests(unittest.TestCase):
    def test_mismatch_clears_stamp(self) -> None:
        from envybot.apply import reconcile_heard

        node = {**_STRONG, "name": "Patrick"}
        sites = {"ophir": {"node": "me0001", "loc": [39.5, -119.8], "advert_name": "Ophir"}}
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(node, sites, key="me0001")
            insert_apply(conn, unit="me0001", field="name", desired=applicable["name"], ok=True)
            changed = reconcile_heard(
                conn,
                "me0001",
                node,
                sites,
                doc={},
                keys={},
                field="name",
                heard="Wrong Name",
            )
            self.assertTrue(changed)
            self.assertIsNone(last_ok_apply(conn, "me0001", "name"))

    def test_sync_book_site_loc_marks_lat_lon_due(self) -> None:
        from envybot.nodes_doc import sync_book, write_nodes_doc

        node = {
            **_STRONG,
            "unit_id": "ME0001",
            "name": "Ophir",
        }
        sites = {"ophir": {"node": "me0001", "loc": [39.5, -119.8], "advert_name": "Ophir"}}
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            (book / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
            write_nodes_doc(nodes_path, {"next_unit": 2, "nodes": {"me0001": node}})
            from ruamel.yaml import YAML

            yaml = YAML()
            yaml.dump({"sites": sites}, sites_path.open("w", encoding="utf-8"))
            conn = open_history(book)
            applicable = applicable_field_desireds(node, sites, key="me0001")
            for field, des in applicable.items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            self.assertEqual(apply_due_fields(conn, "me0001", node, sites), [])
            yaml.dump(
                {"sites": {"ophir": {**sites["ophir"], "loc": [39.6, -119.9]}}},
                sites_path.open("w", encoding="utf-8"),
            )
            doc = {"next_unit": 2}
            mem_nodes = {"me0001": dict(node)}
            mem_sites: dict = dict(sites)
            keys: dict = {}
            sync_book(nodes_path, doc, mem_nodes, mem_sites, keys)
            due = apply_due_fields(conn, "me0001", mem_nodes["me0001"], mem_sites)
            self.assertIn("lat", due)
            self.assertIn("lon", due)


class FormatTests(unittest.TestCase):
    def test_six_decimals(self) -> None:
        self.assertEqual(format_book_coord(39.909448), "39.909448")
        self.assertEqual(format_book_coord(-119.328847), "-119.328847")
        self.assertEqual(format_book_coord(0.0), "0.000000")


if __name__ == "__main__":
    unittest.main()
