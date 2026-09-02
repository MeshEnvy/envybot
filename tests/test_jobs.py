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

    async def test_manual_runs_ahead_of_auto(self) -> None:
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
        self.assertEqual(order[:3], ["me0037", "me0037", "me0037"])
        self.assertEqual(order[3], "me0001")

    async def test_settle_ignores_superseded_job(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        t = _target("me0001")
        first = RadioJob(kind="login", unit_key="me0001")
        sched.enqueue_jobs(t, [first, RadioJob(kind="get:status", unit_key="me0001")])
        ran: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            ran.append(job.kind)
            if job is first:
                sched.enqueue_manual(
                    t,
                    "refresh",
                    [RadioJob(kind="get:telemetry", unit_key="me0001", manual=True)],
                )
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        self.assertEqual(ran, ["login", "get:telemetry"])
        self.assertFalse(sched.units["me0001"].jobs)
        self.assertTrue(sched.units["me0001"].succeeded)

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

    async def test_apply_timeout_retries_then_drops_remaining_sets(self) -> None:
        sched = FleetScheduler(max_attempts=2, retry_delay=0.0)
        t = _target()
        sched.enqueue_jobs(
            t,
            [
                RadioJob(kind="apply:advert", unit_key="me0001"),
                RadioJob(kind="apply:flood", unit_key="me0001"),
                RadioJob(kind="get:status", unit_key="me0001"),
            ],
        )
        kinds: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            kinds.append(job.kind)
            if job.kind == "apply:advert":
                return JobOutcome.TIMEOUT, "advert"
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        uq = sched.units["me0001"]
        self.assertEqual(kinds, ["apply:advert", "apply:advert", "get:status"])
        self.assertFalse(uq.jobs)
        self.assertTrue(uq.apply_aborted)
        self.assertIn("apply:advert", uq.session_extra.get("dropped_jobs") or [])
        self.assertIn("apply:flood", uq.session_extra.get("dropped_jobs") or [])

    async def test_apply_hard_fail_keeps_get_jobs(self) -> None:
        sched = FleetScheduler(max_attempts=10, retry_delay=0.0)
        t = _target()
        sched.enqueue_jobs(
            t,
            [
                RadioJob(kind="apply:advert", unit_key="me0001"),
                RadioJob(kind="apply:flood", unit_key="me0001"),
                RadioJob(kind="get:status", unit_key="me0001"),
            ],
        )
        kinds: list[str] = []

        async def execute(job: RadioJob, uq: UnitQueue) -> tuple[JobOutcome, object | None]:
            kinds.append(job.kind)
            if job.kind.startswith("apply:"):
                return JobOutcome.HARD_FAIL, "advert"
            return JobOutcome.HEARD, None

        await sched.run(execute, once=True)
        self.assertEqual(kinds, ["apply:advert", "get:status"])
        self.assertTrue(sched.units["me0001"].apply_aborted)

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
        self.assertTrue(await asyncio.wait_for(task, timeout=1.0))

    async def test_wait_for_work_timeout_returns_false(self) -> None:
        sched = FleetScheduler()
        woke = await sched.wait_for_work(timeout=0.05)
        self.assertFalse(woke)
        self.assertEqual(sched.pending_count(), 0)


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

    def test_manual_lane_wins_until_done(self) -> None:
        sched = FleetScheduler()
        auto = _target("me0001")
        manual = _target("me0002")
        sched.enqueue_jobs(auto, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_manual(
            manual, "refresh", [RadioJob(kind="login", unit_key="me0002")]
        )
        picked = sched.pick_next(0.0)
        assert picked is not None
        uq, _ = picked
        self.assertEqual(uq.target.key, "me0002")
        self.assertTrue(sched.units["me0002"].manual)

    def test_exclusive_pick_only_held_unit(self) -> None:
        sched = FleetScheduler()
        a = _target("me0001")
        b = _target("me0002")
        sched.enqueue_jobs(a, [RadioJob(kind="login", unit_key="me0001")])
        sched.enqueue_jobs(b, [RadioJob(kind="login", unit_key="me0002")])
        sched.set_exclusive("me0002")
        sched.enqueue_console(
            b,
            [RadioJob(kind="console:login", unit_key="me0002", manual=True, manual_job="console")],
        )
        picked = sched.pick_next(0.0)
        assert picked is not None
        uq, job = picked
        self.assertEqual(uq.target.key, "me0002")
        self.assertEqual(job.kind, "console:login")

    def test_clear_exclusive(self) -> None:
        sched = FleetScheduler()
        sched.set_exclusive("me0001")
        sched.clear_exclusive()
        self.assertIsNone(sched.exclusive_key)

    def test_console_cli_future_deferred_to_manager(self) -> None:
        sched = FleetScheduler()
        uq = sched.get_or_create(_target("me0052"))
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        job = RadioJob(
            kind="console:cli",
            unit_key="me0052",
            manual=True,
            manual_job="console",
            future=fut,
        )
        uq.jobs.append(job)
        sched._settle_job(uq, job, JobOutcome.HEARD, "OTA | fw v1", {})
        self.assertFalse(fut.done())
        loop.close()


if __name__ == "__main__":
    unittest.main()
