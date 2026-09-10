"""Swim-lane job queue for fleet poll/apply. One radio command per unit per turn."""

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
    CANCELLED = "cancelled"


INVENTORY_GROUPS = frozenset(
    g for g, spec in GET_GROUPS.items() if spec.mode == "inventory"
)

TIMER_JOB_KINDS = frozenset(
    {"get:neighbors_wait", "get:ota_ls_wait", "get:post_install_wait"}
)

# Auto poll/apply only. Console and manual Refresh/Pull/Deploy are exempt.
DEFAULT_RETRY_DELAY_S = 60.0
DEFAULT_MISS_COOLDOWN_S = 3600.0
MANUAL_UI_JOBS = frozenset({"refresh", "pull", "deploy"})


def _auto_retry_policy(job: RadioJob, uq: UnitQueue) -> bool:
    """True when retry_delay and miss_cooldown apply (scheduled poll/apply only)."""
    if is_console_job(job):
        return False
    if job.manual or uq.manual:
        return False
    if uq.manual_job in MANUAL_UI_JOBS:
        return False
    return True


@dataclass
class RadioJob:
    """One radio exchange (or a timer that releases the worker)."""

    kind: str
    unit_key: str
    attempt: int = 0
    attempt_cap: int | None = None
    manual: bool = False
    manual_job: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future[Any] | None = field(default=None, repr=False)


@dataclass
class UnitQueue:
    target: RouterTarget
    jobs: deque[RadioJob] = field(default_factory=deque)
    backoff_until: float = 0.0
    cooldown_until: float = 0.0
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


def is_console_job(job: RadioJob | str) -> bool:
    kind = job.kind if isinstance(job, RadioJob) else job
    return kind.startswith("console:")


def is_login_job(job: RadioJob | str) -> bool:
    kind = job.kind if isinstance(job, RadioJob) else job
    return kind in ("login", "console:login")


def job_still_queued(uq: UnitQueue, job: RadioJob) -> bool:
    return any(j is job for j in uq.jobs)


def _pick_tier(uq: UnitQueue, job: RadioJob) -> int:
    """Console heads stay on the radio until heard, exhausted, or cancelled."""
    if is_console_job(job):
        return 0
    if uq.manual:
        return 1
    return 2


def keep_console_jobs(uq: UnitQueue) -> list[RadioJob]:
    """Remove non-console jobs. Returns the console jobs that remain."""
    kept = [job for job in uq.jobs if is_console_job(job)]
    uq.jobs.clear()
    uq.jobs.extend(kept)
    return kept


def drop_console_jobs(uq: UnitQueue, tab_id: str | None = None) -> list[RadioJob]:
    """Remove console jobs. Waiters are resolved by ConsoleManager."""
    dropped: list[RadioJob] = []
    kept: list[RadioJob] = []
    for job in uq.jobs:
        if is_console_job(job) and (tab_id is None or job.extra.get("tab_id") == tab_id):
            dropped.append(job)
        else:
            kept.append(job)
    uq.jobs.clear()
    uq.jobs.extend(kept)
    return dropped


def is_ota_poll_job(kind: str) -> bool:
    return kind == "get:ota" or kind.startswith("get:ota_")


def drop_ota_poll_jobs(uq: UnitQueue, *, keep_head: bool = True) -> list[str]:
    """Drop remaining OTA GET jobs after this firmware reports no CLI."""
    dropped: list[str] = []
    kept: list[RadioJob] = []
    head = uq.jobs[0] if uq.jobs else None
    for job in uq.jobs:
        if keep_head and job is head:
            kept.append(job)
            continue
        if is_ota_poll_job(job.kind):
            dropped.append(job.kind)
        else:
            kept.append(job)
    uq.jobs.clear()
    uq.jobs.extend(kept)
    return dropped


def drop_remaining_apply(uq: UnitQueue) -> list[str]:
    """Drop queued SET jobs after a hard apply fail or exhausted retries."""
    dropped: list[str] = []
    kept: list[RadioJob] = []
    for job in uq.jobs:
        if job.kind.startswith("apply:"):
            dropped.append(job.kind)
        else:
            kept.append(job)
    uq.jobs.clear()
    uq.jobs.extend(kept)
    uq.apply_aborted = True
    return dropped


@dataclass
class FleetScheduler:
    """Swim-lane scheduler: one radio command per unit per turn, then rotate."""

    units: dict[str, UnitQueue] = field(default_factory=dict)
    max_attempts: int = 10
    retry_delay: float = DEFAULT_RETRY_DELAY_S
    miss_cooldown: float = DEFAULT_MISS_COOLDOWN_S
    round_delay: float = 0.0
    _idle_waiters: list[asyncio.Future[None]] = field(default_factory=list, repr=False)
    _stop: bool = False
    _timer_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    def get_or_create(self, target: RouterTarget) -> UnitQueue:
        key = target.key
        if key not in self.units:
            self.units[key] = UnitQueue(target=target)
        else:
            self.units[key].target = target
        return self.units[key]

    def clear_cooldown(self, uq: UnitQueue) -> None:
        uq.cooldown_until = 0.0

    def is_on_cooldown(self, key: str, now: float | None = None) -> bool:
        uq = self.units.get(key.lower())
        if uq is None:
            return False
        now = now or time.monotonic()
        return uq.cooldown_until > now

    def _cancel_timer(self, uq: UnitQueue) -> None:
        task = uq.session_extra.pop("timer_task", None)
        if task is not None and not task.done():
            task.cancel()

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
        """Refresh / Pull / Deploy. Returns (http_status, error)."""
        uq = self.get_or_create(target)
        self._cancel_timer(uq)
        uq.manual = True
        uq.manual_job = manual_job
        uq.manual_bump = True
        uq.apply_aborted = False
        uq.succeeded = False
        for job in jobs:
            job.manual = True
            job.manual_job = manual_job
            job.unit_key = target.key
        console_kept = [job for job in uq.jobs if is_console_job(job)]
        uq.jobs.clear()
        uq.jobs.extend(console_kept)
        uq.jobs.extend(jobs)
        uq.backoff_until = 0.0
        self.clear_cooldown(uq)
        uq.last_served = 0.0
        uq.has_inventory_gap = any(
            j.kind.startswith("get:") and j.kind.split(":", 1)[1] in INVENTORY_GROUPS
            for j in jobs
        )
        self._wake_idle()
        return 200, None

    def cancel_console_jobs(self, key: str, tab_id: str | None = None) -> bool:
        """Drop queued console:* on this unit (one tab, or all). In-flight uses cancel_check."""
        uq = self.units.get(key.lower())
        if uq is None:
            return False
        return bool(drop_console_jobs(uq, tab_id))

    def enqueue_console(
        self,
        target: RouterTarget,
        jobs: list[RadioJob],
    ) -> None:
        """FIFO among console:* on this unit, still in front of poll/apply."""
        uq = self.get_or_create(target)
        uq.apply_aborted = False
        uq.succeeded = False
        for job in jobs:
            job.manual = True
            job.manual_job = "console"
            job.unit_key = target.key
        existing = list(uq.jobs)
        console_jobs = [job for job in existing if is_console_job(job)]
        other = [job for job in existing if not is_console_job(job)]
        uq.jobs.clear()
        uq.jobs.extend(console_jobs)
        uq.jobs.extend(jobs)
        uq.jobs.extend(other)
        uq.backoff_until = 0.0
        self.clear_cooldown(uq)
        self._wake_idle()

    def drop_auto_paused(self, paused_keys: set[str], *, manual_keys: set[str]) -> list[str]:
        dropped: list[str] = []
        for key in list(self.units.keys()):
            if key in manual_keys or key not in paused_keys:
                continue
            uq = self.units[key]
            if uq.manual:
                continue
            if uq.jobs:
                self._cancel_timer(uq)
                if keep_console_jobs(uq):
                    continue
                dropped.append(key)
        return dropped

    def pending_count(self) -> int:
        return sum(len(uq.jobs) for uq in self.units.values())

    def active_unit_count(self) -> int:
        return sum(1 for uq in self.units.values() if uq.jobs)

    def _wake_idle(self) -> None:
        for fut in self._idle_waiters:
            if not fut.done():
                fut.set_result(None)
        self._idle_waiters.clear()

    async def wait_for_work(self, timeout: float | None = None) -> bool:
        """Wait until a job is queued, stop(), or timeout. True if work is pending."""
        if self.pending_count() > 0:
            return True
        if self._stop:
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._idle_waiters.append(fut)
        try:
            if timeout is None:
                await fut
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            return self.pending_count() > 0
        except asyncio.CancelledError:
            raise
        finally:
            if fut in self._idle_waiters:
                self._idle_waiters.remove(fut)

    def stop(self) -> None:
        self._stop = True
        self._wake_idle()
        for uq in self.units.values():
            self._cancel_timer(uq)
        for task in list(self._timer_tasks):
            task.cancel()

    def _defer_console_future(self, job: RadioJob) -> bool:
        """ConsoleManager.on_cli_done resolves console:cli waiters."""
        return job.kind == "console:cli" and job.future is not None

    def _settle_job(
        self,
        uq: UnitQueue,
        job: RadioJob,
        outcome: JobOutcome,
        payload: Any | None,
        succeeded: dict[str, bool],
    ) -> None:
        defer = self._defer_console_future(job)
        head = uq.jobs[0] if uq.jobs else None
        if head is not job:
            if job.future and not job.future.done() and not defer:
                if outcome in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
                    job.future.set_result(payload)
                else:
                    job.future.set_exception(RuntimeError(str(payload or "superseded")))
            return
        if outcome in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
            uq.jobs.popleft()
            job.attempt = 0
            if job.future and not job.future.done() and not defer:
                job.future.set_result(payload)
            if not uq.jobs:
                uq.succeeded = True
                succeeded[uq.target.key] = True
                if uq.manual and uq.manual_job != "console":
                    uq.manual = False
                    uq.manual_job = None
        elif outcome == JobOutcome.CANCELLED:
            uq.jobs.popleft()
            job.attempt = 0
            if job.future and not job.future.done() and not defer:
                job.future.set_result({"cancelled": True})
        elif outcome == JobOutcome.HARD_FAIL:
            if job.future and not job.future.done() and not defer:
                job.future.set_exception(RuntimeError(str(payload or "hard fail")))
            if job.kind.startswith("apply:"):
                uq.jobs.popleft()
                drop_remaining_apply(uq)
            elif is_console_job(job):
                drop_console_jobs(uq, job.extra.get("tab_id"))
            else:
                uq.jobs.clear()
                uq.apply_aborted = True
        else:
            job.attempt += 1
            cap = job.attempt_cap or self.max_attempts
            if cap and job.attempt >= cap:
                uq.jobs.popleft()
                dropped = uq.session_extra.setdefault("dropped_jobs", [])
                if isinstance(dropped, list):
                    dropped.append(job.kind)
                if job.future and not job.future.done() and not defer:
                    job.future.set_exception(
                        TimeoutError(f"{job.kind} gave up after {cap} attempts")
                    )
                if job.kind == "login":
                    uq.jobs.clear()
                elif job.kind == "console:login":
                    drop_console_jobs(uq, job.extra.get("tab_id"))
                elif job.kind.startswith("apply:"):
                    extra = drop_remaining_apply(uq)
                    if isinstance(dropped, list):
                        dropped.extend(extra)
                if _auto_retry_policy(job, uq) and self.miss_cooldown > 0:
                    uq.cooldown_until = time.monotonic() + self.miss_cooldown
            else:
                if (
                    _auto_retry_policy(job, uq)
                    and self.retry_delay > 0
                ):
                    uq.backoff_until = time.monotonic() + self.retry_delay

    def _timer_in_flight(self) -> bool:
        return any(
            uq.session_extra.get("timer_task") is not None
            and not uq.session_extra["timer_task"].done()
            for uq in self.units.values()
        )

    def _soonest_backoff(self, now: float) -> float | None:
        waits = [
            uq.backoff_until - now
            for uq in self.units.values()
            if uq.jobs and uq.backoff_until > now
        ]
        if not waits:
            return None
        return min(waits)

    def pick_next_radio(self, now: float | None = None) -> tuple[UnitQueue, RadioJob] | None:
        """Pick the next radio-ready lane (swim-lane RR)."""
        now = now or time.monotonic()
        candidates: list[tuple[int, float, str, UnitQueue, RadioJob]] = []
        for key, uq in self.units.items():
            if not uq.jobs:
                continue
            if uq.backoff_until > now:
                continue
            task = uq.session_extra.get("timer_task")
            if task is not None and not task.done():
                continue
            job = uq.jobs[0]
            if job.kind in TIMER_JOB_KINDS:
                continue
            retry_rank = 0 if is_console_job(job) and job.attempt else 1
            candidates.append((_pick_tier(uq, job), retry_rank, uq.last_served, key, uq, job))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        uq = candidates[0][4]
        job = candidates[0][5]
        uq.manual_bump = False
        return uq, job

    def pick_next(self, now: float | None = None) -> tuple[UnitQueue, RadioJob] | None:
        """Alias for tests: same as pick_next_radio."""
        return self.pick_next_radio(now)

    def _start_pending_timers(
        self,
        execute: ExecuteFn,
        *,
        on_job_start: JobStartFn = None,
        on_job_done: JobDoneFn = None,
    ) -> bool:
        now = time.monotonic()
        succeeded: dict[str, bool] = getattr(self, "_run_succeeded", {})
        started = False
        for uq in self.units.values():
            if not uq.jobs:
                continue
            if uq.backoff_until > now:
                continue
            task = uq.session_extra.get("timer_task")
            if task is not None and not task.done():
                continue
            job = uq.jobs[0]
            if job.kind not in TIMER_JOB_KINDS:
                continue

            async def run_timer(
                bound_uq: UnitQueue = uq,
                bound_job: RadioJob = job,
            ) -> None:
                outcome = JobOutcome.HARD_FAIL
                payload: Any | None = "stopped"
                try:
                    if on_job_start is not None:
                        await on_job_start(bound_uq, bound_job)
                    outcome, payload = await execute(bound_job, bound_uq)
                except asyncio.CancelledError:
                    raise
                finally:
                    bound_uq.session_extra.pop("timer_task", None)
                self._settle_job(bound_uq, bound_job, outcome, payload, succeeded)
                if on_job_done is not None:
                    await on_job_done(bound_uq, bound_job, outcome, payload)

            timer_task = asyncio.create_task(run_timer())
            uq.session_extra["timer_task"] = timer_task
            self._timer_tasks.add(timer_task)
            timer_task.add_done_callback(self._timer_tasks.discard)
            started = True
        return started

    async def run(
        self,
        execute: ExecuteFn,
        *,
        on_job_start: JobStartFn = None,
        on_job_done: JobDoneFn = None,
        once: bool = False,
        max_rounds: int = 0,
    ) -> dict[str, bool]:
        """Dispatch radio jobs in swim-lane order until idle (or stopped)."""
        succeeded: dict[str, bool] = {}
        self._run_succeeded = succeeded
        round_num = 0

        while not self._stop:
            started_timer = self._start_pending_timers(
                execute, on_job_start=on_job_start, on_job_done=on_job_done
            )
            if started_timer:
                await asyncio.sleep(0)

            now = time.monotonic()
            picked = self.pick_next_radio(now)
            if picked is None:
                if self._timer_in_flight():
                    await asyncio.sleep(0.05)
                    continue
                wait = self._soonest_backoff(now)
                if wait is not None:
                    await asyncio.sleep(min(wait, 0.25))
                    continue
                if self.pending_count() == 0:
                    break
                if not once:
                    await self.wait_for_work()
                    if self._stop:
                        break
                    continue
                await asyncio.sleep(0.05)
                continue

            uq, job = picked
            uq.last_served = time.monotonic()

            outcome = JobOutcome.HARD_FAIL
            payload: Any | None = "stopped"
            try:
                if on_job_start is not None:
                    await on_job_start(uq, job)
                outcome, payload = await execute(job, uq)
            except asyncio.CancelledError:
                raise

            self._settle_job(uq, job, outcome, payload, succeeded)
            if on_job_done is not None:
                await on_job_done(uq, job, outcome, payload)

            round_num += 1
            if max_rounds and round_num >= max_rounds:
                break

        self._run_succeeded = {}
        return succeeded
