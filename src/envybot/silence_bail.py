"""Bail a poll/apply pass after consecutive radio silences."""

from __future__ import annotations

import sqlite3
from typing import Any

from envybot.jobs import (
    TIMER_JOB_KINDS,
    JobOutcome,
    RadioJob,
    UnitQueue,
    drop_remaining_poll_apply,
    is_console_job,
)

RADIO_SILENCE_STREAK_KEY = "radio_silence_streak"
BAIL_PAYLOAD = "bail"
BAIL_AFTER_SILENCES = 3
GET_REPLY_UNPARSED = "unparsed"


def job_kind_to_gap_name(kind: str) -> str | None:
    if kind.startswith("get:"):
        group = kind.split(":", 1)[1]
        if group in (
            "neighbors_wait",
            "ota_ls_wait",
            "post_install_wait",
            "ota_ls_probe",
        ):
            return None
        return group
    if kind.startswith("apply:"):
        field = kind.split(":", 1)[1]
        if field in ("force_clear", "clock", "push_advert"):
            return None
        return field
    if kind == "login":
        return None
    return None


def counts_toward_silence_bail(job: RadioJob) -> bool:
    from envybot.jobs import PATH_JOB_KINDS, is_path_job

    if is_console_job(job) or job.kind in TIMER_JOB_KINDS or is_path_job(job):
        return False
    if job.kind.startswith("cmd:"):
        return True
    return (
        job.kind == "login"
        or job.kind.startswith("get:")
        or job.kind.startswith("apply:")
    )


def resets_silence_streak(job: RadioJob, outcome: JobOutcome) -> bool:
    if outcome not in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
        return False
    if job.kind in TIMER_JOB_KINDS:
        return False
    return True


def is_radio_silence_timeout(payload: Any | None) -> bool:
    if payload == GET_REPLY_UNPARSED or payload == BAIL_PAYLOAD:
        return False
    if payload in ("not authed", "auth"):
        return False
    return True


def gap_names_for_kinds(kinds: list[str]) -> list[str]:
    names: list[str] = []
    for kind in kinds:
        gap = job_kind_to_gap_name(kind)
        if gap and gap not in names:
            names.append(gap)
    return names


def apply_silence_outcome(
    uq: UnitQueue,
    job: RadioJob,
    outcome: JobOutcome,
    payload: Any | None,
    *,
    conn: sqlite3.Connection | None,
    unit: str,
    log: Any | None = None,
) -> tuple[JobOutcome, Any | None]:
    """Adjust outcome after execute; may bail the rest of the pass."""
    if resets_silence_streak(job, outcome):
        uq.session_extra[RADIO_SILENCE_STREAK_KEY] = 0
        return outcome, payload
    if outcome != JobOutcome.TIMEOUT or payload == BAIL_PAYLOAD:
        return outcome, payload
    if not counts_toward_silence_bail(job) or not is_radio_silence_timeout(payload):
        return outcome, payload

    streak = int(uq.session_extra.get(RADIO_SILENCE_STREAK_KEY) or 0) + 1
    uq.session_extra[RADIO_SILENCE_STREAK_KEY] = streak
    if streak < BAIL_AFTER_SILENCES:
        return outcome, payload

    gap_kinds = [job.kind]
    dropped = drop_remaining_poll_apply(uq, skip_job=job)
    gap_kinds.extend(dropped)
    gaps = gap_names_for_kinds(gap_kinds)
    if conn is not None and gaps:
        from envybot.history import merge_refresh_gaps

        merge_refresh_gaps(conn, unit, gaps)
    uq.session_extra["bail_dropped"] = gap_kinds
    if log is not None:
        gap_text = ", ".join(gaps) if gaps else "none"
        log.step(
            f"refresh bailed after {BAIL_AFTER_SILENCES} silences "
            f"(will retry after cooldown; gaps: {gap_text})"
        )
    return JobOutcome.TIMEOUT, BAIL_PAYLOAD
