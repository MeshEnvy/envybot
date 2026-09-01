"""Fleet job scheduler swim-lane tests."""

from __future__ import annotations

import asyncio
import unittest

from envybot.jobs import (
    FleetScheduler,
    JobOutcome,
    RadioJob,
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


def _no_domination(order: list[str]) -> None:
    """No unit may take two consecutive radio turns while another is ready."""
    for idx in range(1, len(order)):
        if order[idx] == order[idx - 1]:
            raise AssertionError(f"unit {order[idx]!r} dominated at index {idx}: {order}")


class FleetSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_unit_login_interleaves(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        for key in ("me0001", "me0002", "me0003"):
            sched.enqueue_jobs(_target(key), [RadioJob(kind="login", unit_key=key)])
        attempts: dict[str, int] = {k: 0 for k in ("me0001", "me0002", "me0003")}
        order: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            key = uq.target.key
            order.append(key)
            if job.kind != "login":
                return JobOutcome.HEARD, None
            attempts[key] += 1
            if attempts[key] <= 2:
                return JobOutcome.TIMEOUT, "login timeout"
            return JobOutcome.HEARD, "ok"

        await sched.run(execute, once=True)
        _no_domination(order)
        self.assertEqual(order[:3], ["me0001", "me0002", "me0003"])
        self.assertEqual(order[3:6], ["me0001", "me0002", "me0003"])
        self.assertEqual(order[6:9], ["me0001", "me0002", "me0003"])

    async def test_manual_bump_one_command_then_rotate(self) -> None:
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
        self.assertEqual(order[1], "me0001")
        self.assertEqual(order[2], "me0037")
        self.assertEqual(order.count("me0001"), 1)

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
                await asyncio.sleep(float(job.extra.get("wait_s", 0.15)))
            else:
                order.append(f"{uq.target.key}:{job.kind}")
            if job.kind == "get:neighbors_wait":
                return JobOutcome.TIMER_DONE, None
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        wait_idx = order.index("me0001:wait")
        me2_login = order.index("me0002:login")
        me1_status = order.index("me0001:get:status")
        self.assertLess(wait_idx, me2_login)
        self.assertLess(me2_login, me1_status)

    async def test_mid_run_enqueue_gets_turn(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        order: list[str] = []
        enqueued = asyncio.Event()

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            key = uq.target.key
            order.append(key)
            if key == "me0001" and job.attempt == 0:
                if not enqueued.is_set():
                    sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])
                    enqueued.set()
                return JobOutcome.TIMEOUT, "login timeout"
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        self.assertIn("me0002", order)
        me2_idx = order.index("me0002")
        self.assertLess(me2_idx, 10)

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


class PickNextTests(unittest.TestCase):
    def test_prefers_least_recently_served(self) -> None:
        sched = FleetScheduler()
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])
        sched.units["me0001"].last_served = 100.0
        sched.units["me0002"].last_served = 0.0
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
        self.assertFalse(sched.units["me0002"].manual_bump)


if __name__ == "__main__":
    unittest.main()
