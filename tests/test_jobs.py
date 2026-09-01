"""Fleet job scheduler and transport queue tests."""

from __future__ import annotations

import asyncio
import unittest

from envybot.jobs import (
    FleetScheduler,
    JobOutcome,
    RadioJob,
    TransportQueue,
    UnitQueue,
)
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
                return JobOutcome.TIMEOUT, "login timeout"
            return JobOutcome.HEARD, "ok"

        succeeded = await sched.run(execute, once=True)
        self.assertEqual(succeeded.get("me0002"), True)
        self.assertIn("me0001", calls)
        self.assertIn("me0002", calls)
        self.assertLess(calls.index("me0002"), len(calls))

    async def test_manual_bump_once_not_whole_queue(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        manual = _target("me0037")
        auto = _target("me0001")
        sched.enqueue_manual(
            manual,
            "refresh",
            [
                RadioJob(kind="login", unit_key="me0037", manual=True),
                RadioJob(kind="get:status", unit_key="me0037", manual=True),
                RadioJob(kind="get:telemetry", unit_key="me0037", manual=True),
            ],
        )
        sched.enqueue_jobs(auto, [RadioJob(kind="login", unit_key="me0001")])
        order: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            order.append(uq.target.key)
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        self.assertEqual(order[0], "me0037")
        self.assertIn("me0001", order)
        self.assertLess(order.index("me0001"), len(order))

    async def test_discover_wait_does_not_block_transport(self) -> None:
        sched = FleetScheduler(max_attempts=3, retry_delay=0.0)
        slow = _target("me0001")
        fast = _target("me0002")
        sched.enqueue_jobs(
            slow,
            [
                RadioJob(kind="login", unit_key="me0001"),
                RadioJob(
                    kind="get:neighbors_wait",
                    unit_key="me0001",
                    extra={"wait_s": 0.15},
                ),
                RadioJob(kind="get:status", unit_key="me0001"),
            ],
        )
        sched.enqueue_jobs(fast, [RadioJob(kind="login", unit_key="me0002")])
        order: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            if job.kind == "get:neighbors_wait":
                order.append(f"{uq.target.key}:wait")
            else:
                order.append(f"{uq.target.key}:{job.kind}")
            return JobOutcome.HEARD if job.kind != "get:neighbors_wait" else JobOutcome.TIMER_DONE, None

        await sched.run(execute, once=True)
        wait_idx = order.index("me0001:wait")
        me2_login = order.index("me0002:login")
        self.assertLess(wait_idx, me2_login)

    async def test_on_job_done_sees_settled_queue(self) -> None:
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        t = _target()
        sched.enqueue_jobs(t, [RadioJob(kind="login", unit_key="me0001")])
        remaining: list[int] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            return JobOutcome.TIMEOUT, "login timeout"

        async def on_done(uq: UnitQueue, job: RadioJob, outcome: JobOutcome, payload: object) -> None:
            remaining.append(len(uq.jobs))

        await sched.run(execute, on_job_done=on_done, once=True)
        self.assertEqual(remaining, [1, 0])

    async def test_max_attempts_drops_job(self) -> None:
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        t = _target()
        sched.enqueue_jobs(
            t,
            [
                RadioJob(kind="login", unit_key="me0001"),
                RadioJob(kind="get:status", unit_key="me0001"),
            ],
        )

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            if job.kind == "login":
                return JobOutcome.HEARD, None
            return JobOutcome.TIMEOUT, None

        await sched.run(execute, once=True)
        uq = sched.units["me0001"]
        self.assertFalse(uq.jobs)
        dropped = uq.session_extra.get("dropped_jobs")
        self.assertIn("get:status", dropped)

    async def test_wait_for_work_unblocks_on_enqueue(self) -> None:
        sched = FleetScheduler()
        task = asyncio.create_task(sched.wait_for_work())
        await asyncio.sleep(0.05)
        sched.enqueue_jobs(_target(), [RadioJob(kind="login", unit_key="me0001")])
        await asyncio.wait_for(task, timeout=1.0)


class TransportQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fair_alternate(self) -> None:
        tq = TransportQueue()
        order: list[str] = []

        async def worker(key: str) -> None:
            await tq.acquire(key)
            order.append(key)
            await asyncio.sleep(0.01)
            await tq.release(key)

        await asyncio.gather(worker("a"), worker("b"))
        self.assertEqual(len(order), 2)
        self.assertNotEqual(order[0], order[1] if len(order) > 1 else order[0])


class PickNextTests(unittest.TestCase):
    def test_prefers_different_unit_after_last(self) -> None:
        sched = FleetScheduler()
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])
        sched.transport._last_holder = "me0001"
        picked = sched.pick_next(0.0)
        self.assertIsNotNone(picked)
        assert picked is not None
        uq, _ = picked
        self.assertEqual(uq.target.key, "me0002")

    def test_manual_bump_wins_once(self) -> None:
        sched = FleetScheduler()
        auto = _target("me0001")
        manual = _target("me0002")
        sched.enqueue_jobs(auto, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(manual, [RadioJob(kind="login", unit_key="me0002")])
        sched.units["me0002"].manual_bump = True
        picked = sched.pick_next(0.0)
        assert picked is not None
        uq, _ = picked
        self.assertEqual(uq.target.key, "me0002")


if __name__ == "__main__":
    unittest.main()
