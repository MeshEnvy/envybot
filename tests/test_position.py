"""Book-canonical GPS helpers. Location lives on sites.yaml."""

from __future__ import annotations

import unittest

from envybot.position import (
    book_coord,
    display_name,
    is_placeholder_gps,
    node_alias,
    public_radio_name,
    resolve_book_position,
    site_binding,
)


class PlaceholderTests(unittest.TestCase):
    def test_zeros_and_onboard(self) -> None:
        self.assertTrue(is_placeholder_gps(0, 0))
        self.assertTrue(is_placeholder_gps(0, -119.03))
        self.assertTrue(is_placeholder_gps(14.009295, 120.996018))
        self.assertFalse(is_placeholder_gps(39.5, -119.8))


class ResolveTests(unittest.TestCase):
    def test_site_loc_via_node_bind(self) -> None:
        node = {"unit_id": "ME0003"}
        sites = {"foo": {"loc": [40.0, -117.0], "name": "Foo", "node": "me0003"}}
        pos = resolve_book_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)
        self.assertEqual(pos["site"], "foo")
        self.assertEqual(book_coord(node, "lon", sites), -117.0)

    def test_key_bind(self) -> None:
        sites = {"ophir": {"loc": [41.0, -119.0], "node": "me0003"}}
        pos = resolve_book_position({}, sites, key="me0003")
        assert pos is not None
        self.assertAlmostEqual(pos["lat"], 41.0)

    def test_node_lat_lon_ignored(self) -> None:
        node = {"unit_id": "ME0003", "lat": 39.5, "lon": -119.8}
        sites = {"foo": {"loc": [40.0, -117.0], "node": "me0003"}}
        pos = resolve_book_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)

    def test_no_position(self) -> None:
        self.assertIsNone(resolve_book_position({"unit_id": "ME0041"}, {}))
        self.assertIsNone(book_coord({"unit_id": "ME0041"}, "lat", {}))
        self.assertIsNone(site_binding("me0041", {"unit_id": "ME0041"}, {}))


class DisplayNameTests(unittest.TestCase):
    def test_alias_when_unbound(self) -> None:
        node = {"unit_id": "ME0041", "alias": "Yuki"}
        self.assertEqual(node_alias(node), "Yuki")
        self.assertEqual(display_name("me0041", node, {}), "Yuki")

    def test_site_wins_over_alias(self) -> None:
        node = {"unit_id": "ME0003", "alias": "Bag"}
        sites = {"ophir": {"name": "Ophir", "node": "me0003"}}
        self.assertEqual(display_name("me0003", node, sites), "Ophir")

    def test_public_radio_name_ignores_alias(self) -> None:
        node = {"unit_id": "ME0041", "alias": "Yuki"}
        self.assertEqual(public_radio_name("me0041", node, {}), "ME0041")


if __name__ == "__main__":
    unittest.main()
