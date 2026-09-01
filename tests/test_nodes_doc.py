"""Desired YAML migrate: strip observed, drop leftover node GPS/site."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.nodes_doc import (
    MASK_NAME,
    is_decommissioned,
    is_paused,
    is_public,
    migrate_desired,
    sync_paused,
    write_nodes_doc,
)
from ruamel.yaml import YAML


class MigrateTests(unittest.TestCase):
    def test_strip_observed_and_node_location(self) -> None:
        doc = {
            "next_unit": 2,
            "nodes": {
                "me0001": {
                    "unit_id": "ME0001",
                    "name": MASK_NAME,
                    "site": "ophir",
                    "lat": 39.5,
                    "lon": -119.8,
                    "status": {"battery_mv": 1},
                    "firmware_version": "v0.1",
                    "firmware_pulled_at": 9,
                    "name_pulled_at": 9,
                }
            },
        }
        sites = {"ophir": {"name": "Ophir", "loc": [39.5, -119.8], "node": "me0001"}}
        self.assertTrue(migrate_desired(doc, sites))
        node = doc["nodes"]["me0001"]
        self.assertNotIn("status", node)
        self.assertNotIn("firmware_version", node)
        self.assertNotIn("firmware_pulled_at", node)
        self.assertNotIn("lat", node)
        self.assertNotIn("lon", node)
        self.assertNotIn("site", node)
        self.assertNotIn("name", node)
        self.assertNotIn("public", node)
        self.assertFalse(is_public(node))

    def test_strips_offset_gps(self) -> None:
        doc = {
            "next_unit": 2,
            "nodes": {
                "me0001": {
                    "unit_id": "ME0001",
                    "site": "ophir",
                    "lat": 39.51,
                    "lon": -119.81,
                    "name": "Spanish Benchmark East",
                }
            },
        }
        sites = {"ophir": {"loc": [39.5, -119.8], "node": "me0001"}}
        migrate_desired(doc, sites)
        node = doc["nodes"]["me0001"]
        self.assertNotIn("lat", node)
        self.assertNotIn("lon", node)
        self.assertNotIn("site", node)
        self.assertNotIn("name", node)

    def test_write_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            doc = {"next_unit": 2, "nodes": {"me0001": {"unit_id": "ME0001"}}}
            write_nodes_doc(path, doc)
            yaml = YAML(typ="safe")
            loaded = yaml.load(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["next_unit"], 2)
            self.assertIn("desired identity", path.read_text(encoding="utf-8"))
            self.assertIn("sites.yaml", path.read_text(encoding="utf-8"))


class PausedTests(unittest.TestCase):
    def test_blank_is_live(self) -> None:
        self.assertFalse(is_paused(None))
        self.assertFalse(is_paused({}))
        self.assertFalse(is_paused({"paused": False}))
        self.assertFalse(is_paused({"paused": None}))

    def test_true_is_paused(self) -> None:
        self.assertTrue(is_paused({"paused": True}))

    def test_sync_paused_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            disk = {
                "next_unit": 3,
                "nodes": {
                    "me0001": {"unit_id": "ME0001", "paused": True},
                    "me0002": {"unit_id": "ME0002"},
                },
            }
            write_nodes_doc(path, disk)
            mem = {
                "me0001": {"unit_id": "ME0001", "guest_password": "new"},
                "me0002": {"unit_id": "ME0002", "paused": True},
            }
            sync_paused(path, mem)
            self.assertTrue(is_paused(mem["me0001"]))
            self.assertEqual(mem["me0001"]["guest_password"], "new")
            self.assertFalse(is_paused(mem["me0002"]))

    def test_sync_alias_and_notes_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            disk = {
                "next_unit": 2,
                "nodes": {
                    "me0001": {
                        "unit_id": "ME0001",
                        "alias": "Bag",
                        "notes": "Spare repeater",
                    },
                },
            }
            write_nodes_doc(path, disk)
            mem = {"me0001": {"unit_id": "ME0001", "guest_password": "new"}}
            sync_paused(path, mem)
            self.assertEqual(mem["me0001"]["alias"], "Bag")
            self.assertEqual(mem["me0001"]["notes"], "Spare repeater")
            self.assertEqual(mem["me0001"]["guest_password"], "new")

    def test_persist_guest_keeps_disk_paused(self) -> None:
        from envybot.apply import persist_guest_if_new

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(
                path,
                {
                    "next_unit": 2,
                    "nodes": {"me0001": {"unit_id": "ME0001", "paused": True}},
                },
            )
            mem_doc = {
                "next_unit": 2,
                "nodes": {"me0001": {"unit_id": "ME0001", "guest_password": "rolled"}},
            }
            persist_guest_if_new(path, mem_doc)
            self.assertTrue(is_paused(mem_doc["nodes"]["me0001"]))
            from envybot.nodes_doc import load_nodes_doc

            disk = load_nodes_doc(path)
            self.assertTrue(is_paused(disk["nodes"]["me0001"]))
            self.assertEqual(disk["nodes"]["me0001"]["guest_password"], "rolled")


class DecommissionedTests(unittest.TestCase):
    def test_blank_is_live(self) -> None:
        self.assertFalse(is_decommissioned(None))
        self.assertFalse(is_decommissioned({}))
        self.assertFalse(is_decommissioned({"decommissioned": None}))
        self.assertFalse(is_decommissioned({"decommissioned": ""}))

    def test_epoch_is_decommissioned(self) -> None:
        self.assertTrue(is_decommissioned({"decommissioned": 1787943600}))


if __name__ == "__main__":
    unittest.main()
