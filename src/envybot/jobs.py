"""Fair serial job queue for fleet poll/apply. One radio exchange at a time."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from envybot.poll import GET_GROUPS
from envybot.radio import RouterTarget


class JobOutcome(str, Enum):
    HEARD = "heard"
    TIMEOUT = "timeout"
    HARD_FAIL = "hard_fail"
    TIMER_DONE = "timer_done"


INVENTORY_GROUPS = frozenset(
    g for g, spec in GET_GROUPS.items() if spec.mode == "inventory"
)


@dataclass
class RadioJob:
    """One radio exchange (or a timer that releases the worker)."""

    kind: str
    unit_key: str
    attempt: int = 0
    manual: bool = False
    manual_job: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future[Any] | None = field(default=None, repr=False)


@dataclass
class UnitQueue:
    target: RouterTarget
    jobs: deque[RadioJob] = field(default_factory=deque)
    backoff_until: float = 0.0
    last_served: float = 0.0
    manual: bool = False
    manual_job: str | None = None
    has_inventory_gap: bool = False
    apply_aborted: bool = False
    succeeded: bool = False
    session_extra: dict[str, Any] = field(default_factory=dict)


ExecuteFn = Callable[[RadioJob, UnitQueue], Awaitable[tuple[JobOutcome, Any | None]]]


@dataclass
class FleetScheduler:
    """Fair scheduler: one in-flight job; timeout parks the unit."""

    units: dict[str, UnitQueue] = field(default_factory=dict)
    max_attempts: int = 10
    retry_delay: float = 0.0
    round_delay: float = 0.0
    _tick: float = 0.0
    _last_unit_key: str | None = None
    _idle_waiters: list[asyncio.Future[None]] = field(default_factory=list, repr=False)
    _stop: bool = False

    def get_or_create(self, target: RouterTarget) -> UnitQueue:
        key = target.key
        if key not in self.units:
            self.units[key] = UnitQueue(target=target)
        else:
            self.units[key].target = target
        return self.units[key]

    def enqueue_jobs(self, target: RouterTarget, jobs: list[RadioJob]) -> None:
        if not jobs:
            return
        uq = self.get_or_create(target)
        for job in jobs:
            job.unit_key = target.key
            uq.jobs.append(job)
        uq.has_inventory_gap = any(
            j.kind.startswith("get:") and j.kind.split(":", 1)[1] in INVENTORY_GROUPS
            for j in jobs
        )
        self._wake_idle()

    def enqueue_manual(
        self,
        target: RouterTarget,
        manual_job: str,
        jobs: list[RadioJob],
    ) -> tuple[int, str | None]:
        """Refresh / Pull / Push. Returns (http_status, error)."""
        uq = self.get_or_create(target)
        busy_states = ("refreshing", "pulling", "pushing", "polling")
        state = uq.session_extra.get("state")
        if state in busy_states and uq.jobs:
            return 200, None
        uq.manual = True
        uq.manual_job = manual_job
        uq.apply_aborted = False
        uq.succeeded = False
        for job in jobs:
            job.manual = True
            job.manual_job = manual_job
            job.unit_key = target.key
        uq.jobs.clear()
        uq.jobs.extend(jobs)
        uq.backoff_until = 0.0
        uq.has_inventory_gap = any(
            j.kind.startswith("get:") and j.kind.split(":", 1)[1] in INVENTORY_GROUPS
            for j in jobs
        )
        self._wake_idle()
        return 200, None

    def drop_auto_paused(self, paused_keys: set[str], *, manual_keys: set[str]) -> list[str]:
        dropped: list[str] = []
        for key in list(self.units.keys()):
            if key in manual_keys or key not in paused_keys:
                continue
            uq = self.units[key]
            if uq.manual:
                continue
            if uq.jobs:
                uq.jobs.clear()
                dropped.append(key)
        return dropped

    def pending_count(self) -> int:
        return sum(1 for uq in self.units.values() if uq.jobs)

    def pick_next(self, now: float | None = None) -> tuple[UnitQueue, RadioJob] | None:
        now = now or time.monotonic()
        candidates: list[tuple[int, int, float, str, UnitQueue, RadioJob]] = []
        for key, uq in self.units.items():
            if not uq.jobs:
                continue
            if uq.backoff_until > now:
                continue
            job = uq.jobs[0]
            tier = 0 if uq.manual else 1
            inv = 0 if uq.has_inventory_gap else 1
            lrs = uq.last_served
            prefer_other = 1 if key == self._last_unit_key else 0
            candidates.append((tier, inv, prefer_other, lrs, key, uq, job))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
        _, _, _, _, _, uq, job = candidates[0]
        return uq, job

    def _wake_idle(self) -> None:
        for fut in self._idle_waiters:
            if not fut.done():
                fut.set_result(None)
        self._idle_waiters.clear()

    async def wait_for_work(self) -> None:
        if self.pending_count() > 0:
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._idle_waiters.append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut in self._idle_waiters:
                self._idle_waiters.remove(fut)
            raise

    def stop(self) -> None:
        self._stop = True
        self._wake_idle()

    async def run(
        self,
        execute: ExecuteFn,
        *,
        on_job_start: Callable[[UnitQueue, RadioJob], Awaitable[None]] | None = None,
        on_job_done: Callable[[UnitQueue, RadioJob, JobOutcome, Any | None], Awaitable[None]] | None = None,
        once: bool = False,
        max_rounds: int = 0,
    ) -> dict[str, bool]:
        """Run until idle (or stopped). Returns unit_key -> succeeded this run."""
        succeeded: dict[str, bool] = {}
        round_num = 0
        while not self._stop:
            if self.pending_count() == 0:
                if once:
                    break
                await self.wait_for_work()
                if self._stop or self.pending_count() == 0:
                    if once:
                        break
                    continue

            now = time.monotonic()
            picked = self.pick_next(now)
            if picked is None:
                if once:
                    break
                if self.retry_delay > 0:
                    await asyncio.sleep(min(self.retry_delay, 1.0))
                elif self.round_delay > 0 and round_num > 0:
                    await asyncio.sleep(min(self.round_delay, 1.0))
                else:
                    await asyncio.sleep(0.05)
                if once and all(uq.backoff_until <= time.monotonic() for uq in self.units.values() if uq.jobs):
                    round_num += 1
                    if max_rounds and round_num > max_rounds:
                        break
                continue

            uq, job = picked
            self._last_unit_key = uq.target.key
            uq.last_served = time.monotonic()

            if on_job_start is not None:
                await on_job_start(uq, job)

            outcome, payload = await execute(job, uq)

            if on_job_done is not None:
                await on_job_done(uq, job, outcome, payload)

            if outcome in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
                uq.jobs.popleft()
                job.attempt = 0
                if job.future and not job.future.done():
                    job.future.set_result(payload)
                if not uq.jobs:
                    uq.succeeded = True
                    succeeded[uq.target.key] = True
                    if uq.manual:
                        uq.manual = False
                        uq.manual_job = None
            elif outcome == JobOutcome.HARD_FAIL:
                uq.jobs.clear()
                if job.future and not job.future.done():
                    job.future.set_exception(RuntimeError(str(payload or "hard fail")))
                uq.apply_aborted = True
            else:
                job.attempt += 1
                if self.max_attempts and job.attempt >= self.max_attempts:
                    uq.jobs.popleft()
                    if job.future and not job.future.done():
                        job.future.set_exception(
                            TimeoutError(f"{job.kind} gave up after {self.max_attempts} attempts")
                        )
                    if job.kind == "login":
                        uq.jobs.clear()
                else:
                    uq.backoff_until = time.monotonic() + self.retry_delay

            if once and self.pending_count() == 0:
                break

        return succeeded
