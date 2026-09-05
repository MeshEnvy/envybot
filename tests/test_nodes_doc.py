"""Desired-state nodes.yaml write and book flags."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.nodes_doc import (
    is_decommissioned,
    is_paused,
    sync_paused,
    write_nodes_doc,
)
from envybot.routing import resolve_routing, routing_explicit
from ruamel.yaml import YAML


class WriteTests(unittest.TestCase):
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

    def test_sync_power_prefs_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            disk = {
                "next_unit": 2,
                "nodes": {
                    "me0001": {
                        "unit_id": "ME0001",
                        "powersaving": True,
                        "fem_rxgain": False,
                    },
                },
            }
            write_nodes_doc(path, disk)
            mem = {"me0001": {"unit_id": "ME0001", "guest_password": "new"}}
            sync_paused(path, mem)
            self.assertTrue(mem["me0001"]["powersaving"])
            self.assertFalse(mem["me0001"]["fem_rxgain"])
            self.assertEqual(mem["me0001"]["guest_password"], "new")
            disk["nodes"]["me0001"].pop("powersaving")
            disk["nodes"]["me0001"].pop("fem_rxgain")
            write_nodes_doc(path, disk)
            sync_paused(path, mem)
            self.assertNotIn("powersaving", mem["me0001"])
            self.assertNotIn("fem_rxgain", mem["me0001"])

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


class RoutingBookTests(unittest.TestCase):
    def test_resolve_ignores_flood_key(self) -> None:
        self.assertEqual(resolve_routing({"flood": True}), resolve_routing({}))
        self.assertIsNone(routing_explicit({"flood": True}))

    def test_sync_routing_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            disk = {
                "next_unit": 3,
                "nodes": {
                    "me0001": {"unit_id": "ME0001", "routing": "direct"},
                    "me0002": {"unit_id": "ME0002", "routing": "flood"},
                },
            }
            write_nodes_doc(path, disk)
            mem = {
                "me0001": {"unit_id": "ME0001", "guest_password": "new"},
                "me0002": {"unit_id": "ME0002", "routing": "direct"},
            }
            sync_paused(path, mem)
            self.assertEqual(routing_explicit(mem["me0001"]), "direct")
            self.assertEqual(routing_explicit(mem["me0002"]), "flood")
            self.assertNotIn("flood", mem["me0002"])


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
