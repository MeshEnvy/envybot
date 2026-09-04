"""SQLite history import and last_seen."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from envybot.history import (
    _backfill_sample_loc_from_sites,
    _backfill_sample_loc_v2,
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
    poll_snapshots,
    record_poll,
    source_histories,
    rolling_traffic,
    status_series,
)


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = kwargs.get("firmware_version")
        self.bootloader_version = kwargs.get("bootloader_version")
        self.base_hash = kwargs.get("base_hash")
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
                ts=int(time.time()) - 60,
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
            self.assertEqual(bats[0]["value"], 4.1)
            volts = history_series(conn, "me0001", "voltage")
            self.assertEqual(volts[0]["value"], 4.1)
            noise = history_series(conn, "me0001", "noise_floor")
            self.assertEqual(len(noise), 1)
            self.assertEqual(noise[0]["value"], -94)

    def test_record_ota_base_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(base_hash="0011223344556677", polled_groups=frozenset({"ota"})),
            )
            seen = get_last_seen(conn, "me0001")
            assert seen is not None
            self.assertEqual(seen["base_hash"], "0011223344556677")
            self.assertIsNotNone(seen["ota_at"])

    def test_record_poll_logs_site_loc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(
                    status={"battery_mv": 4050},
                    telemetry=[{"channel": 1, "type": "voltage", "value": 4.05}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=1_700_000_000,
                site_loc=(39.5296, -119.8138),
            )
            hist = source_histories(conn, "me0001", hours=99999, limit=10)
            st = hist["status"][0]
            te = hist["telemetry"][0]
            self.assertAlmostEqual(st["lat"], 39.5296)
            self.assertAlmostEqual(st["lon"], -119.8138)
            self.assertAlmostEqual(te["lat"], 39.5296)
            self.assertAlmostEqual(te["lon"], -119.8138)

    def test_backfill_sample_loc_from_current_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            conn.execute(
                "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
                (1_700_000_000, "me0001", json.dumps({"battery_mv": 4000})),
            )
            conn.execute(
                "INSERT INTO telemetry (ts, unit, type, value) VALUES (?, ?, ?, ?)",
                (1_700_000_000, "me0001", "voltage", 4.0),
            )
            conn.execute(
                "INSERT INTO telemetry (ts, unit, type, value, lat, lon) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1_700_000_100, "me0001", "voltage", 4.1, 40.0, -117.0),
            )
            conn.execute(
                "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
                (1_700_000_000, "me0002", json.dumps({"battery_mv": 3900})),
            )
            conn.commit()
            (book / "nodes.yaml").write_text(
                "nodes:\n  me0001: {}\n  me0002: {}\n",
                encoding="utf-8",
            )
            (book / "sites.yaml").write_text(
                "sites:\n  peak:\n    loc: [39.5, -119.8]\n    node: me0001\n",
                encoding="utf-8",
            )
            stamped = _backfill_sample_loc_from_sites(conn, book)
            self.assertGreater(stamped, 0)
            hist = source_histories(conn, "me0001", hours=99999, limit=10)
            self.assertAlmostEqual(hist["status"][0]["lat"], 39.5)
            by_ts = {row["ts"]: row for row in hist["telemetry"]}
            self.assertAlmostEqual(by_ts[1_700_000_000]["lon"], -119.8)
            self.assertAlmostEqual(by_ts[1_700_000_100]["lat"], 40.0)
            bench = source_histories(conn, "me0002", hours=99999, limit=10)
            self.assertIsNone(bench["status"][0]["lat"])
            self.assertEqual(_backfill_sample_loc_from_sites(conn, book), 0)

    def test_backfill_v2_stamps_bench_loc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            conn.execute(
                "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
                (1_700_000_000, "me0002", json.dumps({"battery_mv": 3900})),
            )
            conn.commit()
            (book / "nodes.yaml").write_text(
                "bench_loc: [39.5296, -119.8138]\n"
                "nodes:\n  me0002: {}\n",
                encoding="utf-8",
            )
            (book / "sites.yaml").write_text("sites: {}\n", encoding="utf-8")
            stamped = _backfill_sample_loc_v2(conn, book)
            self.assertGreater(stamped, 0)
            hist = source_histories(conn, "me0002", hours=99999, limit=10)
            self.assertAlmostEqual(hist["status"][0]["lat"], 39.5296)
            self.assertAlmostEqual(hist["status"][0]["lon"], -119.8138)
            self.assertEqual(_backfill_sample_loc_v2(conn, book), 0)

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

    def test_rolling_traffic_sums_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = 10_000
            window = 6 * 3600
            base = {
                "packets_recv": 1000,
                "packets_sent": 100,
                "recv_errors": 10,
                "uptime_secs": 1000,
            }
            record_poll(
                conn,
                unit="me0008",
                res=_Res(status=dict(base), polled_groups=frozenset({"status"})),
                ts=now - window - 1000,
            )
            record_poll(
                conn,
                unit="me0008",
                res=_Res(
                    status={**base, "packets_recv": 1100, "packets_sent": 120, "uptime_secs": 2000},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - window + 1000,
            )
            record_poll(
                conn,
                unit="me0008",
                res=_Res(
                    status={**base, "packets_recv": 1250, "packets_sent": 150, "uptime_secs": 3000},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - 1000,
            )
            rolled = rolling_traffic(conn, "me0008", now=now)
            assert rolled is not None
            # First segment is half outside the window; deltas are prorated.
            self.assertEqual(rolled["packets_recv"], 200)
            self.assertEqual(rolled["packets_sent"], 40)
            self.assertGreaterEqual(rolled["duration_secs"], window - 2000)

    def test_rolling_traffic_prorates_pre_window_anchor(self) -> None:
        """Counter deltas spanning before the window are scaled by overlap fraction."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            window = 6 * 3600
            now = 100_000
            since = now - window
            base = {
                "packets_recv": 0,
                "packets_sent": 0,
                "recv_errors": 0,
                "rx_airtime_secs": 0,
                "uptime_secs": 1000,
            }
            record_poll(
                conn,
                unit="me0012",
                res=_Res(status=dict(base), polled_groups=frozenset({"status"})),
                ts=since - 10_000,
            )
            record_poll(
                conn,
                unit="me0012",
                res=_Res(
                    status={
                        **base,
                        "packets_recv": 1000,
                        "rx_airtime_secs": 600,
                        "uptime_secs": 2000,
                    },
                    polled_groups=frozenset({"status"}),
                ),
                ts=since + 2000,
            )
            rolled = rolling_traffic(conn, "me0012", window_secs=window, now=now)
            assert rolled is not None
            overlap = 2000
            full = 2000 + 10_000
            self.assertEqual(rolled["duration_secs"], overlap)
            self.assertEqual(rolled["packets_recv"], int(round(1000 * overlap / full)))
            self.assertAlmostEqual(
                rolled["rx_airtime_pct"],
                round((600 * overlap / full / overlap) * 100, 1),
            )

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

    def test_derived_series_skips_zero_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            base = {
                "packets_recv": 100,
                "recv_errors": 10,
                "uptime_secs": 1000,
            }
            record_poll(
                conn,
                unit="me0010",
                res=_Res(status=dict(base), polled_groups=frozenset({"status"})),
                ts=1000,
            )
            record_poll(
                conn,
                unit="me0010",
                res=_Res(status=dict(base), polled_groups=frozenset({"status"})),
                ts=2000,
            )
            record_poll(
                conn,
                unit="me0010",
                res=_Res(
                    status={**base, "packets_recv": 200, "recv_errors": 20, "uptime_secs": 2000},
                    polled_groups=frozenset({"status"}),
                ),
                ts=5000,
            )
            record_poll(
                conn,
                unit="me0010",
                res=_Res(
                    status={**base, "packets_recv": 200, "recv_errors": 20, "uptime_secs": 3000},
                    polled_groups=frozenset({"status"}),
                ),
                ts=6000,
            )
            recv_rate = history_series(conn, "me0010", "recv_rate", since=0)
            self.assertEqual(len(recv_rate), 2)
            self.assertAlmostEqual(recv_rate[0]["value"], recv_rate[1]["value"])
            unreadable = history_series(conn, "me0010", "unreadable_pct", since=0)
            self.assertEqual(len(unreadable), 2)
            self.assertAlmostEqual(unreadable[0]["value"], unreadable[1]["value"])

    def test_source_histories_orthogonal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = int(time.time())
            record_poll(
                conn,
                unit="me0015",
                res=_Res(
                    status={"battery_mv": 4050, "packets_recv": 10, "packets_sent": 2, "uptime_secs": 100},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - 2000,
            )
            record_poll(
                conn,
                unit="me0015",
                res=_Res(
                    telemetry=[{"channel": 1, "type": "temperature", "value": 18.5}],
                    polled_groups=frozenset({"telemetry"}),
                ),
                ts=now - 1000,
            )
            hist = source_histories(conn, "me0015", hours=72, limit=10)
            self.assertEqual(len(hist["status"]), 1)
            self.assertEqual(len(hist["telemetry"]), 1)
            self.assertEqual(hist["status"][0]["ts"], now - 2000)
            self.assertAlmostEqual(hist["status"][0]["voltage"], 4.05)
            self.assertNotIn("temperature", hist["status"][0])
            self.assertEqual(hist["telemetry"][0]["ts"], now - 1000)
            self.assertAlmostEqual(hist["telemetry"][0]["temperature"], 18.5)
            self.assertNotIn("packets_recv", hist["telemetry"][0])

    def test_poll_snapshots_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = int(time.time())
            record_poll(
                conn,
                unit="me0011",
                res=_Res(
                    status={"battery_mv": 4100, "packets_recv": 10, "packets_sent": 2, "uptime_secs": 100},
                    telemetry=[{"channel": 1, "type": "temperature", "value": 22.5}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=now - 3600,
            )
            record_poll(
                conn,
                unit="me0011",
                res=_Res(
                    status={
                        "battery_mv": 4050,
                        "packets_recv": 25,
                        "packets_sent": 5,
                        "uptime_secs": 200,
                    },
                    telemetry=[{"channel": 1, "type": "temperature", "value": 23.0}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=now - 600,
            )
            polls = poll_snapshots(conn, "me0011", hours=72, limit=10)
            self.assertEqual(len(polls), 2)
            self.assertEqual(polls[0]["ts"], now - 600)
            self.assertEqual(polls[0]["delta_packets_recv"], 15)
            self.assertAlmostEqual(polls[0]["voltage"], 4.05)
            self.assertAlmostEqual(polls[0]["temperature"], 23.0)
            self.assertEqual(polls[0]["since_prev_secs"], 3000)
            self.assertNotIn("since_prev_secs", polls[1])

    def test_poll_snapshots_interpolates_temperature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = int(time.time())
            record_poll(
                conn,
                unit="me0013",
                res=_Res(
                    status={"battery_mv": 4100, "uptime_secs": 100},
                    telemetry=[{"channel": 1, "type": "temperature", "value": 10.0}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=now - 3000,
            )
            record_poll(
                conn,
                unit="me0013",
                res=_Res(
                    status={"battery_mv": 4080, "uptime_secs": 200},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - 2000,
            )
            record_poll(
                conn,
                unit="me0013",
                res=_Res(
                    status={"battery_mv": 4060, "uptime_secs": 300},
                    telemetry=[{"channel": 1, "type": "temperature", "value": 20.0}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=now - 1000,
            )
            polls = poll_snapshots(conn, "me0013", hours=72, limit=10)
            mid = next(p for p in polls if p["ts"] == now - 2000)
            self.assertAlmostEqual(mid["temperature"], 15.0)
            self.assertIn("temperature", mid.get("synthetic") or [])
            self.assertAlmostEqual(mid["delta_temperature"], 5.0)
            self.assertAlmostEqual(mid["delta_voltage"], -0.02)
            ends = [p for p in polls if p["ts"] != now - 2000]
            self.assertTrue(all("temperature" not in (p.get("synthetic") or []) for p in ends))
            newest = next(p for p in polls if p["ts"] == now - 1000)
            self.assertAlmostEqual(newest["delta_temperature"], 5.0)
            oldest = next(p for p in polls if p["ts"] == now - 3000)
            self.assertNotIn("delta_temperature", oldest)

    def test_poll_snapshots_uses_off_timestamp_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = int(time.time())
            record_poll(
                conn,
                unit="me0014",
                res=_Res(
                    status={"battery_mv": 4100, "uptime_secs": 100},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - 3000,
            )
            conn.execute(
                "INSERT INTO telemetry (ts, unit, channel, type, value) VALUES (?, ?, ?, ?, ?)",
                (now - 2970, "me0014", 1, "temperature", 10.0),
            )
            record_poll(
                conn,
                unit="me0014",
                res=_Res(
                    status={"battery_mv": 4080, "uptime_secs": 200},
                    polled_groups=frozenset({"status"}),
                ),
                ts=now - 2000,
            )
            record_poll(
                conn,
                unit="me0014",
                res=_Res(
                    status={"battery_mv": 4060, "uptime_secs": 300},
                    telemetry=[{"channel": 1, "type": "temperature", "value": 20.0}],
                    polled_groups=frozenset({"status", "telemetry"}),
                ),
                ts=now - 1000,
            )
            conn.commit()
            polls = poll_snapshots(conn, "me0014", hours=72, limit=10)
            oldest = next(p for p in polls if p["ts"] == now - 3000)
            mid = next(p for p in polls if p["ts"] == now - 2000)
            self.assertAlmostEqual(oldest["temperature"], 10.0)
            self.assertNotIn("temperature", oldest.get("synthetic") or [])
            self.assertAlmostEqual(mid["temperature"], 14.9)
            self.assertIn("temperature", mid.get("synthetic") or [])


if __name__ == "__main__":
    unittest.main()
