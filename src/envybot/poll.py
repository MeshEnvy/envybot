"""GET-only fleet poll. Writes sqlite. Never writes heard name or GPS into YAML."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from envybot.history import get_last_seen, record_poll
from envybot.nodes_doc import is_paused
from envybot.radio import (
    DEFAULT_MIN_POLL_INTERVAL,
    PULL_GROUP_ORDER,
    PULL_GROUPS,
    PullGroupSpec,
    PullPolicy,
    PollResult,
    RouterTarget,
    format_interval,
    format_pull_plan,
    poll_one,
    poll_summary,
)

# GET groups. Sticky identity (name/gps/advert/acl) is audit-only; SET via apply.
GET_GROUPS: dict[str, PullGroupSpec] = {
    "firmware": PullGroupSpec("inventory", "firmware_at"),
    "bootloader": PullGroupSpec("inventory", "bootloader_at"),
    "name": PullGroupSpec("audit", "name_at"),
    "lat": PullGroupSpec("audit", "gps_at"),
    "lon": PullGroupSpec("audit", "gps_at"),
    "advert": PullGroupSpec("audit", "advert_at"),
    "flood_advert": PullGroupSpec("audit", "flood_advert_at"),
    "acl": PullGroupSpec("audit", "acl_at"),
    "status": PullGroupSpec("periodic", "status_at"),
    "telemetry": PullGroupSpec("periodic", "telemetry_at"),
    "neighbors": PullGroupSpec("periodic", "neighbors_at"),
}

GET_GROUP_ORDER = tuple(GET_GROUPS.keys())
PERIODIC_GROUPS = tuple(g for g in GET_GROUP_ORDER if GET_GROUPS[g].mode == "periodic")
MANUAL_JOBS = frozenset({"refresh", "pull", "push"})


def refresh_due_groups() -> list[str]:
    """Live GET groups for a manual Refresh (interval ignored)."""
    return list(PERIODIC_GROUPS)


def pull_due_groups() -> list[str]:
    """All GET groups for a manual Pull."""
    return list(GET_GROUP_ORDER)


def manual_job_session_state(job: str) -> str:
    """UI/worker session state for a pending manual job."""
    return {"refresh": "refreshing", "pull": "pulling", "push": "pushing"}[job]


@dataclass
class PollPolicy:
    force: bool = False
    live_only: bool = False
    force_groups: frozenset[str] = frozenset()
    min_interval: float = DEFAULT_MIN_POLL_INTERVAL


def _stamp(seen: dict[str, Any] | None, key: str) -> int | None:
    if not seen:
        return None
    val = seen.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def group_complete(seen: dict[str, Any] | None, group: str) -> bool:
    spec = GET_GROUPS[group]
    return _stamp(seen, spec.pulled_at_key) is not None


def group_is_due(
    seen: dict[str, Any] | None,
    group: str,
    *,
    policy: PollPolicy,
    now: int,
) -> bool:
    spec = GET_GROUPS[group]
    if policy.force or group in policy.force_groups:
        return True
    if spec.mode == "audit":
        return False
    if policy.live_only and spec.mode != "periodic":
        return False
    if not group_complete(seen, group):
        return True
    if spec.mode == "inventory":
        return False
    stamp = _stamp(seen, spec.pulled_at_key)
    if stamp is None:
        return True
    interval = spec.interval if spec.interval is not None else policy.min_interval
    if interval <= 0:
        return True
    return (now - stamp) >= interval


def due_groups(
    conn: sqlite3.Connection,
    unit: str,
    *,
    policy: PollPolicy,
    now: int | None = None,
) -> list[str]:
    now = now or int(time.time())
    seen = get_last_seen(conn, unit)
    return [
        group
        for group in GET_GROUP_ORDER
        if group_is_due(seen, group, policy=policy, now=now)
    ]


def partition_due(
    targets: list[RouterTarget],
    conn: sqlite3.Connection,
    *,
    policy: PollPolicy,
    now: int | None = None,
) -> tuple[list[RouterTarget], list[RouterTarget]]:
    now = now or int(time.time())
    due: list[RouterTarget] = []
    skipped: list[RouterTarget] = []
    for target in targets:
        groups = due_groups(conn, target.key, policy=policy, now=now)
        target.due_groups = groups
        if groups:
            due.append(target)
        else:
            skipped.append(target)
    return due, skipped


def partition_paused(
    targets: list[RouterTarget],
    nodes: dict[str, Any],
    *,
    forced_keys: set[str] | None = None,
) -> tuple[list[RouterTarget], list[RouterTarget]]:
    """Split auto-work targets from paused ones. Manual Refresh/Pull/Push stays active."""
    forced = forced_keys or set()
    active: list[RouterTarget] = []
    paused: list[RouterTarget] = []
    for target in targets:
        if is_paused(nodes.get(target.key)) and target.key not in forced:
            paused.append(target)
        else:
            active.append(target)
    return active, paused


def _get_need_note(seen: dict[str, Any] | None, group: str) -> str:
    if not group_complete(seen, group):
        return "missing"
    return "refresh"


def format_get_plan(
    conn: sqlite3.Connection,
    unit: str,
    due_groups: list[str],
    *,
    policy: PollPolicy,
    now: int | None = None,
) -> tuple[str, str]:
    """Human plan for one unit: GET groups this session will fetch vs skip."""
    now = now or int(time.time())
    seen = get_last_seen(conn, unit)
    need = (
        ", ".join(f"{g} ({_get_need_note(seen, g)})" for g in due_groups) or "none"
    )
    skip_inv: list[str] = []
    skip_audit: list[str] = []
    skip_fresh: list[str] = []
    for group in GET_GROUP_ORDER:
        if group in due_groups:
            continue
        spec = GET_GROUPS[group]
        if spec.mode == "inventory":
            skip_inv.append(group)
        elif spec.mode == "audit":
            skip_audit.append(group)
        else:
            skip_fresh.append(group)
    skip_bits: list[str] = []
    if skip_inv:
        skip_bits.append(f"{', '.join(skip_inv)} (have)")
    if skip_audit:
        skip_bits.append(f"{', '.join(skip_audit)} (audit)")
    if skip_fresh:
        skip_bits.append(
            f"{', '.join(skip_fresh)} (fresh <{format_interval(policy.min_interval)})"
        )
    skip = "; ".join(skip_bits) if skip_bits else "none"
    return need, skip


def gaps_from_poll(res: PollResult) -> list[str]:
    gaps: list[str] = []
    if "firmware" in res.polled_groups and not res.firmware_version:
        gaps.append("firmware")
    if "bootloader" in res.polled_groups and res.bootloader_version is None:
        gaps.append("bootloader")
    if "name" in res.polled_groups and res.name is None:
        gaps.append("name")
    if "lat" in res.polled_groups and res.lat is None:
        gaps.append("lat")
    if "lon" in res.polled_groups and res.lon is None:
        gaps.append("lon")
    if "telemetry" in res.polled_groups and res.telemetry is None:
        gaps.append("telemetry")
    if "status" in res.polled_groups and not res.status:
        gaps.append("status")
    if "advert" in res.polled_groups and res.advert_interval_min is None:
        gaps.append("advert")
    if "flood_advert" in res.polled_groups and res.flood_advert_interval_h is None:
        gaps.append("flood_advert")
    if "acl" in res.polled_groups and res.acl is None:
        gaps.append("acl")
    if "neighbors" in res.polled_groups and res.neighbors is None:
        gaps.append("neighbors")
    return gaps


# Re-export names fleet/tests already know
__all__ = [
    "GET_GROUP_ORDER",
    "GET_GROUPS",
    "MANUAL_JOBS",
    "PERIODIC_GROUPS",
    "PULL_GROUP_ORDER",
    "PULL_GROUPS",
    "PollPolicy",
    "PollResult",
    "PullPolicy",
    "due_groups",
    "manual_job_session_state",
    "pull_due_groups",
    "refresh_due_groups",
    "format_get_plan",
    "format_interval",
    "format_pull_plan",
    "gaps_from_poll",
    "partition_due",
    "partition_paused",
    "poll_one",
    "poll_summary",
    "record_poll",
]
