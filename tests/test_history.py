"""SQLite history import and last_seen."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from envybot.history import (
    get_last_seen,
    history_series,
    import_jsonl,
    import_yaml_last_seen,
    insert_apply,
    last_ok_apply,
    latest_status,
    migrate_legacy,
    open_history,
    record_poll,
)


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = kwargs.get("firmware_version")
        self.bootloader_version = kwargs.get("bootloader_version")
        self.firmware_platform = kwargs.get("firmware_platform")
        self.name = kwargs.get("name")
        self.lat = kwargs.get("lat")
        self.lon = kwargs.get("lon")
        self.node_clock = kwargs.get("node_clock")
        self.status = kwargs.get("status")
        self.telemetry = kwargs.get("telemetry")
        self.advert_interval_min = kwargs.get("advert_interval_min")
        self.flood_advert_interval_h = kwargs.get("flood_advert_interval_h")
        self.acl = kwargs.get("acl")
        self.neighbors = kwargs.get("neighbors")
        self.polled_groups = kwargs.get("polled_groups", frozenset())


class HistoryTests(unittest.TestCase):
    def test_record_poll_and_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            record_poll(
                conn,
                unit="me0001",
                res=_Res(
                    firmware_version="v0.1.3",
                    name="Repeater",
                    status={"battery_mv": 4100, "uptime_secs": 7200, "packets_recv": 3, "err_events": 0},
                    telemetry=[
                        {"channel": 1, "type": "voltage", "value": 4.1},
                        {"channel": 1, "type": "temperature", "value": 18.3},
                    ],
                    polled_groups=frozenset({"firmware", "name", "status", "telemetry"}),
                ),
                ts=100,
            )
            seen = get_last_seen(conn, "me0001")
            assert seen is not None
            self.assertEqual(seen["firmware_version"], "v0.1.3")
            self.assertEqual(seen["name_heard"], "Repeater")
            self.assertEqual(seen["battery_mv"], 4100)
            self.assertEqual(seen["uptime_secs"], 7200)
            self.assertEqual(seen["voltage"], 4.1)
            self.assertEqual(seen["temperature"], 18.3)
            bats = history_series(conn, "me0001", "battery_mv")
            self.assertEqual(len(bats), 1)
            self.assertEqual(bats[0]["value"], 4100)
            volts = history_series(conn, "me0001", "voltage")
            self.assertEqual(volts[0]["value"], 4.1)

    def test_apply_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            self.assertIsNone(last_ok_apply(conn, "me0001", "profile"))
            insert_apply(conn, unit="me0001", field="profile", desired="private", ok=True)
            self.assertEqual(last_ok_apply(conn, "me0001", "profile"), "private")

    def test_import_jsonl_then_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            jsonl = book / "data" / "fleet" / "polls.jsonl"
            jsonl.parent.mkdir(parents=True)
            jsonl.write_text(
                json.dumps(
                    {
                        "event": "monitor",
                        "ts": 50,
                        "unit": "me0002",
                        "polled_groups": ["status"],
                        "status": {"battery_mv": 3900},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            conn = migrate_legacy(book, {})
            seen = get_last_seen(conn, "me0002")
            assert seen is not None
            self.assertEqual(seen["battery_mv"], 3900)
            self.assertFalse(jsonl.exists())

    def test_yaml_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            n = import_yaml_last_seen(
                conn,
                {
                    "me0003": {
                        "name": "Ophir",
                        "firmware_version": "v1.16.0",
                        "firmware_pulled_at": 10,
                        "status": {"battery_mv": 4000, "uptime_secs": 9},
                        "status_pulled_at": 11,
                    }
                },
            )
            self.assertEqual(n, 1)
            seen = get_last_seen(conn, "me0003")
            assert seen is not None
            self.assertEqual(seen["name_heard"], "Ophir")
            self.assertEqual(seen["firmware_version"], "v1.16.0")

    def test_latest_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0004",
                res=_Res(
                    status={
                        "packets_recv": 120,
                        "packets_sent": 45,
                        "recv_errors": 2,
                        "err_events": 1,
                        "uptime_secs": 86400,
                        "recv_flood": 80,
                        "recv_direct": 40,
                        "last_snr": 5.5,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=200,
            )
            status = latest_status(conn, "me0004")
            assert status is not None
            self.assertEqual(status["packets_recv"], 120)
            self.assertEqual(status["recv_errors"], 2)
            self.assertEqual(status["uptime_secs"], 86400)
            self.assertEqual(status["recv_flood"], 80)
            self.assertAlmostEqual(status["last_snr"], 5.5)
            self.assertIsNone(latest_status(conn, "me9999"))


if __name__ == "__main__":
    unittest.main()
