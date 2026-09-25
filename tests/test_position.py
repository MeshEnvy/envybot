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
    resolve_display_position,
    resolve_map_position,
    site_binding,
    site_loc_for_unit,
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

    def test_bench_loc_is_display_only(self) -> None:
        node = {"unit_id": "ME0041"}
        doc = {"bench_loc": [39.5276, -119.8142]}
        self.assertIsNone(resolve_book_position(node, {}, key="me0041"))
        self.assertIsNone(resolve_map_position(node, {}, key="me0041"))
        pos = resolve_display_position(node, {}, key="me0041", doc=doc)
        assert pos is not None
        self.assertEqual(pos["source"], "bench")
        self.assertAlmostEqual(pos["lat"], 39.5276)
        loc = site_loc_for_unit("me0041", node, {}, doc=doc)
        assert loc is not None
        self.assertAlmostEqual(loc[0], 39.5276)

    def test_node_loc_overrides_bench(self) -> None:
        node = {"unit_id": "ME0041", "loc": [39.6, -119.9]}
        doc = {"bench_loc": [39.5276, -119.8142]}
        pos = resolve_display_position(node, {}, key="me0041", doc=doc)
        assert pos is not None
        self.assertEqual(pos["source"], "node")
        self.assertAlmostEqual(pos["lat"], 39.6)

    def test_site_wins_over_bench(self) -> None:
        node = {"unit_id": "ME0003", "loc": [39.6, -119.9]}
        sites = {"foo": {"loc": [40.0, -117.0], "node": "me0003"}}
        doc = {"bench_loc": [39.5276, -119.8142]}
        pos = resolve_display_position(node, sites, key="me0003", doc=doc)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 40.0)


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
