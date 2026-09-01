"""Health checks from fleet snapshot fields."""

from __future__ import annotations

import unittest

from envybot.health import (
    RF_BAD_PCT,
    RF_WARN_LIFETIME_DELTA_PTS,
    compute_health,
)


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

    def test_config_leak_has_fix(self) -> None:
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
            status_rows=[
                {"ts": 1, "battery_mv": 4200, "uptime_secs": 1000},
                {"ts": 2, "battery_mv": 4190, "uptime_secs": 2000},
            ],
            reboot_count=0,
        )
        self.assertEqual(health["headline"], "attention")
        cfg = next(i for i in health["issues"] if i["name"] == "Config")
        self.assertIn("leak", cfg["reason"] or "")
        self.assertIn("Push", cfg.get("fix") or "")

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
            status_rows=[{"battery_mv": 3400, "uptime_secs": 100}],
            reboot_count=0,
        )
        self.assertEqual(health["grade"], "bad")
        self.assertEqual(health["headline"], "attention")
        power = next(c for c in health["checks"] if c["name"] == "Power")
        self.assertEqual(power["status"], "bad")

    def test_stability_reboots(self) -> None:
        health = compute_health(
            freshness="fresh",
            session=None,
            drift=None,
            status={"battery_mv": 4000, "packets_recv": 100, "recv_errors": 0},
            telemetry={"voltage": 4.0},
            traffic_interval={"packets_recv": 5, "packets_sent": 1, "duration_secs": 3600},
            status_rows=[
                {"uptime_secs": 5000},
                {"uptime_secs": 100},
                {"uptime_secs": 200},
            ],
            reboot_count=2,
        )
        stability = next(c for c in health["checks"] if c["name"] == "Stability")
        self.assertEqual(stability["status"], "bad")

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
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        traffic = next(c for c in health["checks"] if c["name"] == "Traffic")
        self.assertEqual(traffic["status"], "ok")

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
            status_rows=[{"uptime_secs": 1000}, {"uptime_secs": 2000}],
            reboot_count=0,
        )
        rf = next(c for c in health["checks"] if c["name"] == "RF quality")
        self.assertEqual(rf["status"], "bad")
        self.assertGreaterEqual(RF_BAD_PCT, 90)


if __name__ == "__main__":
    unittest.main()
