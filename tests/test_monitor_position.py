"""Monitor treats book GPS as SET policy, not a pull."""

from __future__ import annotations

import unittest

from envybot.commands.monitor import (
    PollResult,
    PullPolicy,
    apply_poll_to_node,
    fleet_due_groups,
    fleet_group_complete,
    format_book_coord,
)


class DueTests(unittest.TestCase):
    def test_stamped_book_coords_are_complete(self) -> None:
        node = {"lat": 39.9, "lon": -119.3, "lat_pulled_at": 1, "lon_pulled_at": 1}
        self.assertTrue(fleet_group_complete(node, "lat"))
        self.assertTrue(fleet_group_complete(node, "lon"))
        due = fleet_due_groups(node, policy=PullPolicy(), now=10)
        self.assertNotIn("lat", due)
        self.assertNotIn("lon", due)

    def test_unstamped_book_coords_are_due(self) -> None:
        node = {"lat": 39.9, "lon": -119.3, "site": "russell-peak"}
        self.assertFalse(fleet_group_complete(node, "lat"))
        due = fleet_due_groups(node, policy=PullPolicy(), now=10)
        self.assertIn("lat", due)
        self.assertIn("lon", due)

    def test_zero_gps_uses_site_and_is_due(self) -> None:
        node = {"lat": 0.0, "lon": 0.0, "site": "foo"}
        sites = {"foo": {"loc": [41.0, -119.0]}}
        self.assertFalse(fleet_group_complete(node, "lat", sites))
        due = fleet_due_groups(node, policy=PullPolicy(), now=10, sites=sites)
        self.assertIn("lat", due)

    def test_no_book_position_skips_even_on_force(self) -> None:
        node = {"lat": 0.0, "lon": 0.0, "site": None}
        due = fleet_due_groups(node, policy=PullPolicy(force=True), now=10)
        self.assertNotIn("lat", due)
        self.assertNotIn("lon", due)


class ApplyTests(unittest.TestCase):
    def test_successful_set_stamps_book_value(self) -> None:
        node = {"lat": 39.9, "lon": -119.3}
        res = PollResult(
            "me0001",
            ok=True,
            lat=39.909448,
            lon=-119.328847,
            polled_groups=frozenset({"lat", "lon"}),
        )
        apply_poll_to_node(node, res, now=99)
        self.assertEqual(node["lat"], 39.909448)
        self.assertEqual(node["lon"], -119.328847)
        self.assertEqual(node["lat_pulled_at"], 99)
        self.assertEqual(node["lon_pulled_at"], 99)

    def test_failed_set_does_not_write_device_zero(self) -> None:
        node = {"lat": 39.9, "lon": -119.3}
        res = PollResult(
            "me0001",
            ok=True,
            lat=None,
            lon=None,
            polled_groups=frozenset({"lat", "lon"}),
        )
        apply_poll_to_node(node, res, now=99)
        self.assertEqual(node["lat"], 39.9)
        self.assertNotIn("lat_pulled_at", node)


class FormatTests(unittest.TestCase):
    def test_six_decimals(self) -> None:
        self.assertEqual(format_book_coord(39.909448), "39.909448")
        self.assertEqual(format_book_coord(-119.328847), "-119.328847")


if __name__ == "__main__":
    unittest.main()
