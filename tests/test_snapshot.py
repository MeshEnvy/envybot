"""Tests for sanitized fleet snapshots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.position import is_placeholder_gps
from envybot.web.snapshot import (
    assert_no_secrets,
    build_fleet_snapshot,
    is_secret_key,
    lookup_site_name,
    resolve_position,
    strip_secrets,
    unit_label,
)


class SecretKeyTests(unittest.TestCase):
    def test_secret_patterns(self) -> None:
        self.assertTrue(is_secret_key("admin_password"))
        self.assertTrue(is_secret_key("identity_secret"))
        self.assertTrue(is_secret_key("guest_password"))
        self.assertFalse(is_secret_key("identity_pubkey"))

    def test_strip_secrets(self) -> None:
        node = {
            "unit_id": "ME0001",
            "admin_password": "hunter2",
            "identity_secret": "deadbeef",
            "identity_pubkey": "a" * 64,
        }
        clean = strip_secrets(node)
        self.assertNotIn("admin_password", clean)
        self.assertNotIn("identity_secret", clean)
        self.assertIn("identity_pubkey", clean)


class GpsTests(unittest.TestCase):
    def test_placeholder_detection(self) -> None:
        self.assertTrue(is_placeholder_gps(0, 0))
        self.assertTrue(is_placeholder_gps(0, -119.03))
        self.assertTrue(is_placeholder_gps(14.009295, 120.996018))
        self.assertFalse(is_placeholder_gps(39.5, -119.8))

    def test_partial_zero_falls_back_to_site(self) -> None:
        node = {"lat": 0.0, "lon": -119.02876, "site": "slpt-north"}
        sites = {"slpt-north": {"name": "SLPT North", "loc": [41.56303, -119.04481]}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 41.56303)

    def test_node_coords_preferred(self) -> None:
        node = {"lat": 39.5, "lon": -119.8, "site": "foo"}
        sites = {"foo": {"loc": [40.0, -117.0]}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "node")
        self.assertAlmostEqual(pos["lat"], 39.5)

    def test_site_fallback(self) -> None:
        node = {"lat": 0, "lon": 0, "site": "foo"}
        sites = {"foo": {"name": "Foo Peak", "loc": [40.0, -117.0]}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)

    def test_zero_lat_falls_back_to_site(self) -> None:
        node = {"lat": 0.0, "lon": -119.0, "site": "foo"}
        sites = {"foo": {"name": "Foo Peak", "loc": [40.0, -117.0]}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)


class SnapshotTests(unittest.TestCase):
    def test_no_secrets_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump(
                {
                    "sites": {
                        "test-site": {"name": "Test", "loc": [39.0, -119.0]},
                    }
                },
                sites_path.open("w", encoding="utf-8"),
            )
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Alpha",
                            "site": "test-site",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                            "guest_password": "secret-guest",
                            "identity_secret": "deadbeef",
                            "firmware_version": "0.1.0",
                            "status_pulled_at": 1_700_000_000,
                            "status": {"battery_mv": 4200, "uptime_secs": 3600},
                            "telemetry": [
                                {"channel": 1, "type": "voltage", "value": 4.2},
                                {"channel": 1, "type": "temperature", "value": 22.5},
                            ],
                            "neighbors": [
                                {"pubkey": ("b" * 8) + "00", "secs_ago": 10, "snr": 5.0},
                            ],
                            "acl": [{"key": "abcd1234", "perm": 1}],
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "name": "Beta",
                            "site": None,
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "lat": 39.5,
                            "lon": -119.5,
                            "lat_pulled_at": 1_700_000_000,
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            assert_no_secrets(snap)
            u1 = snap["units"]["me0001"]
            self.assertEqual(u1["acl_count"], 1)
            self.assertNotIn("acl", u1)
            self.assertEqual(u1["telemetry"]["voltage"], 4.2)
            self.assertTrue(u1["mapped"])
            self.assertEqual(u1["position"]["source"], "site")
            self.assertEqual(u1["site_name"], "Test")
            self.assertEqual(u1["label"], "Test")
            u2 = snap["units"]["me0002"]
            self.assertEqual(u2["position"]["source"], "node")
            self.assertIsNone(u2["site_name"])
            self.assertEqual(u2["label"], "ME0002")
            nbs = [n for n in u1["neighbors"] if n.get("unit_key") == "me0002"]
            self.assertEqual(len(nbs), 1)
            self.assertEqual(nbs[0]["label"], "ME0002")


class LabelTests(unittest.TestCase):
    def test_bound_uses_site_name(self) -> None:
        sites = {"ophir-hill": {"name": "Ophir"}}
        node = {"unit_id": "ME0003", "name": "RAK4631 Repeater", "site": "ophir-hill"}
        self.assertEqual(lookup_site_name("ophir-hill", sites), "Ophir")
        self.assertEqual(unit_label(key="me0003", node=node, sites=sites), "Ophir")

    def test_bound_missing_site_falls_back_to_slug(self) -> None:
        node = {"unit_id": "ME0003", "name": "RAK4631 Repeater", "site": "ophir-hill"}
        self.assertEqual(lookup_site_name("ophir-hill", {}), "ophir-hill")
        self.assertEqual(unit_label(key="me0003", node=node, sites={}), "ophir-hill")

    def test_unbound_uses_unit_id(self) -> None:
        node = {"unit_id": "ME0041", "name": "Bag radio", "site": None}
        self.assertIsNone(lookup_site_name(None, {}))
        self.assertEqual(unit_label(key="me0041", node=node, sites={}), "ME0041")


if __name__ == "__main__":
    unittest.main()
