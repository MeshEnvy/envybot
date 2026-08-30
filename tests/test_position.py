"""Book-canonical GPS helpers."""

from __future__ import annotations

import unittest

from envybot.position import book_coord, is_placeholder_gps, resolve_book_position


class PlaceholderTests(unittest.TestCase):
    def test_zeros_and_onboard(self) -> None:
        self.assertTrue(is_placeholder_gps(0, 0))
        self.assertTrue(is_placeholder_gps(0, -119.03))
        self.assertTrue(is_placeholder_gps(14.009295, 120.996018))
        self.assertFalse(is_placeholder_gps(39.5, -119.8))


class ResolveTests(unittest.TestCase):
    def test_node_beats_site(self) -> None:
        node = {"lat": 39.5, "lon": -119.8, "site": "foo"}
        sites = {"foo": {"loc": [40.0, -117.0], "name": "Foo"}}
        pos = resolve_book_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "node")
        self.assertAlmostEqual(pos["lat"], 39.5)
        self.assertEqual(book_coord(node, "lon", sites), -119.8)

    def test_site_when_device_zero(self) -> None:
        node = {"lat": 0.0, "lon": 0.0, "site": "foo"}
        sites = {"foo": {"loc": [41.0, -119.0], "name": "Foo"}}
        pos = resolve_book_position(node, sites)
        assert pos is not None
        self.assertEqual(pos["source"], "site")
        self.assertAlmostEqual(pos["lat"], 41.0)

    def test_no_position(self) -> None:
        self.assertIsNone(resolve_book_position({"site": None}, {}))
        self.assertIsNone(book_coord({"lat": 0, "lon": 0}, "lat", {}))


if __name__ == "__main__":
    unittest.main()
