"""Fleet worker persist: one group per record_group write."""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.fleet_worker import (
    AUDIT_GET_GROUPS,
    PollAccumulator,
    WorkerContext,
    _execute_apply,
    build_apply_jobs,
    build_poll_jobs,
)
from envybot.radio import AUDIT_GET_ATTEMPTS
from envybot.apply import applicable_field_desireds
from envybot.history import last_ok_apply, open_history, source_histories, stamp_apply
from envybot.jobs import JobOutcome, RadioJob, UnitQueue
from envybot.radio import (
    FemRxgainUnsupported,
    OtaAutofetchUnsupported,
    PollLog,
    RouterTarget,
)


class BuildPollJobsTests(unittest.TestCase):
    def test_audit_get_jobs_use_lower_attempt_cap(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="aa" * 32,
            admin_password="secret",
        )
        jobs = build_poll_jobs(
            target,
            list(AUDIT_GET_GROUPS),
            do_apply=False,
            apply_due=False,
            force_apply=False,
            skip_discover=True,
        )
        audit_jobs = [j for j in jobs if j.kind.split(":", 1)[-1] in AUDIT_GET_GROUPS]
        self.assertTrue(audit_jobs)
        for job in audit_jobs:
            self.assertEqual(job.attempt_cap, AUDIT_GET_ATTEMPTS)
        self.assertEqual(jobs[0].kind, "login")
        self.assertIsNone(jobs[0].attempt_cap)


class RecordGroupTests(unittest.TestCase):
    def test_later_group_does_not_replay_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            ctx = SimpleNamespace(conn=conn, nodes={"me0001": {}}, sites={}, doc={})
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
            nodes_path=Path(tmp) / "nodes.yaml",
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

    async def test_ota_autofetch_unsupported_stamps_done(self) -> None:
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
            "identity_pubkey": "aa" * 32,
        }
        job = RadioJob(kind="apply:ota_autofetch", unit_key="me0048")
        uq = UnitQueue(target=target, jobs=deque([job]))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch(
                "envybot.fleet_worker.set_ota_autofetch_policy",
                new=AsyncMock(side_effect=OtaAutofetchUnsupported()),
            ):
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
            want = applicable_field_desireds(
                node, None, doc=ctx.doc, keys=ctx.keys, key="me0048"
            )["ota_autofetch"]
            self.assertEqual(outcome, JobOutcome.HEARD)
            self.assertTrue(payload)
            self.assertEqual(last_ok_apply(ctx.conn, "me0048", "ota_autofetch"), want)

    async def test_already_synced_field_logs_skip(self) -> None:
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
            "identity_pubkey": "aa" * 32,
        }
        job = RadioJob(kind="apply:advert", unit_key="me0048")
        uq = UnitQueue(target=target, jobs=deque([job]))
        log = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            ctx.log = log
            want = applicable_field_desireds(
                node, None, doc=ctx.doc, keys=ctx.keys, key="me0048"
            )["advert"]
            stamp_apply(ctx.conn, unit="me0048", field="advert", desired=want, ok=True)
            outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
        self.assertEqual(outcome, JobOutcome.HEARD)
        self.assertEqual(payload, "skip")
        log.substep.assert_called_once_with("advert: skip (synced)")

    async def test_fem_rxgain_unsupported_stamps_done(self) -> None:
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
            "identity_pubkey": "aa" * 32,
            "fem_rxgain": False,
        }
        job = RadioJob(kind="apply:fem_rxgain", unit_key="me0048")
        uq = UnitQueue(target=target, jobs=deque([job]))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch(
                "envybot.fleet_worker.set_fem_rxgain_policy",
                new=AsyncMock(side_effect=FemRxgainUnsupported()),
            ):
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
            want = applicable_field_desireds(
                node, None, doc=ctx.doc, keys=ctx.keys, key="me0048"
            )["fem_rxgain"]
            self.assertEqual(outcome, JobOutcome.HEARD)
            self.assertTrue(payload)
            self.assertEqual(last_ok_apply(ctx.conn, "me0048", "fem_rxgain"), want)

    async def test_push_advert_after_identity_set(self) -> None:
        target = RouterTarget(
            key="me0049",
            unit_id="ME0049",
            name="ME0049",
            site="mount-schader",
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
            "identity_pubkey": "aa" * 32,
        }
        uq = UnitQueue(target=target, jobs=deque())
        uq.session_extra["apply_identity_changed"] = True
        job = RadioJob(kind="apply:push_advert", unit_key="me0049")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch(
                "envybot.fleet_worker.push_flood_advert",
                new=AsyncMock(return_value=True),
            ) as push:
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
        self.assertEqual(outcome, JobOutcome.HEARD)
        self.assertTrue(payload)
        push.assert_awaited_once()

    async def test_push_advert_skips_when_identity_unchanged(self) -> None:
        target = RouterTarget(
            key="me0049",
            unit_id="ME0049",
            name="ME0049",
            site=None,
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        job = RadioJob(kind="apply:push_advert", unit_key="me0049")
        uq = UnitQueue(target=target, jobs=deque([job]))
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, node)
            with patch(
                "envybot.fleet_worker.push_flood_advert",
                new=AsyncMock(return_value=True),
            ) as push:
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)
        self.assertEqual(outcome, JobOutcome.HEARD)
        self.assertEqual(payload, "skip")
        push.assert_not_awaited()

    def test_build_apply_jobs_includes_push_advert(self) -> None:
        kinds = [j.kind for j in build_apply_jobs("me0049", force=False)]
        self.assertEqual(kinds[-2:], ["apply:clock", "apply:push_advert"])
        self.assertEqual(
            kinds[:3],
            ["apply:fem_rxgain", "apply:agc_reset_interval", "apply:rxgain"],
        )


if __name__ == "__main__":
    unittest.main()
