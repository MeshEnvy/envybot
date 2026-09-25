"""GET-only fleet poll. Writes sqlite. Never writes heard name or GPS into YAML."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from envybot.history import get_last_seen, record_poll
from envybot.nodes_doc import is_paused
from envybot.radio import (
    AUDIT_POLL_INTERVAL,
    DEFAULT_MIN_POLL_INTERVAL,
    NEIGHBOR_POLL_INTERVAL,
    OTA_POLL_INTERVAL,
    PollResult,
    RouterTarget,
    format_interval,
    poll_summary,
)


@dataclass(frozen=True)
class PullGroupSpec:
    mode: str  # "inventory" | "audit" | "periodic"
    pulled_at_key: str
    interval: float | None = None


# GET groups. Audit identity (name/gps/advert/acl) reconciles stamps; SET via apply.
GET_GROUPS: dict[str, PullGroupSpec] = {
    "firmware": PullGroupSpec("inventory", "firmware_at"),
    "bootloader": PullGroupSpec("inventory", "bootloader_at"),
    "ota": PullGroupSpec("inventory", "ota_at"),
    "name": PullGroupSpec("audit", "name_at", interval=AUDIT_POLL_INTERVAL),
    "lat": PullGroupSpec("audit", "gps_at", interval=AUDIT_POLL_INTERVAL),
    "lon": PullGroupSpec("audit", "gps_at", interval=AUDIT_POLL_INTERVAL),
    "advert": PullGroupSpec("audit", "advert_at", interval=AUDIT_POLL_INTERVAL),
    "flood_advert": PullGroupSpec("audit", "flood_advert_at", interval=AUDIT_POLL_INTERVAL),
    "acl": PullGroupSpec("audit", "acl_at", interval=AUDIT_POLL_INTERVAL),
    "repeat": PullGroupSpec("audit", "repeat_at", interval=AUDIT_POLL_INTERVAL),
    "path_hash": PullGroupSpec("audit", "path_hash_at", interval=AUDIT_POLL_INTERVAL),
    "dutycycle": PullGroupSpec("audit", "dutycycle_at", interval=AUDIT_POLL_INTERVAL),
    "powersaving": PullGroupSpec("audit", "powersaving_at", interval=AUDIT_POLL_INTERVAL),
    "hop_retry": PullGroupSpec("audit", "hop_retry_at", interval=AUDIT_POLL_INTERVAL),
    "hop_retry_ms": PullGroupSpec("audit", "hop_retry_ms_at", interval=AUDIT_POLL_INTERVAL),
    "fem_rxgain": PullGroupSpec("audit", "fem_rxgain_at", interval=AUDIT_POLL_INTERVAL),
    "agc_reset_interval": PullGroupSpec("audit", "agc_reset_interval_at", interval=AUDIT_POLL_INTERVAL),
    "rxgain": PullGroupSpec("audit", "rxgain_at", interval=AUDIT_POLL_INTERVAL),
    "ota_autofetch": PullGroupSpec("audit", "ota_autofetch_at", interval=AUDIT_POLL_INTERVAL),
    "ota_status": PullGroupSpec("periodic", "ota_status_at", interval=OTA_POLL_INTERVAL),
    "ota_ls": PullGroupSpec("periodic", "ota_ls_at", interval=OTA_POLL_INTERVAL),
    "status": PullGroupSpec("periodic", "status_at"),
    "telemetry": PullGroupSpec("periodic", "telemetry_at"),
    "neighbors": PullGroupSpec("periodic", "neighbors_at", interval=NEIGHBOR_POLL_INTERVAL),
}

GET_GROUP_ORDER = tuple(GET_GROUPS.keys())
PERIODIC_GROUPS = tuple(g for g in GET_GROUP_ORDER if GET_GROUPS[g].mode == "periodic")
LIVE_GROUPS = tuple(g for g in PERIODIC_GROUPS if GET_GROUPS[g].interval is None)
DAILY_GROUPS = tuple(g for g in PERIODIC_GROUPS if GET_GROUPS[g].interval is not None)
OTA_CLI_GROUPS = frozenset({"ota", "ota_status", "ota_ls"})
MANUAL_JOBS = frozenset({"sync", "full", "stage", "install"})
IN_FLIGHT_STATES = frozenset(
    {
        "queued",
        "syncing",
        "full_syncing",
        "staging",
        "installing",
        "polling",
        "console",
        "pinning",
    }
)

_STAGE_LABELS = {
    "login": "Logging in",
    "get:firmware": "Fetching firmware",
    "get:bootloader": "Fetching bootloader",
    "get:ota": "Fetching OTA",
    "get:ota_status": "Fetching OTA status",
    "get:ota_ls_probe": "Querying OTA catalog",
    "get:ota_ls_wait": "OTA catalog wait",
    "get:ota_ls": "Fetching OTA catalog",
    "cmd:ota_pull": "Staging OTA pull",
    "cmd:ota_install": "Installing OTA",
    "get:post_install_wait": "Post-install wait",
    "get:name": "Fetching name",
    "get:lat": "Fetching GPS",
    "get:lon": "Fetching GPS",
    "get:advert": "Fetching advert",
    "get:flood_advert": "Fetching flood",
    "get:acl": "Fetching ACL",
    "get:status": "Fetching status",
    "get:telemetry": "Fetching telemetry",
    "get:neighbors_discover": "Discovering neighbors",
    "get:neighbors_wait": "Neighbor wait",
    "get:neighbors": "Fetching neighbors",
    "path:pin": "Pinning path",
    "path:clear": "Clearing route cache",
    "apply:force_clear": "Clearing stamps",
    "apply:name": "Setting name",
    "apply:lat": "Setting GPS",
    "apply:lon": "Setting GPS",
    "apply:advert": "Setting advert",
    "apply:flood": "Setting flood",
    "apply:guest": "Setting guest",
    "apply:admin": "Setting admin",
    "apply:path_hash": "Setting path hash",
    "apply:dutycycle": "Setting duty cycle",
    "apply:ota_autofetch": "Setting OTA autofetch",
    "apply:powersaving": "Setting powersaving",
    "apply:hop_retry": "Setting hop retry",
    "apply:hop_retry_ms": "Setting hop retry ms",
    "apply:fem_rxgain": "Setting FEM RX gain",
    "apply:agc_reset_interval": "Setting AGC reset interval",
    "apply:rxgain": "Setting radio RX gain",
    "apply:acl": "Setting ACL",
    "apply:clock": "Syncing clock",
    "apply:push_advert": "Pushing flood advert",
    "console:login": "Console login",
    "console:cli": "Console CLI",
}


def sync_due_groups() -> list[str]:
    """Live GET groups for manual Sync (status/telemetry)."""
    return list(LIVE_GROUPS)


def full_due_groups() -> list[str]:
    """All GET groups for manual Full sync."""
    return list(GET_GROUP_ORDER)


def refresh_due_groups() -> list[str]:
    return sync_due_groups()


def pull_due_groups() -> list[str]:
    return full_due_groups()


def manual_job_session_state(job: str) -> str:
    """UI/worker session state for a pending manual job."""
    return {
        "sync": "syncing",
        "full": "full_syncing",
        "stage": "staging",
        "install": "installing",
        "console": "console",
        "path": "pinning",
    }[job]


def job_stage_label(kind: str | None) -> str:
    """Short badge label for a scheduler job kind."""
    if not kind:
        return "Queued"
    if kind in _STAGE_LABELS:
        return _STAGE_LABELS[kind]
    _, _, tail = kind.partition(":")
    name = (tail or kind).replace("_", " ")
    if kind.startswith("get:"):
        return f"Fetching {name}"
    if kind.startswith("apply:"):
        return f"Setting {name}"
    return name[:1].upper() + name[1:] if name else "Queued"


def in_flight_session(
    *,
    manual_job: str | None,
    job_kind: str | None,
    attempt: int = 0,
    max_attempts: int = 0,
    due_groups: list[str] | None = None,
    apply: bool = False,
    error: str | None = None,
    queued: bool = False,
) -> dict[str, Any]:
    """Session payload for a unit that still has scheduler work."""
    if manual_job:
        state = manual_job_session_state(manual_job)
    elif queued:
        state = "queued"
    else:
        state = "polling"
    sess: dict[str, Any] = {
        "state": state,
        "stage": job_stage_label(job_kind),
        "kind": job_kind,
        "due_groups": list(due_groups or []),
        "apply": apply,
        "job": manual_job,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    if manual_job:
        sess["manual"] = True
    if error:
        sess["error"] = error
    return sess


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


def ota_cli_absent(seen: dict[str, Any] | None) -> bool:
    """True when this firmware already answered with no OTA CLI."""
    if not seen:
        return False
    try:
        flagged = int(seen.get("ota_unsupported") or 0)
    except (TypeError, ValueError):
        return False
    if not flagged:
        return False
    marked = seen.get("ota_unsupported_fw") or ""
    current = seen.get("firmware_version") or ""
    return marked == current


def omit_unsupported_ota(groups: list[str], seen: dict[str, Any] | None) -> list[str]:
    if not ota_cli_absent(seen):
        return list(groups)
    return [group for group in groups if group not in OTA_CLI_GROUPS]


def group_is_due(
    seen: dict[str, Any] | None,
    group: str,
    *,
    policy: PollPolicy,
    now: int,
) -> bool:
    spec = GET_GROUPS[group]
    if group in OTA_CLI_GROUPS and ota_cli_absent(seen):
        return False
    if group in policy.force_groups:
        return True
    if policy.force and spec.mode != "audit":
        return True
    if spec.mode == "audit":
        if not group_complete(seen, group):
            return True
        stamp = _stamp(seen, spec.pulled_at_key)
        if stamp is None:
            return True
        interval = group_interval(group, policy)
        return interval <= 0 or (now - stamp) >= interval
    if policy.live_only and spec.mode != "periodic":
        return False
    if not group_complete(seen, group):
        return True
    if spec.mode == "inventory":
        return False
    stamp = _stamp(seen, spec.pulled_at_key)
    if stamp is None:
        return True
    interval = group_interval(group, policy)
    if interval <= 0:
        return True
    return (now - stamp) >= interval


def group_interval(group: str, policy: PollPolicy) -> float:
    spec = GET_GROUPS[group]
    if spec.interval is not None:
        return spec.interval
    return policy.min_interval


def due_groups(
    conn: sqlite3.Connection,
    unit: str,
    *,
    policy: PollPolicy,
    now: int | None = None,
) -> list[str]:
    now = now or int(time.time())
    seen = get_last_seen(conn, unit)
    return omit_unsupported_ota(
        [
            group
            for group in GET_GROUP_ORDER
            if group_is_due(seen, group, policy=policy, now=now)
        ],
        seen,
    )


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
    """Split auto-work targets from paused ones. Manual Refresh/Pull/Deploy stays active."""
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
    skip_audit_fresh: dict[float, list[str]] = {}
    skip_fresh: list[str] = []
    skip_nocli: list[str] = []
    for group in GET_GROUP_ORDER:
        if group in due_groups:
            continue
        if group in OTA_CLI_GROUPS and ota_cli_absent(seen):
            skip_nocli.append(group)
            continue
        spec = GET_GROUPS[group]
        if spec.mode == "inventory":
            skip_inv.append(group)
        elif spec.mode == "audit":
            skip_audit_fresh.setdefault(group_interval(group, policy), []).append(group)
        else:
            skip_fresh.append(group)
    skip_bits: list[str] = []
    if skip_inv:
        skip_bits.append(f"{', '.join(skip_inv)} (have)")
    for iv, names in skip_audit_fresh.items():
        skip_bits.append(f"{', '.join(names)} (audit <{format_interval(iv)})")
    if skip_nocli:
        skip_bits.append(f"{', '.join(skip_nocli)} (no CLI)")
    if skip_fresh:
        by_iv: dict[float, list[str]] = {}
        for group in skip_fresh:
            by_iv.setdefault(group_interval(group, policy), []).append(group)
        for iv, names in by_iv.items():
            skip_bits.append(f"{', '.join(names)} (fresh <{format_interval(iv)})")
    skip = "; ".join(skip_bits) if skip_bits else "none"
    return need, skip


def gaps_from_poll(res: PollResult) -> list[str]:
    gaps: list[str] = []
    if "firmware" in res.polled_groups and not res.firmware_version:
        gaps.append("firmware")
    if "bootloader" in res.polled_groups and res.bootloader_version is None:
        gaps.append("bootloader")
    if "ota" in res.polled_groups and res.base_hash is None:
        gaps.append("ota")
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
    if "ota_status" in res.polled_groups and res.ota is None:
        gaps.append("ota_status")
    if "ota_ls" in res.polled_groups and res.ota is None:
        gaps.append("ota_ls")
    return gaps


# Re-export names fleet/tests already know
__all__ = [
    "GET_GROUP_ORDER",
    "GET_GROUPS",
    "MANUAL_JOBS",
    "LIVE_GROUPS",
    "DAILY_GROUPS",
    "OTA_CLI_GROUPS",
    "PERIODIC_GROUPS",
    "PullGroupSpec",
    "ota_cli_absent",
    "omit_unsupported_ota",
    "PollPolicy",
    "PollResult",
    "due_groups",
    "group_interval",
    "group_is_due",
    "manual_job_session_state",
    "pull_due_groups",
    "refresh_due_groups",
    "format_get_plan",
    "format_interval",
    "gaps_from_poll",
    "partition_due",
    "partition_paused",
    "poll_summary",
    "record_poll",
]
