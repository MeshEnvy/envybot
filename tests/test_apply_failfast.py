"""Apply fail-fast when the first due SET gets no response."""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.fleet_worker import _execute_apply
from envybot.history import last_ok_apply, open_history, stamp_apply
from envybot.apply import applicable_field_desireds
from envybot.jobs import FleetScheduler, JobOutcome, RadioJob, UnitQueue
from envybot.radio import PollLog, RouterTarget


class ApplyFailFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_no_response_skips_lat(self) -> None:
        target = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="Ophir",
            site="ophir-hill",
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "name": "Ophir",
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        steps: list[str] = []
        log = PollLog(progress=False)
        log.step = lambda msg: steps.append(msg)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            name_job = RadioJob(kind="apply:name", unit_key="me0003")
            lat_job = RadioJob(kind="apply:lat", unit_key="me0003")
            uq = UnitQueue(target=target, jobs=deque([name_job, lat_job]))
            ctx = MagicMock(
                client=MagicMock(),
                conn=conn,
                nodes_path=Path(tmp) / "nodes.yaml",
                nodes={"me0003": node},
                sites={},
                doc={"nodes": {"me0003": node}},
                keys={},
                session=MagicMock(),
                log=log,
                cmd_timeout=9.0,
                max_attempts=10,
            )
            calls: list[str] = []

            async def fake_set_cli(_client, _target, cmd, **kwargs):
                calls.append(cmd)
                return "timeout"

            with patch("envybot.fleet_worker._set_cli", new=fake_set_cli):
                outcome, payload = await _execute_apply(name_job, uq, ctx, node, 1)

        self.assertEqual(outcome, JobOutcome.TIMEOUT)
        self.assertEqual(payload, "name")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("set name "))
        self.assertFalse(any(cmd.startswith("set lat") for cmd in calls))

    async def test_scheduler_drops_remaining_apply_after_exhausted_retries(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="x",
        )
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        sched.enqueue_jobs(
            target,
            [
                RadioJob(kind="apply:name", unit_key="me0001"),
                RadioJob(kind="apply:lat", unit_key="me0001"),
            ],
        )

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            if job.kind == "apply:name":
                return JobOutcome.TIMEOUT, "name"
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        uq = sched.units["me0001"]
        self.assertTrue(uq.apply_aborted)
        self.assertEqual([j.kind for j in uq.jobs], [])

    async def test_identity_stamped_when_only_metadata_due(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "name": "Test",
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
            "identity_pubkey": "cc" * 32,
        }
        job = RadioJob(kind="apply:name", unit_key="me0001")
        uq = UnitQueue(target=target, jobs=deque([job]))

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            applicable = applicable_field_desireds(node, None)
            for field, des in applicable.items():
                if field == "identity":
                    continue
                stamp_apply(conn, unit="me0001", field=field, desired=des, ok=True)

            ctx = MagicMock(
                client=MagicMock(),
                conn=conn,
                nodes_path=Path(tmp) / "nodes.yaml",
                nodes={"me0001": node},
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                session=MagicMock(),
                log=PollLog(progress=False),
                cmd_timeout=9.0,
                max_attempts=10,
            )
            with patch("envybot.fleet_worker._set_cli", new=AsyncMock(return_value="ok")):
                outcome, payload = await _execute_apply(job, uq, ctx, node, 1)

        self.assertEqual(outcome, JobOutcome.HEARD)
        self.assertTrue(payload)
        self.assertEqual(
            last_ok_apply(conn, "me0001", "identity"), applicable["identity"]
        )

    async def test_clock_timeout_is_retryable(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="x",
        )
        node = {"name": "Test"}
        job = RadioJob(kind="apply:clock", unit_key="me0001")
        uq = UnitQueue(target=target, jobs=deque([job]))
        uq.session_extra["login_clock"] = 1_700_000_000

        with tempfile.TemporaryDirectory() as tmp:
            ctx = MagicMock(
                client=MagicMock(),
                conn=open_history(Path(tmp)),
                nodes_path=Path(tmp) / "nodes.yaml",
                nodes={"me0001": node},
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                session=MagicMock(),
                log=PollLog(progress=False),
                cmd_timeout=9.0,
                max_attempts=10,
            )
            with patch(
                "envybot.fleet_worker.maybe_sync_repeater_clock",
                new=AsyncMock(return_value=(1_700_000_000, "timeout")),
            ) as sync:
                outcome, payload = await _execute_apply(job, uq, ctx, node, 3)

        self.assertEqual(outcome, JobOutcome.TIMEOUT)
        self.assertEqual(payload, "clock")
        sync.assert_awaited_once()
        self.assertEqual(sync.await_args.kwargs["attempt_num"], 3)
        self.assertEqual(sync.await_args.kwargs["attempt_cap"], 10)

    async def test_scheduler_retries_clock_then_keeps_advert(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="x",
        )
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        sched.enqueue_jobs(
            target,
            [
                RadioJob(kind="apply:clock", unit_key="me0001"),
                RadioJob(kind="apply:push_advert", unit_key="me0001"),
            ],
        )
        kinds: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            kinds.append(job.kind)
            if job.kind == "apply:clock":
                return JobOutcome.TIMEOUT, "clock"
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        uq = sched.units["me0001"]
        self.assertFalse(uq.apply_aborted)
        self.assertEqual(kinds, ["apply:clock", "apply:clock", "apply:push_advert"])
        self.assertEqual([j.kind for j in uq.jobs], [])


if __name__ == "__main__":
    unittest.main()
