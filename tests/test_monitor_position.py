"""Poll GET due comes from sqlite, not YAML SET stamps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.history import open_history, record_poll
from envybot.poll import PollPolicy, due_groups, group_complete


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = kwargs.get("firmware_version", "v0.1.0")
        self.bootloader_version = kwargs.get("bootloader_version", "bl")
        self.firmware_platform = "meshcore"
        self.name = kwargs.get("name", "Repeater")
        self.lat = kwargs.get("lat", 0.0)
        self.lon = kwargs.get("lon", 0.0)
        self.node_clock = None
        self.status = {"battery_mv": 4000, "uptime_secs": 1}
        self.telemetry = []
        self.advert_interval_min = 0
        self.flood_advert_interval_h = 0
        self.acl = []
        self.neighbors = []
        self.polled_groups = kwargs.get(
            "polled_groups",
            frozenset(
                {
                    "firmware",
                    "bootloader",
                    "name",
                    "lat",
                    "lon",
                    "advert",
                    "flood_advert",
                    "status",
                    "telemetry",
                    "acl",
                    "neighbors",
                }
            ),
        )


class DueTests(unittest.TestCase):
    def test_empty_db_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            due = due_groups(conn, "me0001", policy=PollPolicy(), now=10)
            self.assertIn("firmware", due)
            self.assertIn("status", due)
            self.assertIn("lat", due)
            self.assertNotIn("path_hash", due)
            self.assertNotIn("dutycycle", due)

    def test_inventory_complete_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(conn, unit="me0001", res=_Res(), ts=10)
            self.assertTrue(group_complete(None, "firmware") is False)
            seen_due = due_groups(conn, "me0001", policy=PollPolicy(), now=20)
            self.assertNotIn("firmware", seen_due)
            later = due_groups(conn, "me0001", policy=PollPolicy(), now=10 + 86400)
            self.assertIn("status", later)
            self.assertNotIn("firmware", later)


if __name__ == "__main__":
    unittest.main()
