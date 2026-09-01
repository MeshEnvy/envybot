"""Fleet job scheduler tests."""

from __future__ import annotations

import asyncio
import unittest

from envybot.jobs import FleetScheduler, JobOutcome, RadioJob, UnitQueue
from envybot.radio import RouterTarget


def _target(key: str = "me0001") -> RouterTarget:
    return RouterTarget(
        key=key,
        unit_id=key.upper(),
        name="Test",
        site=None,
        pubkey_hex="a" * 64,
        admin_password="secret",
    )


class FleetSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_parks_unit_and_serves_other(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])

        calls: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            calls.append(uq.target.key)
            if uq.target.key == "me0001" and job.attempt == 0:
                return JobOutcome.TIMEOUT, None
            return JobOutcome.HEARD, "ok"

        succeeded = await sched.run(execute, once=True)
        self.assertEqual(succeeded.get("me0002"), True)
        self.assertEqual(calls[0], "me0001")
        self.assertEqual(calls[1], "me0002")
        self.assertGreaterEqual(len(calls), 2)

    async def test_manual_priority_over_auto(self) -> None:
        sched = FleetScheduler(max_attempts=1, retry_delay=0.0)
        auto = _target("me0001")
        manual = _target("me0002")
        sched.enqueue_jobs(auto, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_manual(
            manual,
            "refresh",
            [RadioJob(kind="login", unit_key="me0002", manual=True)],
        )
        order: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            order.append(uq.target.key)
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        self.assertEqual(order[0], "me0002")

    async def test_max_attempts_drops_job(self) -> None:
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        t = _target()
        sched.enqueue_jobs(t, [RadioJob(kind="login", unit_key="me0001")])

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            return JobOutcome.TIMEOUT, None

        await sched.run(execute, once=True)
        self.assertFalse(sched.units["me0001"].jobs)

    async def test_wait_for_work_unblocks_on_enqueue(self) -> None:
        sched = FleetScheduler()
        task = asyncio.create_task(sched.wait_for_work())
        await asyncio.sleep(0.05)
        sched.enqueue_jobs(_target(), [RadioJob(kind="login", unit_key="me0001")])
        await asyncio.wait_for(task, timeout=1.0)


class PickNextTests(unittest.TestCase):
    def test_prefers_different_unit_after_last(self) -> None:
        sched = FleetScheduler()
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])
        sched._last_unit_key = "me0001"
        picked = sched.pick_next(0.0)
        self.assertIsNotNone(picked)
        assert picked is not None
        uq, _ = picked
        self.assertEqual(uq.target.key, "me0002")


if __name__ == "__main__":
    unittest.main()
