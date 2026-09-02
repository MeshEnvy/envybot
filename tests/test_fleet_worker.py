"""Fleet worker persist: one group per record_group write."""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.fleet_worker import PollAccumulator, WorkerContext, _execute_apply
from envybot.history import open_history, source_histories
from envybot.jobs import JobOutcome, RadioJob, UnitQueue
from envybot.radio import PollLog, RouterTarget


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


class ApplyTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, tmp: str, node: dict) -> WorkerContext:
        return WorkerContext(
            client=MagicMock(),
            conn=open_history(Path(tmp)),
            nodes={"me0048": node},
            sites={},
            doc={"nodes": {"me0048": node}},
            keys={},
            session=MagicMock(),
            log=PollLog(progress=False),
            cmd_timeout=9.0,
            login_timeout=11.0,
            discover_wait=0.0,
            skip_discover=True,
            do_poll=False,
            do_apply=True,
        )

    async def test_set_timeout_does_not_abort_queue(self) -> None:
        target = RouterTarget(
            key="me0048",
            unit_id="ME0048",
            name="ME0048",
            site=None,
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        job = RadioJob(kind="apply:advert", unit_key="me0048")
        flood = RadioJob(kind="apply:flood", unit_key="me0048")
        uq = UnitQueue(target=target, jobs=deque([job, flood]))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch("envybot.fleet_worker._set_cli", new=AsyncMock(return_value="timeout")):
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
        self.assertEqual(outcome, JobOutcome.TIMEOUT)
        self.assertEqual(payload, "advert")
        self.assertFalse(uq.apply_aborted)
        self.assertEqual([j.kind for j in uq.jobs], ["apply:advert", "apply:flood"])

    async def test_set_cli_error_hard_fails_without_clearing(self) -> None:
        target = RouterTarget(
            key="me0048",
            unit_id="ME0048",
            name="ME0048",
            site=None,
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        job = RadioJob(kind="apply:advert", unit_key="me0048")
        flood = RadioJob(kind="apply:flood", unit_key="me0048")
        uq = UnitQueue(target=target, jobs=deque([job, flood]))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch("envybot.fleet_worker._set_cli", new=AsyncMock(return_value="error")):
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
        self.assertEqual(outcome, JobOutcome.HARD_FAIL)
        self.assertEqual(payload, "advert")
        self.assertEqual([j.kind for j in uq.jobs], ["apply:advert", "apply:flood"])


if __name__ == "__main__":
    unittest.main()
