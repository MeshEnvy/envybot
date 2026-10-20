"""Per-target fleet refresh: pull vs apply, job build, row UI meta."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from envybot.apply import apply_due_fields
from envybot.field_state import failure_cap
from envybot.jobs import RadioJob
from envybot.poll import (
    GET_GROUP_ORDER,
    GET_GROUPS,
    PollPolicy,
    full_due_groups,
    group_interval,
    group_is_due,
    lead_neighbors,
    omit_unsupported_ota,
    sync_due_groups,
)

REFRESH_ALL = "*"
REFRESH_SYNC = "__sync__"

RefreshKind = Literal["pull", "apply", "acl_hybrid"]


@dataclass(frozen=True)
class RefreshTargetSpec:
    id: str
    kind: RefreshKind
    pull_groups: tuple[str, ...] = ()
    apply_fields: tuple[str, ...] = ()


# Profile row ids and header chip ids.
REFRESH_TARGETS: dict[str, RefreshTargetSpec] = {
    "firmware": RefreshTargetSpec("firmware", "pull", pull_groups=("firmware",)),
    "bootloader": RefreshTargetSpec("bootloader", "pull", pull_groups=("bootloader",)),
    "ota": RefreshTargetSpec("ota", "pull", pull_groups=("ota",)),
    "ota_status": RefreshTargetSpec("ota_status", "pull", pull_groups=("ota_status",)),
    "ota_ls": RefreshTargetSpec("ota_ls", "pull", pull_groups=("ota_ls",)),
    "status": RefreshTargetSpec("status", "pull", pull_groups=("status",)),
    "telemetry": RefreshTargetSpec("telemetry", "pull", pull_groups=("telemetry",)),
    "neighbors": RefreshTargetSpec("neighbors", "pull", pull_groups=("neighbors",)),
    "name": RefreshTargetSpec("name", "apply", apply_fields=("name",)),
    "lat": RefreshTargetSpec("lat", "apply", apply_fields=("lat", "lon")),
    "lon": RefreshTargetSpec("lon", "apply", apply_fields=("lat", "lon")),
    "owner": RefreshTargetSpec("owner", "apply", apply_fields=("owner",)),
    "admin": RefreshTargetSpec("admin", "apply", apply_fields=("admin",)),
    "guest": RefreshTargetSpec("guest", "apply", apply_fields=("guest",)),
    "acl": RefreshTargetSpec("acl", "acl_hybrid", pull_groups=("acl",), apply_fields=("acl",)),
    "repeat": RefreshTargetSpec("repeat", "apply", apply_fields=("repeat",)),
    "powersaving": RefreshTargetSpec("powersaving", "apply", apply_fields=("powersaving",)),
    "hop_retry": RefreshTargetSpec("hop_retry", "apply", apply_fields=("hop_retry",)),
    "hop_retry_ms": RefreshTargetSpec("hop_retry_ms", "apply", apply_fields=("hop_retry_ms",)),
    "fem_rxgain": RefreshTargetSpec("fem_rxgain", "apply", apply_fields=("fem_rxgain",)),
    "agc_reset_interval": RefreshTargetSpec(
        "agc_reset_interval", "apply", apply_fields=("agc_reset_interval",)
    ),
    "rxgain": RefreshTargetSpec("rxgain", "apply", apply_fields=("rxgain",)),
    "dutycycle": RefreshTargetSpec("dutycycle", "apply", apply_fields=("dutycycle",)),
    "path_hash": RefreshTargetSpec("path_hash", "apply", apply_fields=("path_hash",)),
    "ota_autofetch": RefreshTargetSpec("ota_autofetch", "apply", apply_fields=("ota_autofetch",)),
    "advert": RefreshTargetSpec("advert", "apply", apply_fields=("advert",)),
    "flood": RefreshTargetSpec("flood", "apply", apply_fields=("flood",)),
}

HEADER_REFRESH_IDS = (
    "firmware",
    "bootloader",
    "status",
    "telemetry",
    "neighbors",
    "ota_status",
    "ota_ls",
)


def resolve_refresh_work(
    target_ids: list[str],
    *,
    conn: sqlite3.Connection | None,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    seen: dict[str, Any] | None,
    do_apply: bool,
) -> tuple[list[str], set[str]]:
    """Return ordered pull groups and apply field names to queue."""
    pull: list[str] = []
    apply_fields: set[str] = set()
    pull_set: set[str] = set()
    due_apply: set[str] = set()
    if do_apply and conn is not None:
        due_apply = set(
            apply_due_fields(conn, unit, node, sites, doc=doc, keys=keys)
        )

    ids = [t.strip() for t in target_ids if t and str(t).strip()]
    if REFRESH_ALL in ids:
        pull = lead_neighbors(omit_unsupported_ota(full_due_groups(), seen))
        if do_apply:
            apply_fields = due_apply
        return pull, apply_fields
    if REFRESH_SYNC in ids:
        pull = list(sync_due_groups())
        if do_apply:
            apply_fields = due_apply
        return pull, apply_fields

    for tid in ids:
        spec = REFRESH_TARGETS.get(tid)
        if spec is None:
            continue
        if spec.kind == "acl_hybrid":
            if do_apply and "acl" in due_apply:
                apply_fields.add("acl")
            else:
                for g in spec.pull_groups:
                    if g not in pull_set:
                        pull_set.add(g)
                        pull.append(g)
            continue
        if spec.kind == "apply":
            if do_apply:
                apply_fields.update(spec.apply_fields)
            continue
        for g in spec.pull_groups:
            if g not in pull_set:
                pull_set.add(g)
                pull.append(g)

    ordered_pull = [g for g in GET_GROUP_ORDER if g in pull_set]
    return ordered_pull, apply_fields


def merge_refresh_jobs(existing_kinds: set[str], new_jobs: list[RadioJob]) -> list[RadioJob]:
    """Drop jobs whose kind is already queued (except console)."""
    out: list[RadioJob] = []
    kinds = set(existing_kinds)
    for job in new_jobs:
        if job.kind in kinds:
            continue
        if job.kind == "login" and "login" in kinds:
            continue
        out.append(job)
        kinds.add(job.kind)
    return out


def _job_kinds_for_target(target_id: str) -> set[str]:
    spec = REFRESH_TARGETS.get(target_id)
    if spec is None:
        return set()
    kinds: set[str] = {"login"}
    if spec.kind == "apply":
        for f in spec.apply_fields:
            kinds.add(f"apply:{f}")
        kinds.add("apply:clock")
        kinds.add("apply:push_advert")
        return kinds
    if spec.kind == "acl_hybrid":
        kinds.add("get:acl")
        kinds.add("apply:acl")
        return kinds
    for g in spec.pull_groups:
        if g == "neighbors":
            kinds.update(
                {"get:neighbors_discover", "get:neighbors_wait", "get:neighbors"}
            )
        elif g == "ota_ls":
            kinds.update({"get:ota_ls_probe", "get:ota_ls_wait", "get:ota_ls"})
        else:
            kinds.add(f"get:{g}")
    return kinds


def _running_kind_matches(session: dict[str, Any] | None, target_id: str) -> bool:
    if not session:
        return False
    kind = str(session.get("kind") or "")
    if not kind:
        return False
    return kind in _job_kinds_for_target(target_id)


def _queued_matches(pending_kinds: set[str], target_id: str) -> bool:
    want = _job_kinds_for_target(target_id)
    return bool(want & pending_kinds)


def _pull_stamp_key(group: str) -> str:
    return GET_GROUPS[group].pulled_at_key


def compute_refresh_meta(
    target_id: str,
    *,
    spec: RefreshTargetSpec | None,
    seen: dict[str, Any] | None,
    now: int,
    policy: PollPolicy,
    field_failures: dict[str, int],
    session: dict[str, Any] | None,
    pending_kinds: set[str],
    apply_due_fields_set: set[str],
    max_attempts: int = 10,
    paused: bool = False,
    on_cooldown: bool = False,
) -> dict[str, Any]:
    spec = spec or REFRESH_TARGETS.get(target_id)
    if spec is None:
        return {"phase": "n/a"}

    failure_keys: list[str] = []
    if spec.kind == "apply":
        failure_keys = list(spec.apply_fields)
    elif spec.kind == "acl_hybrid":
        failure_keys = ["acl"]
    elif spec.pull_groups:
        failure_keys = list(spec.pull_groups)

    attempts = 0
    cap = max_attempts
    for key in failure_keys:
        count = field_failures.get(key, 0)
        if count >= attempts:
            attempts = count
        cap = min(cap, failure_cap(key, default_attempts=max_attempts))

    failed = attempts >= cap and cap > 0 and attempts > 0
    running = _running_kind_matches(session, target_id)
    queued = _queued_matches(pending_kinds, target_id)

    meta: dict[str, Any] = {
        "kind": spec.kind,
        "attempts": attempts,
        "cap": cap,
    }

    if paused:
        meta["phase"] = "paused"
        return meta
    if on_cooldown:
        meta["phase"] = "cooldown"
        return meta
    if failed:
        meta["phase"] = "failed"
        return meta
    if running:
        meta["phase"] = "running"
        return meta
    if queued:
        meta["phase"] = "queued"
        return meta

    if spec.kind == "apply":
        due = any(f in apply_due_fields_set for f in spec.apply_fields)
        meta["apply_state"] = "due" if due else "synced"
        meta["phase"] = "idle"
        return meta

    if spec.kind == "acl_hybrid":
        if "acl" in apply_due_fields_set:
            meta["apply_state"] = "due"
            meta["phase"] = "idle"
            return meta
        meta["apply_state"] = "synced"
        group = spec.pull_groups[0] if spec.pull_groups else "acl"
        stamp = seen.get(_pull_stamp_key(group)) if seen else None
        if stamp is None:
            meta["phase"] = "idle"
            meta["next_auto_secs"] = 0
            return meta
        interval = int(group_interval(group, policy))
        next_at = int(stamp) + interval
        meta["next_auto_secs"] = max(0, next_at - now)
        meta["phase"] = "idle"
        return meta

    if spec.pull_groups:
        group = spec.pull_groups[0]
        if group_is_due(seen, group, policy=policy, now=now):
            meta["next_auto_secs"] = 0
        else:
            stamp = seen.get(_pull_stamp_key(group)) if seen else None
            if stamp is None:
                meta["next_auto_secs"] = 0
            else:
                interval = int(group_interval(group, policy))
                next_at = int(stamp) + interval
                meta["next_auto_secs"] = max(0, next_at - now)
        meta["phase"] = "idle"
        return meta

    meta["phase"] = "idle"
    return meta


def pending_job_kinds(jobs: list[RadioJob]) -> set[str]:
    return {j.kind for j in jobs}


def build_refresh_jobs(
    target: Any,
    target_ids: list[str],
    *,
    conn: sqlite3.Connection | None,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    seen: dict[str, Any] | None,
    do_apply: bool,
    do_poll: bool,
    skip_discover: bool,
    discover_wait: float,
    force_apply_pass: bool = False,
) -> list[RadioJob]:
    from envybot.fleet_worker import build_poll_jobs

    pull, apply_set = resolve_refresh_work(
        target_ids,
        conn=conn,
        unit=unit,
        node=node,
        sites=sites,
        doc=doc,
        keys=keys,
        seen=seen,
        do_apply=do_apply,
    )
    if not do_poll:
        pull = []
    if not do_apply:
        apply_set = set()
    if not pull and not apply_set and not (do_apply and force_apply_pass):
        return []
    apply_after = bool(pull) and (bool(apply_set) or force_apply_pass)
    fields_only = frozenset(apply_set) if apply_set else None
    if force_apply_pass and do_apply and not apply_set:
        fields_only = None
    run_apply = bool(apply_set) or (force_apply_pass and do_apply)
    return build_poll_jobs(
        target,
        pull,
        do_apply=run_apply,
        apply_due=run_apply,
        force_apply=False,
        skip_discover=skip_discover,
        discover_wait=discover_wait,
        apply_after_poll=apply_after,
        apply_fields_only=fields_only,
    )
