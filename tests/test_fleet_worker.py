"""Fleet worker persist: one group per record_group write."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from envybot.fleet_worker import PollAccumulator
from envybot.history import open_history, source_histories


class RecordGroupTests(unittest.TestCase):
    def test_later_group_does_not_replay_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            ctx = SimpleNamespace(conn=conn, nodes={"me0001": {}}, sites={})
            acc = PollAccumulator()
            acc.status = {
                "battery_mv": 3980,
                "packets_recv": 1000,
                "packets_sent": 400,
                "uptime_secs": 10_000,
            }
            acc.telemetry = [
                {"channel": 1, "type": "voltage", "value": 3.98},
                {"channel": 1, "type": "temperature", "value": 22.0},
            ]
            acc.record_group(ctx, "me0001", "status")
            acc.record_group(ctx, "me0001", "telemetry")

            n_status = conn.execute(
                "SELECT COUNT(*) FROM status WHERE unit = ?", ("me0001",)
            ).fetchone()[0]
            n_tele = conn.execute(
                "SELECT COUNT(*) FROM telemetry WHERE unit = ?", ("me0001",)
            ).fetchone()[0]
            self.assertEqual(n_status, 1)
            self.assertGreaterEqual(n_tele, 1)

            hist = source_histories(conn, "me0001", hours=72, limit=10)
            self.assertEqual(len(hist["status"]), 1)
            self.assertNotIn("delta_packets_recv", hist["status"][0])


if __name__ == "__main__":
    unittest.main()
