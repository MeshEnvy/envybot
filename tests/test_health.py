"""Health checks from fleet snapshot fields."""

from __future__ import annotations

import unittest

from envybot.health import (
    RF_BAD_PCT,
    RF_WARN_LIFETIME_DELTA_PTS,
    compute_health,
)


def _traffic_window(
    *,
    recv: int = 50,
    sent: int = 10,
    covered_secs: int = 6 * 3600,
) -> dict:
    return {
        "packets_recv": recv,
        "packets_sent": sent,
        "duration_secs": covered_secs,
        "window_secs": 6 * 3600,
    }


class HealthTests(unittest.TestCase):
    def test_all_ok_when_healthy(self) -> None:
        health = compute_health(
            freshness="fresh",
            session={"state": "ok"},
            drift=None,
            status={"battery_mv": 4200, "packets_recv": 1000, "recv_errors": 100},
            telemetry={"voltage": 4.2, "temperature": 25.0},
            traffic_interval={
                "packets_recv": 50,
                "packets_sent": 10,
                "recv_errors": 5,
                "duration_secs": 3600,
                "rx_airtime_pct": 5.0,
            },
            traffic_window=_traffic_window(),
            status_rows=[
                {"ts": 1, "battery_mv": 4200, "uptime_secs": 1000},
                {"ts": 2, "battery_mv": 4190, "uptime_secs": 2000},
            ],
            reboot_count=0,
        )
        self.assertEqual(health["grade"], "ok")
        self.assertEqual(health["headline"], "healthy")
        self.assertEqual(health["summary"], "Healthy — all checks pass")
        self.assertEqual(health["issues"], [])

    def test_config_due_is_not_attention(self) -> None:
        health = compute_health(
            freshness="fresh",
            session={"state": "ok"},
            drift="due",
            status={"battery_mv": 4200, "packets_recv": 1000, "recv_errors": 10},
            telemetry={"voltage": 4.2, "temperature": 25.0},
            traffic_interval={
                "packets_recv": 50,
                "packets_sent": 10,
                "recv_errors": 2,
                "duration_secs": 3600,
                "rx_airtime_pct": 5.0,
            },
            traffic_window=_traffic_window(),
            status_rows=[
                {"ts": 1, "battery_mv": 4200, "uptime_secs": 1000},
                {"ts": 2, "battery_mv": 4190, "uptime_secs": 2000},
            ],
            reboot_count=0,
        )
        self.assertEqual(health["headline"], "healthy")
        self.assertEqual(health["issues"], [])
        cfg = next(c for c in health["checks"] if c["name"] == "Config")
        self.assertEqual(cfg["status"], "ok")
        self.assertIn("auto", (cfg["reason"] or "").lower())

    def test_config_leak_is_bad(self) -> None:
        health = compute_health(
            freshness="fresh",
            session={"state": "ok"},
            drift="leak",
            status={"battery_mv": 4200, "packets_recv": 1000, "recv_errors": 10},
            telemetry={"voltage": 4.2, "temperature": 25.0},
            traffic_interval={
                "packets_recv": 50,
                "packets_sent": 10,
                "recv_errors": 2,
                "duration_secs": 3600,
                "rx_airtime_pct": 5.0,
            },
            traffic_window=_traffic_window(),
            status_rows=[
                {"ts": 1, "battery_mv": 4200, "uptime_secs": 1000},
                {"ts": 2, "battery_mv": 4190, "uptime_secs": 2000},
            ],
            reboot_count=0,
        )
        cfg = next(i for i in health["issues"] if i["name"] == "Config")
        self.assertEqual(cfg["status"], "bad")
        self.assertIn("Last pull", cfg["reason"] or "")

    def test_reachability_never(self) -> None:
        health = compute_health(
            freshness="never",
            session=None,
            drift=None,
            status=None,
            telemetry=None,
            traffic_interval=None,
            status_rows=[],
        )
        self.assertEqual(health["grade"], "bad")
        self.assertEqual(health["headline"], "unreachable")
        self.assertTrue(any(i["name"] == "Reachability" for i in health["issues"]))

    def test_in_flight_reachability_unknown(self) -> None:
        health = compute_health(
            freshness="never",
            session={"state": "refreshing", "stage": "Logging in"},
            drift=None,
            status=None,
            telemetry=None,
            traffic_interval=None,
            status_rows=[],
        )
        reach = next(c for c in health["checks"] if c["name"] == "Reachability")
        self.assertEqual(reach["status"], "unknown")
        self.assertEqual(reach["reason"], "Poll in progress")
        self.assertEqual(health["headline"], "healthy")

    def test_paused_reachability_unknown(self) -> None:
        health = compute_health(
            freshness="never",
            session={"state": "unreachable"},
            drift=None,
            status=None,
            telemetry=None,
            traffic_interval=None,
            status_rows=[],
            paused=True,
        )
        reach = next(c for c in health["checks"] if c["name"] == "Reachability")
        self.assertEqual(reach["status"], "unknown")
        self.assertEqual(reach["reason"], "Polling paused")
        self.assertEqual(health["headline"], "paused")

    def test_power_low_voltage(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 3400},
            telemetry={"voltage": 3.4},
            traffic_interval={"packets_recv": 10, "packets_sent": 5, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=10, sent=5),
            status_rows=[{"battery_mv": 3400, "uptime_secs": 100}],
            reboot_count=0,
        )
        self.assertEqual(health["grade"], "bad")
        self.assertEqual(health["headline"], "attention")
        power = next(c for c in health["checks"] if c["name"] == "Power")
        self.assertEqual(power["status"], "bad")

    def test_power_high_voltage_warn(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4280},
            telemetry={"voltage": 4.28},
            traffic_interval={"packets_recv": 10, "packets_sent": 5, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=10, sent=5),
            status_rows=[{"battery_mv": 4280, "uptime_secs": 100}],
            reboot_count=0,
        )
        power = next(c for c in health["checks"] if c["name"] == "Power")
        self.assertEqual(power["status"], "warn")
        self.assertIn("4.25", power.get("reason") or "")

    def test_power_high_voltage_bad(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4360},
            telemetry={"voltage": 4.36},
            traffic_interval={"packets_recv": 10, "packets_sent": 5, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=10, sent=5),
            status_rows=[{"battery_mv": 4360, "uptime_secs": 100}],
            reboot_count=0,
        )
        self.assertEqual(health["headline"], "attention")
        power = next(c for c in health["checks"] if c["name"] == "Power")
        self.assertEqual(power["status"], "bad")
        self.assertIn("4.35", power.get("reason") or "")

    def test_power_ignores_telemetry_voltage(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4200},
            telemetry={"voltage": 3.4},
            traffic_interval={"packets_recv": 10, "packets_sent": 5, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=10, sent=5),
            status_rows=[
                {"ts": 1, "battery_mv": 4200, "uptime_secs": 1000},
                {"ts": 2, "battery_mv": 4190, "uptime_secs": 2000},
            ],
            reboot_count=0,
        )
        power = next(c for c in health["checks"] if c["name"] == "Power")
        self.assertEqual(power["status"], "ok")
        self.assertIn("4.20 V", power.get("reason") or "")

    def test_stability_reboots(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 100, "recv_errors": 0},
            telemetry={"voltage": 4.0},
            traffic_interval={"packets_recv": 5, "packets_sent": 1, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=5, sent=1),
            status_rows=[
                {"uptime_secs": 5000},
                {"uptime_secs": 100},
                {"uptime_secs": 200},
            ],
            reboot_count=2,
        )
        stability = next(c for c in health["checks"] if c["name"] == "Stability")
        self.assertEqual(stability["status"], "bad")

    def test_stability_ack_suppresses_known_reboot(self) -> None:
        rows = [
            {"ts": 100, "uptime_secs": 2000},
            {"ts": 200, "uptime_secs": 100},
            {"ts": 300, "uptime_secs": 500},
        ]
        health = compute_health(
            freshness="fresh",
            session={"state": "ok"},
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 100, "recv_errors": 0},
            telemetry={"voltage": 4.0},
            traffic_interval={"packets_recv": 5, "packets_sent": 1, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=5, sent=1),
            status_rows=rows,
            stability_ack_ts=250,
        )
        self.assertEqual(health["headline"], "healthy")
        stability = next(c for c in health["checks"] if c["name"] == "Stability")
        self.assertEqual(stability["status"], "ok")

    def test_stability_ack_returns_after_new_reboot(self) -> None:
        rows = [
            {"ts": 100, "uptime_secs": 2000},
            {"ts": 200, "uptime_secs": 100},
            {"ts": 400, "uptime_secs": 3000},
            {"ts": 500, "uptime_secs": 50},
        ]
        health = compute_health(
            freshness="fresh",
            session={"state": "ok"},
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 100, "recv_errors": 0},
            telemetry={"voltage": 4.0},
            traffic_interval={"packets_recv": 5, "packets_sent": 1, "duration_secs": 3600},
            traffic_window=_traffic_window(recv=5, sent=1),
            status_rows=rows,
            stability_ack_ts=250,
        )
        self.assertEqual(health["headline"], "attention")
        stability = next(c for c in health["checks"] if c["name"] == "Stability")
        self.assertEqual(stability["status"], "warn")

    def test_traffic_deaf_warn(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 100, "recv_errors": 0},
            telemetry={"voltage": 4.0},
            traffic_interval={
                "packets_recv": 0,
                "packets_sent": 5,
                "duration_secs": 13 * 3600,
            },
            traffic_window=_traffic_window(recv=0, sent=5, covered_secs=13 * 3600),
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        traffic = next(c for c in health["checks"] if c["name"] == "Traffic")
        self.assertEqual(traffic["status"], "warn")

    def test_traffic_quiet_short_ok(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000},
            telemetry={"voltage": 4.0},
            traffic_interval={
                "packets_recv": 0,
                "packets_sent": 0,
                "duration_secs": 3600,
            },
            traffic_window=_traffic_window(recv=0, sent=0, covered_secs=3600),
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        traffic = next(c for c in health["checks"] if c["name"] == "Traffic")
        self.assertEqual(traffic["status"], "unknown")

    def test_traffic_dead_air_bad(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000},
            telemetry={"voltage": 4.0},
            traffic_interval={
                "packets_recv": 0,
                "packets_sent": 0,
                "duration_secs": 6 * 3600,
            },
            traffic_window=_traffic_window(recv=0, sent=0, covered_secs=6 * 3600),
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        traffic = next(c for c in health["checks"] if c["name"] == "Traffic")
        self.assertEqual(traffic["status"], "bad")
        self.assertIn("6 h", traffic.get("reason") or "")

    def test_rf_worsening_warn(self) -> None:
        lifetime_pct = 50.0
        interval_pct = lifetime_pct + RF_WARN_LIFETIME_DELTA_PTS + 1
        recv = 100
        errs = int(recv * interval_pct / (100 - interval_pct))
        lifetime_recv = 1000
        lifetime_errs = int(lifetime_recv * lifetime_pct / (100 - lifetime_pct))
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={
                "battery_mv": 4000,
                "packets_recv": lifetime_recv,
                "recv_errors": lifetime_errs,
            },
            telemetry={"voltage": 4.0},
            traffic_interval={
                "packets_recv": recv,
                "recv_errors": errs,
                "packets_sent": 1,
                "duration_secs": 3600,
            },
            traffic_window=_traffic_window(recv=recv, sent=1),
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        rf = next(c for c in health["checks"] if c["name"] == "RF quality")
        self.assertEqual(rf["status"], "warn")

    def test_rf_bad_threshold(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 10, "recv_errors": 90},
            telemetry={"voltage": 4.0},
            traffic_interval={
                "packets_recv": 1,
                "recv_errors": 99,
                "packets_sent": 0,
                "duration_secs": 3600,
            },
            traffic_window=_traffic_window(recv=1, sent=0),
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        rf = next(c for c in health["checks"] if c["name"] == "RF quality")
        self.assertEqual(rf["status"], "bad")
        self.assertGreaterEqual(RF_BAD_PCT, 90)


if __name__ == "__main__":
    unittest.main()
