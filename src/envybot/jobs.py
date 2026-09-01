"""Fair serial job queue for fleet poll/apply. One radio exchange at a time."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

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

TIMER_JOB_KINDS = frozenset({"get:neighbors_wait"})


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
    manual_bump: bool = False
    has_inventory_gap: bool = False
    apply_aborted: bool = False
    succeeded: bool = False
    session_extra: dict[str, Any] = field(default_factory=dict)


ExecuteFn = Callable[[RadioJob, UnitQueue], Awaitable[tuple[JobOutcome, Any | None]]]
JobStartFn = Callable[[UnitQueue, RadioJob], Awaitable[None]] | None
JobDoneFn = Callable[[UnitQueue, RadioJob, JobOutcome, Any | None], Awaitable[None]] | None


class TransportQueue:
    """Fair mutex: one MeshCore send+wait at a time across all unit actors."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._holder: str | None = None
        self._waiting: dict[str, tuple[bool, bool, float]] = {}
        self._last_served: dict[str, float] = {}
        self._last_holder: str | None = None

    @property
    def holder(self) -> str | None:
        return self._holder

    def _pick_grantee(self) -> str | None:
        if not self._waiting:
            return None
        rows: list[tuple[int, int, int, float, str]] = []
        for key, (manual_bump, inventory, _) in self._waiting.items():
            tier = 0 if manual_bump else 1
            inv = 0 if inventory else 1
            prefer_other = 1 if key == self._last_holder else 0
            lrs = self._last_served.get(key, 0.0)
            rows.append((tier, inv, prefer_other, lrs, key))
        rows.sort()
        return rows[0][4]

    async def acquire(
        self,
        unit_key: str,
        *,
        manual_bump: bool = False,
        inventory: bool = False,
    ) -> None:
        async with self._cond:
            if self._holder is None:
                self._holder = unit_key
                self._last_served[unit_key] = time.monotonic()
                self._last_holder = unit_key
                return
            if self._holder == unit_key:
                return
            self._waiting[unit_key] = (manual_bump, inventory, time.monotonic())
            try:
                while True:
                    if self._holder is None and self._pick_grantee() == unit_key:
                        self._waiting.pop(unit_key, None)
                        self._holder = unit_key
                        self._last_served[unit_key] = time.monotonic()
                        self._last_holder = unit_key
                        return
                    await self._cond.wait()
            finally:
                self._waiting.pop(unit_key, None)

    async def release(self, unit_key: str) -> None:
        async with self._cond:
            if self._holder == unit_key:
                self._holder = None
            self._cond.notify_all()


@dataclass
class FleetScheduler:
    """Fair scheduler: per-unit actors share one transport queue."""

    units: dict[str, UnitQueue] = field(default_factory=dict)
    max_attempts: int = 10
    retry_delay: float = 0.0
    round_delay: float = 0.0
    transport: TransportQueue = field(default_factory=TransportQueue)
    _idle_waiters: list[asyncio.Future[None]] = field(default_factory=list, repr=False)
    _stop: bool = False
    _actors: dict[str, asyncio.Task[None]] = field(default_factory=dict, repr=False)

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
        busy_states = ("queued", "refreshing", "pulling", "pushing", "polling")
        state = uq.session_extra.get("state")
        if state in busy_states and uq.jobs:
            return 200, None
        uq.manual = True
        uq.manual_job = manual_job
        uq.manual_bump = True
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
        return sum(len(uq.jobs) for uq in self.units.values())

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
        for task in list(self._actors.values()):
            task.cancel()

    def _ensure_actor(
        self,
        uq: UnitQueue,
        execute: ExecuteFn,
        *,
        on_job_start: JobStartFn = None,
        on_job_done: JobDoneFn = None,
    ) -> None:
        key = uq.target.key
        if key in self._actors and not self._actors[key].done():
            return
        self._actors[key] = asyncio.create_task(
            self._unit_actor(uq, execute, on_job_start=on_job_start, on_job_done=on_job_done)
        )

    def _settle_job(
        self,
        uq: UnitQueue,
        job: RadioJob,
        outcome: JobOutcome,
        payload: Any | None,
        succeeded: dict[str, bool],
    ) -> None:
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
                dropped = uq.session_extra.setdefault("dropped_jobs", [])
                if isinstance(dropped, list):
                    dropped.append(job.kind)
                if job.future and not job.future.done():
                    job.future.set_exception(
                        TimeoutError(f"{job.kind} gave up after {self.max_attempts} attempts")
                    )
                if job.kind == "login":
                    uq.jobs.clear()
            else:
                uq.backoff_until = time.monotonic() + self.retry_delay

    async def _unit_actor(
        self,
        uq: UnitQueue,
        execute: ExecuteFn,
        *,
        on_job_start: JobStartFn = None,
        on_job_done: JobDoneFn = None,
    ) -> None:
        key = uq.target.key
        succeeded: dict[str, bool] = getattr(self, "_run_succeeded", {})
        try:
            while not self._stop:
                if not uq.jobs:
                    break
                now = time.monotonic()
                if uq.backoff_until > now:
                    await asyncio.sleep(min(uq.backoff_until - now, 0.25))
                    continue

                job = uq.jobs[0]
                needs_radio = job.kind not in TIMER_JOB_KINDS

                if needs_radio:
                    manual_bump = uq.manual_bump
                    if manual_bump:
                        uq.manual_bump = False
                    await self.transport.acquire(
                        key,
                        manual_bump=manual_bump,
                        inventory=uq.has_inventory_gap,
                    )

                outcome = JobOutcome.HARD_FAIL
                payload: Any | None = "stopped"
                try:
                    if on_job_start is not None:
                        await on_job_start(uq, job)
                    outcome, payload = await execute(job, uq)
                finally:
                    if needs_radio:
                        await self.transport.release(key)

                self._settle_job(uq, job, outcome, payload, succeeded)
                if on_job_done is not None:
                    await on_job_done(uq, job, outcome, payload)
        except asyncio.CancelledError:
            raise
        finally:
            self._actors.pop(key, None)

    async def run(
        self,
        execute: ExecuteFn,
        *,
        on_job_start: JobStartFn = None,
        on_job_done: JobDoneFn = None,
        once: bool = False,
        max_rounds: int = 0,
    ) -> dict[str, bool]:
        """Run unit actors until idle (or stopped). Returns unit_key -> succeeded this run."""
        succeeded: dict[str, bool] = {}
        self._run_succeeded = succeeded
        round_num = 0

        while not self._stop:
            active = [uq for uq in self.units.values() if uq.jobs]
            if not active:
                if once:
                    break
                await self.wait_for_work()
                if self._stop or not any(uq.jobs for uq in self.units.values()):
                    if once:
                        break
                    continue

            for uq in list(self.units.values()):
                if uq.jobs:
                    self._ensure_actor(uq, execute, on_job_start=on_job_start, on_job_done=on_job_done)

            if once:
                pending = [t for t in self._actors.values() if not t.done()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                break

            await asyncio.sleep(0.05)
            if not any(uq.jobs for uq in self.units.values()) and not self._actors:
                round_num += 1
                if max_rounds and round_num > max_rounds:
                    break

        self._run_succeeded = {}
        return succeeded

    # Legacy pick_next for unit tests of fairness ordering.
    def pick_next(self, now: float | None = None) -> tuple[UnitQueue, RadioJob] | None:
        now = now or time.monotonic()
        last = self.transport._last_holder
        candidates: list[tuple[int, int, int, float, str, UnitQueue, RadioJob]] = []
        for key, uq in self.units.items():
            if not uq.jobs:
                continue
            if uq.backoff_until > now:
                continue
            job = uq.jobs[0]
            tier = 0 if uq.manual_bump else 1
            inv = 0 if uq.has_inventory_gap else 1
            prefer_other = 1 if key == last else 0
            lrs = self.transport._last_served.get(key, 0.0)
            candidates.append((tier, inv, prefer_other, lrs, key, uq, job))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
        _, _, _, _, _, uq, job = candidates[0]
        return uq, job
