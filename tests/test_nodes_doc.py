"""Desired YAML migrate: strip observed, drop leftover node GPS/site."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.nodes_doc import (
    MASK_NAME,
    is_public,
    migrate_desired,
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
        self.assertEqual(node["name"], "Spanish Benchmark East")

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


if __name__ == "__main__":
    unittest.main()
