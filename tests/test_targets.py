"""Fleet target selection skips Meshtastic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.nodes_doc import is_meshcore_platform
from envybot.radio import load_targets


class PlatformTests(unittest.TestCase):
    def test_blank_is_meshcore(self) -> None:
        self.assertTrue(is_meshcore_platform({}))
        self.assertTrue(is_meshcore_platform({"firmware_platform": "meshcore"}))
        self.assertFalse(is_meshcore_platform({"firmware_platform": "meshtastic"}))


class LoadTargetsTests(unittest.TestCase):
    def test_skips_meshtastic_even_with_mc_creds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes = book / "nodes.yaml"
            sites = book / "sites.yaml"
            yaml = YAML()
            with nodes.open("w") as fh:
                yaml.dump(
                    {
                        "nodes": {
                            "me0001": {
                                "unit_id": "ME0001",
                                "firmware_platform": "meshtastic",
                                "identity_pubkey": "a" * 64,
                                "admin_password": "AdminOneStrong1",
                            },
                            "me0003": {
                                "unit_id": "ME0003",
                                "firmware_platform": "meshcore",
                                "identity_pubkey": "b" * 64,
                                "admin_password": "AdminTwoStrong2",
                            },
                        }
                    },
                    fh,
                )
            with sites.open("w") as fh:
                yaml.dump({"sites": {"ophir": {"node": "me0003", "loc": [39.3, -119.6]}}}, fh)
            targets = load_targets(nodes, deployed_only=False, include=None)
            self.assertEqual([t.key for t in targets], ["me0003"])

    def test_includes_bench_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes = book / "nodes.yaml"
            sites = book / "sites.yaml"
            yaml = YAML()
            with nodes.open("w") as fh:
                yaml.dump(
                    {
                        "nodes": {
                            "me0003": {
                                "unit_id": "ME0003",
                                "firmware_platform": "meshcore",
                                "identity_pubkey": "b" * 64,
                                "admin_password": "AdminTwoStrong2",
                            },
                            "me0043": {
                                "unit_id": "ME0043",
                                "firmware_platform": "meshcore",
                                "identity_pubkey": "c" * 64,
                                "admin_password": "AdminThreeStrong3",
                            },
                        }
                    },
                    fh,
                )
            with sites.open("w") as fh:
                yaml.dump({"sites": {"ophir": {"node": "me0003", "loc": [39.3, -119.6]}}}, fh)
            all_units = load_targets(nodes, deployed_only=False, include=None)
            self.assertEqual(sorted(t.key for t in all_units), ["me0003", "me0043"])
            deployed = load_targets(nodes, deployed_only=True, include=None)
            self.assertEqual([t.key for t in deployed], ["me0003"])

    def test_skips_decommissioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes = book / "nodes.yaml"
            sites = book / "sites.yaml"
            yaml = YAML()
            with nodes.open("w") as fh:
                yaml.dump(
                    {
                        "nodes": {
                            "me0007": {
                                "unit_id": "ME0007",
                                "firmware_platform": "meshcore",
                                "identity_pubkey": "a" * 64,
                                "admin_password": "AdminOneStrong1",
                                "decommissioned": 1787943600,
                            },
                            "me0003": {
                                "unit_id": "ME0003",
                                "firmware_platform": "meshcore",
                                "identity_pubkey": "b" * 64,
                                "admin_password": "AdminTwoStrong2",
                            },
                        }
                    },
                    fh,
                )
            with sites.open("w") as fh:
                yaml.dump({"sites": {"ophir": {"node": "me0003", "loc": [39.3, -119.6]}}}, fh)
            targets = load_targets(nodes, deployed_only=False, include=None)
            self.assertEqual([t.key for t in targets], ["me0003"])
