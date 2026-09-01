"""Clear-sky charging-expected marks for the fleet UI."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from envybot.sun import attach_sun, sun_at, sun_series

RENO = (39.5296, -119.8138)


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


class SunAtTests(unittest.TestCase):
    def test_reno_summer_noon_is_charging(self) -> None:
        # 2026-06-21 20:00 UTC ≈ 13:00 PDT, near solar noon.
        sun = sun_at(*RENO, _utc(2026, 6, 21, 20))
        assert sun is not None
        self.assertGreater(sun["elev"], 60)
        self.assertTrue(sun["charging"])
        self.assertEqual(sun["phase"], "day")
        self.assertEqual(sun["emoji"], "☀️")

    def test_reno_midnight_is_not_charging(self) -> None:
        sun = sun_at(*RENO, _utc(2026, 6, 21, 8))
        assert sun is not None
        self.assertLess(sun["elev"], 0)
        self.assertFalse(sun["charging"])
        self.assertEqual(sun["phase"], "night")
        self.assertEqual(sun["emoji"], "🌙")

    def test_placeholder_gps_none(self) -> None:
        self.assertIsNone(sun_at(0, 0, _utc(2026, 6, 21, 20)))
        self.assertIsNone(sun_at(14.009295, 120.996018, _utc(2026, 6, 21, 20)))

    def test_attach_sun_stamps_rows_with_logged_loc(self) -> None:
        rows = [{"ts": _utc(2026, 6, 21, 20), "voltage": 4.1, "lat": RENO[0], "lon": RENO[1]}]
        attach_sun(rows)
        self.assertTrue(rows[0]["sun"]["charging"])

    def test_attach_sun_skips_rows_without_loc(self) -> None:
        rows = [{"ts": _utc(2026, 6, 21, 20)}]
        attach_sun(rows)
        self.assertNotIn("sun", rows[0])

    def test_attach_sun_uses_fallback_loc(self) -> None:
        rows = [{"ts": _utc(2026, 6, 21, 20)}]
        attach_sun(rows, loc=RENO)
        self.assertTrue(rows[0]["sun"]["charging"])

    def test_sun_series_crosses_horizon(self) -> None:
        start = _utc(2026, 6, 21, 2)
        end = _utc(2026, 6, 22, 2)
        series = sun_series(*RENO, start, end, step=3600)
        self.assertGreater(len(series), 20)
        self.assertEqual(series[0]["ts"], start)
        self.assertEqual(series[-1]["ts"], end)
        elevs = [p["value"] for p in series]
        self.assertTrue(any(v > 20 for v in elevs))
        self.assertTrue(any(v < 0 for v in elevs))

    def test_attach_sun_prefers_row_loc(self) -> None:
        rows = [{"ts": _utc(2026, 6, 21, 8), "lat": RENO[0], "lon": RENO[1]}]
        attach_sun(rows, loc=RENO)
        self.assertFalse(rows[0]["sun"]["charging"])


if __name__ == "__main__":
    unittest.main()
