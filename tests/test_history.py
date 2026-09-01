"""SQLite history import and last_seen."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from envybot.history import (
    count_reboots,
    get_last_seen,
    history_series,
    import_jsonl,
    import_yaml_last_seen,
    insert_apply,
    interval_traffic,
    last_ok_apply,
    latest_status,
    migrate_legacy,
    open_history,
    record_poll,
    status_series,
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
                    status={
                        "battery_mv": 4100,
                        "uptime_secs": 7200,
                        "packets_recv": 3,
                        "err_events": 0,
                        "noise_floor": -94,
                    },
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
            noise = history_series(conn, "me0001", "noise_floor")
            self.assertEqual(len(noise), 1)
            self.assertEqual(noise[0]["value"], -94)

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

    def test_interval_traffic_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            base_status = {
                "packets_recv": 1000,
                "packets_sent": 200,
                "recv_errors": 100,
                "uptime_secs": 3600,
            }
            record_poll(
                conn,
                unit="me0005",
                res=_Res(status=dict(base_status), polled_groups=frozenset({"status"})),
                ts=1000,
            )
            record_poll(
                conn,
                unit="me0005",
                res=_Res(
                    status={
                        **base_status,
                        "packets_recv": 1500,
                        "packets_sent": 280,
                        "recv_errors": 250,
                        "uptime_secs": 7200,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=2000,
            )
            interval = interval_traffic(conn, "me0005")
            assert interval is not None
            self.assertEqual(interval["duration_secs"], 1000)
            self.assertFalse(interval["reboot_reset"])
            self.assertEqual(interval["packets_recv"], 500)
            self.assertEqual(interval["packets_sent"], 80)
            self.assertEqual(interval["recv_errors"], 150)
            self.assertIsNone(interval_traffic(conn, "me9999"))

    def test_interval_traffic_reboot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0006",
                res=_Res(
                    status={"packets_recv": 5000, "uptime_secs": 86400},
                    polled_groups=frozenset({"status"}),
                ),
                ts=1000,
            )
            record_poll(
                conn,
                unit="me0006",
                res=_Res(
                    status={"packets_recv": 100, "uptime_secs": 600},
                    polled_groups=frozenset({"status"}),
                ),
                ts=2000,
            )
            interval = interval_traffic(conn, "me0006")
            assert interval is not None
            self.assertTrue(interval["reboot_reset"])
            self.assertNotIn("packets_recv", interval)

    def test_interval_traffic_extended_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            base = {
                "packets_recv": 100,
                "packets_sent": 10,
                "recv_errors": 5,
                "recv_flood": 80,
                "recv_direct": 20,
                "sent_flood": 8,
                "sent_direct": 2,
                "flood_dups": 3,
                "direct_dups": 1,
                "tx_airtime_secs": 100,
                "rx_airtime_secs": 200,
                "uptime_secs": 1000,
            }
            record_poll(
                conn,
                unit="me0007",
                res=_Res(status=dict(base), polled_groups=frozenset({"status"})),
                ts=1000,
            )
            record_poll(
                conn,
                unit="me0007",
                res=_Res(
                    status={
                        **base,
                        "packets_recv": 150,
                        "recv_flood": 120,
                        "rx_airtime_secs": 500,
                        "uptime_secs": 2000,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=3000,
            )
            interval = interval_traffic(conn, "me0007")
            assert interval is not None
            self.assertEqual(interval["packets_recv"], 50)
            self.assertEqual(interval["recv_flood"], 40)
            self.assertEqual(interval["rx_airtime_secs"], 300)
            self.assertAlmostEqual(interval["rx_airtime_pct"], 15.0)

    def test_status_series_and_reboot_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            base_ts = int(time.time()) - 7200
            for i, uptime in enumerate([1000, 2000, 100]):
                record_poll(
                    conn,
                    unit="me0008",
                    res=_Res(
                        status={"uptime_secs": uptime, "packets_recv": i},
                        polled_groups=frozenset({"status"}),
                    ),
                    ts=base_ts + i * 3600,
                )
            rows = status_series(conn, "me0008", days=7)
            self.assertEqual(len(rows), 3)
            self.assertEqual(count_reboots(rows), 1)

    def test_history_derived_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0009",
                res=_Res(
                    status={
                        "packets_recv": 100,
                        "recv_errors": 10,
                        "rx_airtime_secs": 0,
                        "uptime_secs": 1000,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=1000,
            )
            record_poll(
                conn,
                unit="me0009",
                res=_Res(
                    status={
                        "packets_recv": 200,
                        "recv_errors": 30,
                        "rx_airtime_secs": 360,
                        "uptime_secs": 2000,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=4600,
            )
            unreadable = history_series(conn, "me0009", "unreadable_pct", since=0)
            self.assertEqual(len(unreadable), 1)
            self.assertAlmostEqual(unreadable[0]["value"], 16.67, places=1)
            recv_rate = history_series(conn, "me0009", "recv_rate", since=0)
            self.assertEqual(len(recv_rate), 1)
            self.assertAlmostEqual(recv_rate[0]["value"], 100.0)
            airtime = history_series(conn, "me0009", "airtime_pct", since=0)
            self.assertEqual(len(airtime), 1)
            self.assertAlmostEqual(airtime[0]["value"], 10.0)
            temps = history_series(conn, "me0009", "temperature", since=0)
            self.assertEqual(temps, [])


if __name__ == "__main__":
    unittest.main()
