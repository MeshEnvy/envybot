"""Privacy apply due logic. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.apply import apply_is_due, profile_id
from envybot.history import insert_apply, open_history, record_poll
from envybot.radio import format_book_coord


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = None
        self.bootloader_version = None
        self.firmware_platform = None
        self.name = kwargs.get("name")
        self.lat = kwargs.get("lat")
        self.lon = kwargs.get("lon")
        self.node_clock = None
        self.status = None
        self.telemetry = None
        self.advert_interval_min = kwargs.get("advert_interval_min")
        self.flood_advert_interval_h = None
        self.acl = None
        self.neighbors = None
        self.polled_groups = kwargs.get("polled_groups", frozenset({"name", "lat", "lon"}))


class ProfileTests(unittest.TestCase):
    def test_private_id(self) -> None:
        self.assertEqual(profile_id({"name": "Ophir"}, None), "private")

    def test_public_id_includes_name_and_gps(self) -> None:
        node = {"public": True, "name": "Ophir", "lat": 39.5, "lon": -119.8}
        self.assertIn("public:Ophir:39.50000", profile_id(node, None))


class DueTests(unittest.TestCase):
    def test_first_run_private_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            self.assertTrue(apply_is_due(conn, "me0001", {"name": "Ophir"}, None))

    def test_after_ok_private_not_due_unless_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            insert_apply(conn, unit="me0001", field="profile", desired="private", ok=True)
            self.assertFalse(apply_is_due(conn, "me0001", {"name": "Ophir"}, None))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(name="Ophir Hill", lat=39.5, lon=-119.8),
            )
            self.assertTrue(apply_is_due(conn, "me0001", {"name": "Ophir"}, None))

    def test_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            insert_apply(conn, unit="me0001", field="profile", desired="private", ok=True)
            self.assertTrue(apply_is_due(conn, "me0001", {}, None, force=True))


class FormatTests(unittest.TestCase):
    def test_six_decimals(self) -> None:
        self.assertEqual(format_book_coord(39.909448), "39.909448")
        self.assertEqual(format_book_coord(-119.328847), "-119.328847")
        self.assertEqual(format_book_coord(0.0), "0.000000")


if __name__ == "__main__":
    unittest.main()
