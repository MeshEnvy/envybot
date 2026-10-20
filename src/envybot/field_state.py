"""Per-field failure counts for fleet row refresh and attention."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from envybot.jobs import JobOutcome, RadioJob, TIMER_JOB_KINDS, is_console_job, is_path_job
from envybot.poll import GET_GROUPS
from envybot.radio import AUDIT_GET_ATTEMPTS
from envybot.silence_bail import BAIL_PAYLOAD, GET_REPLY_UNPARSED, is_radio_silence_timeout

DEFAULT_FAILURE_ATTEMPTS = 10

_AUDIT_GET_FIELDS = frozenset(
    g for g, spec in GET_GROUPS.items() if spec.mode == "audit" or g in ("firmware", "bootloader", "ota")
)


def parse_field_failures(raw: Any | None) -> dict[str, int]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for k, v in raw.items():
            if not k:
                continue
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parse_field_failures(parsed)
    except json.JSONDecodeError:
        pass
    return {}


def failure_cap(field_key: str, *, default_attempts: int = DEFAULT_FAILURE_ATTEMPTS) -> int:
    if field_key == "login":
        return default_attempts
    if field_key in _AUDIT_GET_FIELDS:
        return AUDIT_GET_ATTEMPTS
    if field_key in GET_GROUPS:
        spec = GET_GROUPS[field_key]
        if spec.mode in ("audit", "inventory"):
            return AUDIT_GET_ATTEMPTS
    return default_attempts


def field_key_for_job(job: RadioJob) -> str | None:
    if job.kind == "login":
        return "login"
    if job.kind.startswith("get:"):
        group = job.kind.split(":", 1)[1]
        if group in (
            "neighbors_wait",
            "ota_ls_wait",
            "post_install_wait",
            "ota_ls_probe",
        ):
            return None
        if group == "neighbors_discover":
            return "neighbors"
        return group
    if job.kind.startswith("apply:"):
        field = job.kind.split(":", 1)[1]
        if field in ("force_clear", "clock", "push_advert"):
            return None
        return field
    if job.kind.startswith("cmd:"):
        return None
    return None


def field_label(field_key: str) -> str:
    if field_key == "login":
        return "Login"
    if field_key in GET_GROUPS:
        return field_key.replace("_", " ").title()
    return field_key.replace("_", " ").title()


def clear_field_failure(conn: sqlite3.Connection, unit: str, field_key: str) -> None:
    from envybot.history import get_last_seen, _upsert_last_seen

    seen = get_last_seen(conn, unit) or {}
    fails = parse_field_failures(seen.get("field_failures"))
    if field_key not in fails:
        return
    del fails[field_key]
    _upsert_last_seen(
        conn,
        unit,
        {
            "field_failures": json.dumps(fails) if fails else None,
            "updated_at": int(time.time()),
        },
    )
    conn.commit()


def increment_field_failure(conn: sqlite3.Connection, unit: str, field_key: str) -> int:
    from envybot.history import get_last_seen, _upsert_last_seen

    seen = get_last_seen(conn, unit) or {}
    fails = parse_field_failures(seen.get("field_failures"))
    count = int(fails.get(field_key) or 0) + 1
    fails[field_key] = count
    _upsert_last_seen(
        conn,
        unit,
        {
            "field_failures": json.dumps(fails),
            "updated_at": int(time.time()),
        },
    )
    conn.commit()
    return count


def record_job_outcome(
    conn: sqlite3.Connection | None,
    unit: str,
    job: RadioJob,
    outcome: JobOutcome,
    payload: Any | None,
) -> None:
    if conn is None or is_console_job(job) or is_path_job(job):
        return
    if job.kind in TIMER_JOB_KINDS:
        return
    field = field_key_for_job(job)
    if field is None:
        return
    if outcome in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
        clear_field_failure(conn, unit, field)
        return
    if outcome != JobOutcome.TIMEOUT:
        return
    if payload == GET_REPLY_UNPARSED:
        return
    if payload == BAIL_PAYLOAD or is_radio_silence_timeout(payload):
        increment_field_failure(conn, unit, field)
