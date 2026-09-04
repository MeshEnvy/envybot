"""Open-Meteo weather cache and attach."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from envybot.history import open_history
from envybot.weather import (
    WEATHER_BACKFILL_META,
    _payload_from_raw,
    attach_weather,
    hour_floor,
    lat_lon_cell,
    lookup_weather,
    missing_hours,
    run_weather_backfill,
    weather_at,
    weather_backfill_pending,
)

RENO = (39.5296, -119.8138)


def _utc(y: int, m: int, d: int, hh: int) -> int:
    return int(datetime(y, m, d, hh, 0, tzinfo=timezone.utc).timestamp())


def _mock_hourly(start_ts: int, hours: int = 3) -> dict:
    times = []
    temps = []
    clouds = []
    for i in range(hours):
        ts = start_ts + i * 3600
        times.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M"))
        temps.append(18.0 + i)
        clouds.append(10 * i)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "cloud_cover": clouds,
            "shortwave_radiation": [100.0] * hours,
            "precipitation": [0.0] * hours,
            "weather_code": [0] * hours,
            "wind_speed_10m": [2.0] * hours,
            "wind_gusts_10m": [4.0] * hours,
            "wind_direction_10m": [180] * hours,
        }
    }


class WeatherHelpersTests(unittest.TestCase):
    def test_lat_lon_cell_rounds(self) -> None:
        self.assertEqual(lat_lon_cell(39.52964, -119.81381), (39.53, -119.814))

    def test_hour_floor(self) -> None:
        ts = _utc(2026, 6, 21, 13) + 900
        self.assertEqual(hour_floor(ts), _utc(2026, 6, 21, 13))

    def test_payload_label(self) -> None:
        payload = _payload_from_raw(
            {
                "temperature_2m": 18.4,
                "cloud_cover": 45,
                "precipitation": 0.0,
                "wind_speed_10m": 0.5,
            }
        )
        self.assertIn("18 °C ambient", payload["label"])
        self.assertIn("45% cloud", payload["label"])


class WeatherCacheTests(unittest.TestCase):
    def test_insert_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            hour = _utc(2026, 6, 21, 20)
            raw = {
                "temperature_2m": 22.0,
                "cloud_cover": 5,
                "precipitation": 0.0,
                "wind_speed_10m": 1.2,
            }
            lat_c, lon_c = lat_lon_cell(*RENO)
            conn.execute(
                "INSERT INTO weather_hourly (lat_cell, lon_cell, hour_ts, payload) "
                "VALUES (?, ?, ?, ?)",
                (lat_c, lon_c, hour, json.dumps(raw)),
            )
            conn.commit()
            wx = lookup_weather(conn, *RENO, hour)
            assert wx is not None
            self.assertEqual(wx["temp_c"], 22.0)
            self.assertEqual(wx["cloud_pct"], 5.0)

    @patch("envybot.weather._fetch_open_meteo")
    def test_weather_at_fetches_on_miss(self, fetch_mock) -> None:
        hour = _utc(2026, 6, 21, 20)
        fetch_mock.return_value = _mock_hourly(hour, hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            wx = weather_at(conn, *RENO, hour)
            assert wx is not None
            self.assertEqual(wx["temp_c"], 18.0)
            fetch_mock.assert_called()

    @patch("envybot.weather.ensure_weather_cached")
    def test_attach_weather_stamps_rows(self, ensure_mock) -> None:
        ensure_mock.return_value = 1
        hour = _utc(2026, 6, 21, 20)
        rows = [{"ts": hour, "lat": RENO[0], "lon": RENO[1]}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            lat_c, lon_c = lat_lon_cell(*RENO)
            conn.execute(
                "INSERT INTO weather_hourly (lat_cell, lon_cell, hour_ts, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    lat_c,
                    lon_c,
                    hour,
                    json.dumps(
                        {
                            "temperature_2m": 17.0,
                            "cloud_cover": 80,
                            "precipitation": 0.2,
                            "wind_speed_10m": 3.0,
                        }
                    ),
                ),
            )
            conn.commit()
            attach_weather(conn, rows, loc=RENO)
            self.assertIn("weather", rows[0])
            self.assertEqual(rows[0]["weather"]["temp_c"], 17.0)

    def test_missing_hours_detects_gaps(self) -> None:
        start = _utc(2026, 6, 21, 10)
        end = _utc(2026, 6, 21, 12)
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            lat_c, lon_c = lat_lon_cell(*RENO)
            conn.execute(
                "INSERT INTO weather_hourly (lat_cell, lon_cell, hour_ts, payload) "
                "VALUES (?, ?, ?, ?)",
                (lat_c, lon_c, start, "{}"),
            )
            conn.commit()
            missing = missing_hours(conn, *RENO, start, end)
            self.assertEqual(missing, [start + 3600, start + 7200])


class WeatherBackfillTests(unittest.TestCase):
    def test_backfill_pending_until_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            self.assertTrue(weather_backfill_pending(conn))
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (WEATHER_BACKFILL_META, "1"),
            )
            conn.commit()
            self.assertFalse(weather_backfill_pending(conn))

    @patch("envybot.weather.ensure_weather_cached")
    def test_backfill_skips_when_done(self, ensure_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            (book / "nodes.yaml").write_text(
                "next_unit: 2\nnodes:\n  me0001:\n    unit_id: ME0001\n",
                encoding="utf-8",
            )
            conn = open_history(book)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (WEATHER_BACKFILL_META, "1"),
            )
            conn.commit()
            result = run_weather_backfill(conn, book)
            self.assertTrue(result["skipped"])
            ensure_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
