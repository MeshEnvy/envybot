"""Trust contact rows: bag/bench units and name fallback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.commands.trust import build_trust_rows, contact_adv_name


PUB_A = "a" * 64
PUB_B = "b" * 64
ADMIN = "AdminOneStrong1"


class ContactAdvNameTests(unittest.TestCase):
    def test_site_name_wins_over_book_name(self) -> None:
        node = {"name": "RAK4631 Repeater", "unit_id": "ME0003"}
        sites = {"ophir-hill": {"name": "Ophir", "node": "me0003"}}
        self.assertEqual(contact_adv_name(node, "ME0003", sites, key="me0003"), "Ophir")

    def test_missing_site_uses_unit_id(self) -> None:
        self.assertEqual(contact_adv_name({}, "ME0041", {}), "ME0041")

    def test_blank_site_uses_unit_id(self) -> None:
        self.assertEqual(contact_adv_name({"name": "Bag"}, "ME0041"), "ME0041")


class BuildTrustRowsTests(unittest.TestCase):
    def test_site_name_or_unit_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            yaml = YAML()
            with (book / "sites.yaml").open("w", encoding="utf-8") as fh:
                yaml.dump(
                    {"sites": {"ophir-hill": {"name": "Ophir", "loc": [39.3, -119.6], "node": "me0003"}}},
                    fh,
                )
            with nodes_path.open("w", encoding="utf-8") as fh:
                yaml.dump(
                    {
                        "next_unit": 42,
                        "nodes": {
                            "me0003": {
                                "unit_id": "ME0003",
                                "name": "RAK4631 Repeater",
                                "identity_pubkey": PUB_A,
                                "admin_password": ADMIN,
                                "firmware_platform": "meshcore",
                            },
                            "me0041": {
                                "unit_id": "ME0041",
                                "identity_pubkey": PUB_B,
                                "admin_password": "AdminTwoStrong2",
                                "firmware_platform": "meshcore",
                            },
                        },
                    },
                    fh,
                )
            rows = {r["unit_id"]: r for r in build_trust_rows(nodes_path)}
            self.assertEqual(rows["ME0003"]["adv_name"], "Ophir")
            self.assertEqual(rows["ME0041"]["adv_name"], "ME0041")
            self.assertIsNone(rows["ME0041"]["site"])
            self.assertEqual(rows["ME0041"]["adv_lat"], 0.0)
            self.assertEqual(rows["ME0041"]["adv_lon"], 0.0)


if __name__ == "__main__":
    unittest.main()
