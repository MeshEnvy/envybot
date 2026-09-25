"""Tests for sanitized fleet snapshots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.apply import applicable_field_desireds
from envybot.history import import_yaml_last_seen, insert_apply, open_history, record_poll
from envybot.position import approx_miles, haversine_miles, is_placeholder_gps, lookup_site_name
from envybot.web.snapshot import (
    assert_no_secrets,
    attach_neighbor_miles,
    build_fleet_snapshot,
    drift_state,
    is_secret_key,
    neighbor_is_fresh,
    reported_locs_from_contacts,
    resolve_position,
    sanitize_neighbors,
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

    def test_site_bind_via_unit_id(self) -> None:
        node = {"unit_id": "ME0011"}
        sites = {"slpt-north": {"name": "SLPT North", "loc": [41.56303, -119.04481], "node": "me0011"}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 41.56303)

    def test_node_coords_ignored(self) -> None:
        node = {"unit_id": "ME0001", "lat": 39.5, "lon": -119.8}
        sites = {"foo": {"loc": [40.0, -117.0], "node": "me0001"}}
        pos = resolve_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)

    def test_unbound_bench_loc_is_not_a_map_pin(self) -> None:
        node = {"unit_id": "ME0041"}
        sites = {"foo": {"name": "Foo Peak", "loc": [40.0, -117.0], "node": "me0001"}}
        doc = {"bench_loc": [39.5296, -119.8138]}
        pos = resolve_position(node, sites, key="me0041", doc=doc)
        self.assertIsNone(pos)

    def test_unbound_node_loc_is_a_map_pin(self) -> None:
        node = {"unit_id": "ME0006", "loc": [39.3094, -119.6516]}
        pos = resolve_position(node, {}, key="me0006")
        assert pos is not None
        self.assertEqual(pos["source"], "node")
        self.assertAlmostEqual(pos["lat"], 39.3094)

    def test_site_wins_over_bench(self) -> None:
        node = {"unit_id": "ME0011"}
        sites = {"slpt-north": {"name": "SLPT North", "loc": [41.56303, -119.04481], "node": "me0011"}}
        doc = {"bench_loc": [39.5296, -119.8138]}
        pos = resolve_position(node, sites, key="me0011", doc=doc)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 41.56303)


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
                        "test-site": {"name": "Test", "loc": [39.0, -119.0], "node": "me0001"},
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
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "lat_pulled_at": 1_700_000_000,
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)
            import_yaml_last_seen(conn, yaml.load(nodes_path.read_text(encoding="utf-8"))["nodes"])
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            assert_no_secrets(snap)
            u1 = snap["units"]["me0001"]
            self.assertNotIn("acl", u1)
            self.assertEqual(u1["status"]["battery_mv"], 4200)
            self.assertEqual(u1["status"]["uptime_secs"], 3600)
            self.assertEqual(u1["telemetry"]["voltage"], 4.2)
            self.assertEqual(u1["telemetry"]["temperature"], 22.5)
            self.assertTrue(u1["mapped"])
            self.assertEqual(u1["position"]["source"], "site")
            self.assertEqual(u1["site_name"], "Test")
            self.assertEqual(u1["label"], "Test")
            self.assertNotIn("sun", u1)
            u2 = snap["units"]["me0002"]
            self.assertIsNone(u2["position"])
            self.assertIsNone(u2["site_name"])
            self.assertEqual(u2["label"], "ME0002")
            self.assertNotIn("sun", u2)
            nbs = [n for n in u1["neighbors"] if n.get("unit_key") == "me0002"]
            self.assertEqual(len(nbs), 1)
            self.assertEqual(nbs[0]["label"], "ME0002")

    def test_unbound_bench_loc_is_not_a_unit_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "bench_loc": [39.5296, -119.8138],
                    "next_unit": 42,
                    "nodes": {
                        "me0041": {"unit_id": "ME0041", "identity_pubkey": "c" * 64},
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            u = snap["units"]["me0041"]
            self.assertFalse(u["mapped"])
            self.assertIsNone(u["position"])

    def test_exposes_base_hash_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)

            class _Res:
                base_hash = "AABBCCDDEEFF0011"
                polled_groups = frozenset({"ota"})

            record_poll(conn, unit="me0001", res=_Res())
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertEqual(snap["units"]["me0001"]["base_hash"], "AABBCCDDEEFF0011")

    def test_omits_decommissioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Live",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "name": "Dead",
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "decommissioned": 1787943600,
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertIn("me0001", snap["units"])
            self.assertNotIn("me0002", snap["units"])
            self.assertEqual(snap["counts"]["total"], 1)

    def test_includes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Live",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "name": "Quiet",
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "paused": True,
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertFalse(snap["units"]["me0001"]["paused"])
            self.assertTrue(snap["units"]["me0002"]["paused"])
            self.assertEqual(snap["units"]["me0001"]["routing"], "path")
            self.assertIsNone(snap["units"]["me0001"]["routing_explicit"])
            self.assertEqual(snap["units"]["me0002"]["routing"], "path")
            self.assertIsNone(snap["units"]["me0002"]["routing_explicit"])
            self.assertEqual(snap["counts"]["paused"], 1)
            self.assertEqual(snap["counts"]["total"], 2)

    def test_includes_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Live",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "name": "Far bench",
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "routing": "flood",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertEqual(snap["units"]["me0001"]["routing"], "path")
            self.assertEqual(snap["units"]["me0002"]["routing"], "flood")
            self.assertEqual(snap["units"]["me0002"]["routing_explicit"], "flood")

    def test_omits_meshtastic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Live",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "name": "MT leftover",
                            "identity_pubkey": "b" * 64,
                            "admin_password": "other",
                            "firmware_platform": "meshtastic",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertIn("me0001", snap["units"])
            self.assertNotIn("me0002", snap["units"])
            self.assertEqual(snap["counts"]["total"], 1)

    def test_drift_follows_profile_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            private = {
                "unit_id": "ME0001",
                "name": "Patrick",
                "guest_password": "GuestOneStrong1",
                "admin_password": "AdminOneStrong1",
                "identity_pubkey": "aa" * 32,
            }
            site_bound = {
                "unit_id": "ME0002",
                "name": "Ophir",
                "guest_password": "GuestTwoStrong2",
                "admin_password": "AdminTwoStrong2",
                "identity_pubkey": "bb" * 32,
            }
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {"me0001": private, "me0002": site_bound},
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)
            applicable = applicable_field_desireds(
                private, {}, doc={"nodes": {"me0001": private}}, keys={}, key="me0001"
            )
            for field, des in applicable.items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            self.assertIsNone(snap["units"]["me0001"]["drift"])
            self.assertEqual(snap["units"]["me0002"]["drift"], "due")

    def test_status_traffic_from_latest_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Traffic",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                            "guest_password": "secret-guest",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)

            class _Res:
                polled_groups = frozenset({"status"})
                status = {
                    "packets_recv": 500,
                    "packets_sent": 120,
                    "recv_errors": 3,
                    "err_events": 0,
                    "uptime_secs": 3600,
                    "recv_flood": 400,
                    "recv_direct": 100,
                    "sent_flood": 90,
                    "sent_direct": 30,
                    "last_snr": 4.25,
                    "last_rssi": -95,
                    "noise_floor": -110,
                }

            record_poll(conn, unit="me0001", res=_Res(), ts=100)
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            status = snap["units"]["me0001"]["status"]
            assert status is not None
            self.assertEqual(status["packets_recv"], 500)
            self.assertEqual(status["recv_errors"], 3)
            self.assertEqual(status["uptime_secs"], 3600)
            self.assertEqual(status["recv_flood"], 400)
            self.assertAlmostEqual(status["last_snr"], 4.25)
            self.assertEqual(status["last_rssi"], -95)
            self.assertEqual(status["noise_floor"], -110)

    def test_traffic_interval_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Interval",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                            "guest_password": "secret-guest",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)

            class _Res:
                polled_groups = frozenset({"status"})

            _Res.status = {"packets_recv": 1000, "packets_sent": 100, "recv_errors": 50, "uptime_secs": 1000}
            record_poll(conn, unit="me0001", res=_Res(), ts=1000)
            _Res.status = {"packets_recv": 1300, "packets_sent": 150, "recv_errors": 110, "uptime_secs": 2000}
            record_poll(conn, unit="me0001", res=_Res(), ts=3000)
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            interval = snap["units"]["me0001"]["traffic_interval"]
            assert interval is not None
            self.assertEqual(interval["duration_secs"], 2000)
            self.assertEqual(interval["packets_recv"], 300)
            self.assertEqual(interval["recv_errors"], 60)

    def test_sparks_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Spark",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                            "guest_password": "secret-guest",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)
            now = int(__import__("time").time())

            class _Res:
                polled_groups = frozenset({"status"})

            _Res.status = {
                "battery_mv": 4100,
                "packets_recv": 100,
                "packets_sent": 10,
                "recv_errors": 5,
                "uptime_secs": 100,
            }
            record_poll(conn, unit="me0001", res=_Res(), ts=now - 7200)
            _Res.status = {
                "battery_mv": 4050,
                "packets_recv": 400,
                "packets_sent": 20,
                "recv_errors": 15,
                "uptime_secs": 200,
            }
            record_poll(conn, unit="me0001", res=_Res(), ts=now - 3600)
            conn.execute(
                "INSERT INTO telemetry (unit, ts, type, value) VALUES (?, ?, ?, ?)",
                ("me0001", now - 5400, "temperature", 22.5),
            )
            conn.execute(
                "INSERT INTO telemetry (unit, ts, type, value) VALUES (?, ?, ?, ?)",
                ("me0001", now - 1800, "temperature", 24.0),
            )
            conn.commit()
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            sparks = snap["units"]["me0001"]["sparks"]
            self.assertIn("battery_mv", sparks)
            self.assertIn("temperature", sparks)
            self.assertIn("recv_rate", sparks)
            self.assertIn("unreadable_pct", sparks)
            self.assertEqual(len(sparks["battery_mv"]), 2)
            self.assertAlmostEqual(sparks["battery_mv"][0]["value"], 4.1)
            self.assertAlmostEqual(sparks["battery_mv"][1]["value"], 4.05)
            self.assertEqual(len(sparks["temperature"]), 2)
            self.assertEqual(len(sparks["recv_rate"]), 1)
            self.assertAlmostEqual(sparks["recv_rate"][0]["value"], 300.0)
            self.assertEqual(len(sparks["unreadable_pct"]), 1)
            self.assertAlmostEqual(sparks["unreadable_pct"][0]["value"], 3.23, places=1)

    def test_health_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "name": "Healthy",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "secret-admin",
                            "guest_password": "secret-guest",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)

            class _Res:
                polled_groups = frozenset({"status", "telemetry"})

            _Res.status = {
                "packets_recv": 100,
                "packets_sent": 10,
                "recv_errors": 5,
                "battery_mv": 4200,
                "uptime_secs": 1000,
            }
            _Res.telemetry = [
                {"type": "voltage", "value": 4.2},
                {"type": "temperature", "value": 22.0},
            ]
            record_poll(conn, unit="me0001", res=_Res(), ts=1000)
            _Res.status = {
                "packets_recv": 150,
                "packets_sent": 20,
                "recv_errors": 8,
                "battery_mv": 4190,
                "uptime_secs": 2000,
            }
            record_poll(conn, unit="me0001", res=_Res(), ts=2000)
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            health = snap["units"]["me0001"]["health"]
            self.assertIn("grade", health)
            self.assertIn("headline", health)
            self.assertIn("checks", health)
            self.assertIn("summary", health)


class NeighborFreshTests(unittest.TestCase):
    def test_age_window(self) -> None:
        self.assertTrue(neighbor_is_fresh(10))
        self.assertTrue(neighbor_is_fresh(2 * 24 * 3600))
        self.assertFalse(neighbor_is_fresh(8 * 24 * 3600))
        self.assertFalse(neighbor_is_fresh(None))
        self.assertTrue(neighbor_is_fresh(-5))

    def test_sanitize_drops_stale(self) -> None:
        nodes = {
            "me0002": {"unit_id": "ME0002", "name": "Live"},
            "me0003": {"unit_id": "ME0003", "name": "Ghost"},
        }
        index = {"b" * 8: "me0002", "c" * 8: "me0003"}
        raw = [
            {"pubkey": "b" * 16, "secs_ago": 30, "snr": 11.0},
            {"pubkey": "c" * 16, "secs_ago": 116 * 24 * 3600, "snr": 12.0},
        ]
        out = sanitize_neighbors(raw, pubkey_index=index, nodes=nodes, sites={})
        self.assertEqual([n["unit_key"] for n in out], ["me0002"])


class NeighborMilesTests(unittest.TestCase):
    def test_approx_rounding(self) -> None:
        self.assertEqual(approx_miles(3.24), 3.2)
        self.assertEqual(approx_miles(18.4), 18.0)
        self.assertAlmostEqual(haversine_miles(39.5, -119.8, 39.6, -119.8), 6.9, places=1)

    def test_book_peer_uses_site_loc(self) -> None:
        units = {
            "me0001": {
                "position": {"lat": 39.5, "lon": -119.8},
                "neighbors": [{"unit_key": "me0002", "pubkey_prefix": "bbbbbbbb"}],
            },
            "me0002": {"position": {"lat": 39.6, "lon": -119.8}},
        }
        attach_neighbor_miles(units)
        self.assertAlmostEqual(units["me0001"]["neighbors"][0]["miles"], 6.9, places=1)

    def test_community_uses_reported_loc(self) -> None:
        units = {
            "me0001": {
                "position": {"lat": 39.5, "lon": -119.8},
                "neighbors": [{"pubkey_prefix": "abcd1234", "snr": 7}],
            }
        }
        attach_neighbor_miles(units, {"abcd1234": (39.6, -119.8)})
        self.assertAlmostEqual(units["me0001"]["neighbors"][0]["miles"], 6.9, places=1)

    def test_contacts_skip_placeholder(self) -> None:
        locs = reported_locs_from_contacts(
            {
                "aa" * 32: {"public_key": "aa" * 32, "adv_lat": 0.0, "adv_lon": 0.0},
                "bb" * 32: {"public_key": "bb" * 32, "adv_lat": 39.6, "adv_lon": -119.8},
            }
        )
        self.assertNotIn("aa" * 32, locs)
        self.assertEqual(locs["bb" * 8], (39.6, -119.8))

    def test_unmapped_origin_omits_miles(self) -> None:
        units = {
            "me0001": {
                "position": None,
                "neighbors": [{"unit_key": "me0002"}],
            },
            "me0002": {"position": {"lat": 39.6, "lon": -119.8}},
        }
        attach_neighbor_miles(units)
        self.assertNotIn("miles", units["me0001"]["neighbors"][0])

    def test_snapshot_stamps_book_miles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump(
                {
                    "sites": {
                        "alpha": {"name": "Alpha", "loc": [39.5, -119.8], "node": "me0001"},
                        "beta": {"name": "Beta", "loc": [39.6, -119.8], "node": "me0002"},
                    }
                },
                sites_path.open("w", encoding="utf-8"),
            )
            yaml.dump(
                {
                    "next_unit": 3,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "identity_pubkey": "a" * 64,
                            "neighbors": [
                                {"pubkey": ("b" * 8) + "00", "secs_ago": 10, "snr": 5.0},
                            ],
                        },
                        "me0002": {
                            "unit_id": "ME0002",
                            "identity_pubkey": "b" * 64,
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)
            import_yaml_last_seen(conn, yaml.load(nodes_path.read_text(encoding="utf-8"))["nodes"])
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            nbs = [n for n in snap["units"]["me0001"]["neighbors"] if n.get("unit_key") == "me0002"]
            self.assertEqual(len(nbs), 1)
            self.assertAlmostEqual(nbs[0]["miles"], 6.9, places=1)


class DriftStateTests(unittest.TestCase):
    def test_profile_ok_clears_drift(self) -> None:
        self.assertIsNone(drift_state({"name": "Patrick"}, profile_ok=True))
        self.assertIsNone(drift_state({"name": "Ophir"}, profile_ok=True))

    def test_due_site_bound_is_due(self) -> None:
        self.assertEqual(
            drift_state({"name": "Ophir"}, profile_ok=False),
            "due",
        )

    def test_due_unbound_is_due(self) -> None:
        self.assertEqual(drift_state({"name": "Patrick"}, profile_ok=False), "due")

    def test_heard_name_on_private_is_not_leak(self) -> None:
        self.assertIsNone(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={"name_heard": "Ophir Hill"},
            )
        )

    def test_heard_gps_on_private_is_not_leak(self) -> None:
        self.assertIsNone(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={"lat_heard": 39.5, "lon_heard": -119.8, "gps_at": 10},
            )
        )

    def test_heard_advert_on_private_is_leak(self) -> None:
        self.assertEqual(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={"advert_interval_min": 120, "advert_at": 10},
            ),
            "leak",
        )

    def test_heard_name_drift_is_not_advert_leak(self) -> None:
        self.assertIsNone(
            drift_state(
                {"name": "Ophir"},
                profile_ok=True,
                seen={"name_heard": "Ophir"},
            )
        )

    def test_masked_heard_is_not_leak(self) -> None:
        self.assertIsNone(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={
                    "name_heard": "Repeater",
                    "lat_heard": 0.0,
                    "lon_heard": 0.0,
                    "advert_interval_min": 0,
                },
            )
        )

    def test_leak_wins_over_due(self) -> None:
        self.assertEqual(
            drift_state(
                {"name": "Patrick"},
                profile_ok=False,
                seen={"flood_advert_interval_h": 47, "flood_advert_at": 10},
            ),
            "leak",
        )

    def test_stale_flood_before_apply_is_not_leak(self) -> None:
        self.assertIsNone(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={"flood_advert_interval_h": 47, "flood_advert_at": 10},
                apply_at={"flood": 20},
            )
        )

    def test_flood_pull_after_apply_is_leak(self) -> None:
        self.assertEqual(
            drift_state(
                {"name": "Patrick"},
                profile_ok=True,
                seen={"flood_advert_interval_h": 47, "flood_advert_at": 30},
                apply_at={"flood": 20},
            ),
            "leak",
        )


class LabelTests(unittest.TestCase):
    def test_bound_uses_site_name(self) -> None:
        sites = {"ophir-hill": {"name": "Ophir", "node": "me0003"}}
        node = {"unit_id": "ME0003"}
        self.assertEqual(lookup_site_name("ophir-hill", sites), "Ophir")
        self.assertEqual(unit_label(key="me0003", node=node, sites=sites), "Ophir")

    def test_bound_missing_site_uses_slug(self) -> None:
        node = {"unit_id": "ME0003"}
        self.assertEqual(lookup_site_name("ophir-hill", {}), "ophir-hill")
        sites = {"ophir-hill": {"node": "me0003"}}
        self.assertEqual(unit_label(key="me0003", node=node, sites=sites), "ophir-hill")

    def test_unbound_uses_unit_id(self) -> None:
        node = {"unit_id": "ME0041"}
        self.assertIsNone(lookup_site_name(None, {}))
        self.assertEqual(unit_label(key="me0041", node=node, sites={}), "ME0041")

    def test_unbound_uses_alias(self) -> None:
        node = {"unit_id": "ME0041", "alias": "Bench A"}
        self.assertEqual(unit_label(key="me0041", node=node, sites={}), "Bench A")


class RadioPrefTests(unittest.TestCase):
    def test_pref_display(self) -> None:
        from envybot.web.snapshot import pref_display

        self.assertEqual(pref_display("powersaving", True), "on")
        self.assertEqual(pref_display("fem_rxgain", False), "off")
        self.assertEqual(pref_display("dutycycle", 50), "50%")
        self.assertEqual(pref_display("path_hash", 1), "2-byte")

    def test_snapshot_prefs_use_apply_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 2,
                    "nodes": {
                        "me0001": {
                            "unit_id": "ME0001",
                            "firmware_platform": "meshcore",
                            "guest_password": "GuestOneStrong1",
                            "admin_password": "AdminOneStrong1",
                            "identity_pubkey": "aa" * 32,
                            "powersaving": True,
                            "fem_rxgain": False,
                        }
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            conn = open_history(book)
            insert_apply(conn, unit="me0001", field="powersaving", desired="True", ok=True)
            conn.close()
            snap = build_fleet_snapshot(nodes_path=nodes_path, sites_path=sites_path)
            prefs = {p["id"]: p for p in snap["units"]["me0001"]["prefs"]}
            self.assertEqual(prefs["powersaving"]["value"], "on")
            self.assertEqual(prefs["powersaving"]["state"], "synced")
            self.assertEqual(prefs["fem_rxgain"]["value"], "off")
            self.assertEqual(prefs["fem_rxgain"]["state"], "due")
            self.assertEqual(prefs["dutycycle"]["value"], "50%")
            self.assertEqual(prefs["path_hash"]["value"], "2-byte")
            self.assertEqual(prefs["ota_autofetch"]["value"], "off")

    def test_only_skip_filter_dashboard_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            yaml = YAML()
            yaml.dump({"sites": {}}, sites_path.open("w", encoding="utf-8"))
            yaml.dump(
                {
                    "next_unit": 50,
                    "nodes": {
                        "me0048": {
                            "unit_id": "ME0048",
                            "identity_pubkey": "a" * 64,
                            "admin_password": "AdminOneStrong1",
                        },
                        "me0049": {
                            "unit_id": "ME0049",
                            "identity_pubkey": "b" * 64,
                            "admin_password": "AdminTwoStrong2",
                        },
                    },
                },
                nodes_path.open("w", encoding="utf-8"),
            )
            only_snap = build_fleet_snapshot(
                nodes_path=nodes_path,
                sites_path=sites_path,
                include={"me0048"},
            )
            self.assertEqual(set(only_snap["units"]), {"me0048"})
            skip_snap = build_fleet_snapshot(
                nodes_path=nodes_path,
                sites_path=sites_path,
                skip={"me0048"},
            )
            self.assertEqual(set(skip_snap["units"]), {"me0049"})


if __name__ == "__main__":
    unittest.main()
