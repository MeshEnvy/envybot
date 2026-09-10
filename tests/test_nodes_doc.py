"""Desired-state nodes.yaml write and book flags."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.nodes_doc import (
    is_decommissioned,
    is_paused,
    sync_book,
    write_nodes_doc,
)
from ruamel.yaml import YAML as YamlLoader
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


class SyncBookTests(unittest.TestCase):
    def test_sync_book_disk_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            path = book / "nodes.yaml"
            (book / "sites.yaml").write_text("sites: {}\n", encoding="utf-8")
            (book / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
            disk = {
                "next_unit": 3,
                "nodes": {
                    "me0001": {"unit_id": "ME0001", "paused": True},
                    "me0002": {"unit_id": "ME0002"},
                },
            }
            write_nodes_doc(path, disk)
            doc: dict = {"next_unit": 3}
            nodes = {
                "me0001": {"unit_id": "ME0001", "guest_password": "new"},
                "me0002": {"unit_id": "ME0002", "paused": True},
            }
            sites: dict = {}
            keys: dict = {}
            self.assertTrue(sync_book(path, doc, nodes, sites, keys))
            self.assertTrue(is_paused(nodes["me0001"]))
            self.assertNotIn("guest_password", nodes["me0001"])
            self.assertFalse(is_paused(nodes["me0002"]))

    def test_sync_book_sites_and_public_advert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            path = book / "nodes.yaml"
            sites_path = book / "sites.yaml"
            (book / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
            write_nodes_doc(
                path,
                {
                    "next_unit": 2,
                    "public_advert": {"suffix": "ME"},
                    "nodes": {"me0001": {"unit_id": "ME0001"}},
                },
            )
            yaml = YamlLoader()
            yaml.dump(
                {"sites": {"ophir": {"node": "me0001", "loc": [39.5, -119.8]}}},
                sites_path.open("w", encoding="utf-8"),
            )
            doc = {"next_unit": 2}
            nodes = {"me0001": {"unit_id": "ME0001"}}
            sites: dict = {}
            keys: dict = {}
            self.assertTrue(sync_book(path, doc, nodes, sites, keys))
            self.assertEqual(doc["public_advert"], {"suffix": "ME"})
            self.assertEqual(sites["ophir"]["loc"], [39.5, -119.8])

    def test_persist_guest_writes_disk(self) -> None:
        from envybot.apply import persist_guest_password

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(
                path,
                {
                    "next_unit": 2,
                    "nodes": {"me0001": {"unit_id": "ME0001", "paused": True}},
                },
            )
            node = {"unit_id": "ME0001", "guest_password": "rolled", "last_guest_roll": 123}
            persist_guest_password(path, "me0001", node)
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
            doc = {"next_unit": 3}
            sites: dict = {}
            keys: dict = {}
            (path.parent / "sites.yaml").write_text("sites: {}\n", encoding="utf-8")
            (path.parent / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
            sync_book(path, doc, mem, sites, keys)
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
