"""Build and execute one fleet radio job at a time."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Any

from envybot.apply import (
    applicable_field_desireds,
    apply_due_fields,
    apply_is_due,
    clear_apply_stamps,
    desired_dutycycle,
    desired_ota_autofetch,
    desired_path_hash_mode,
    profile_id,
    radio_apply_due_fields,
)
from envybot.apply import (
    SetSend,
    _apply_acl,
    _ensure_guest_password,
    _set_cli,
)
from envybot.history import get_last_seen, insert_command, record_poll, stamp_apply
from envybot.jobs import INVENTORY_GROUPS, JobOutcome, RadioJob, UnitQueue
from envybot.keys_doc import UnknownPerson, parse_serial_acl, resolve_node_acl
from envybot.nodes_doc import MASK_NAME, is_public
from envybot.passwords import normalize_password, password_is_strong
from envybot.poll import GET_GROUP_ORDER, PollPolicy, pull_due_groups, refresh_due_groups
from envybot.position import public_radio_name, resolve_book_position, site_loc_for_unit
from envybot.sun import stamp_sun
from envybot.ota_parse import (
    merge_ota_snapshot,
    ota_ls_heard_empty,
    ota_status_heard_empty,
    parse_ota_ls,
    parse_ota_stats,
    parse_ota_status,
)
from envybot.radio import (
    FleetSession,
    NEIGHBOR_DISCOVER_WAIT_S,
    OTA_LS_WAIT_S,
    POST_INSTALL_WAIT_S,
    PollLog,
    PollResult,
    RouterTarget,
    admin_login_attempt,
    binary_req_once,
    cli_error_reply,
    cli_suggests_auth_failure,
    mesh_wait_seconds,
    maybe_sync_repeater_clock,
    normalize_acl_payload,
    normalize_neighbors_payload,
    parse_bootloader,
    parse_coord,
    parse_firmware,
    parse_get_value,
    parse_int_get_value,
    parse_ota_self,
    ota_self_heard_empty,
    pull_repeater_status,
    send_cmd_once,
    set_book_coord,
    set_dutycycle_policy,
    OtaAutofetchUnsupported,
    set_ota_autofetch_policy,
    set_path_hash_policy,
    trigger_neighbor_discover_once,
)

try:
    from meshcore import MeshCore
except ImportError:  # pragma: no cover
    MeshCore = Any  # type: ignore[misc,assignment]


APPLY_FIELD_ORDER = (
    "name",
    "lat",
    "lon",
    "advert",
    "flood",
    "guest",
    "admin",
    "path_hash",
    "dutycycle",
    "ota_autofetch",
    "acl",
)


@dataclass
class WorkerContext:
    client: MeshCore
    conn: sqlite3.Connection
    nodes: dict[str, Any]
    sites: dict[str, dict[str, Any]]
    doc: dict[str, Any]
    keys: dict[str, list[str]]
    session: FleetSession
    log: PollLog
    cmd_timeout: float
    login_timeout: float
    discover_wait: float
    skip_discover: bool
    do_poll: bool
    do_apply: bool
    max_attempts: int = 10


@dataclass
class PollAccumulator:
    polled_groups: set[str] = field(default_factory=set)
    stat_errors: list[str] = field(default_factory=list)
    node_clock: int | None = None
    status: dict[str, Any] | None = None
    name: str | None = None
    telemetry: list[Any] | None = None
    fw: str | None = None
    platform: str | None = None
    bl: str | None = None
    base_hash: str | None = None
    raw_ver: str | None = None
    raw_bl: str | None = None
    lat: float | None = None
    lon: float | None = None
    advert_min: int | None = None
    flood_h: int | None = None
    acl: list[dict[str, Any]] | None = None
    neighbors: list[dict[str, Any]] | None = None
    heard_acl: list[dict[str, Any]] | None = None
    ota: dict[str, Any] | None = None
    ota_partial: dict[str, Any] = field(default_factory=dict)

    def to_result(self, key: str, *, ok: bool = True, error: str | None = None) -> PollResult:
        position = None
        if self.lat is not None or self.lon is not None:
            position = {
                "lat": self.lat if self.lat is not None else 0.0,
                "lon": self.lon if self.lon is not None else 0.0,
            }
        return PollResult(
            key,
            ok=ok,
            error=error,
            firmware_version=self.fw,
            bootloader_version=self.bl,
            base_hash=self.base_hash,
            firmware_platform=self.platform,
            raw_ver=self.raw_ver,
            raw_bl=self.raw_bl,
            node_clock=self.node_clock,
            status=self.status,
            name=self.name,
            lat=self.lat,
            lon=self.lon,
            position=position,
            telemetry=self.telemetry,
            advert_interval_min=self.advert_min,
            flood_advert_interval_h=self.flood_h,
            acl=self.acl,
            neighbors=self.neighbors,
            ota=self.ota,
            polled_groups=frozenset(self.polled_groups),
            stat_errors=list(self.stat_errors),
        )

    def record_group(self, ctx: WorkerContext, unit: str, group: str) -> None:
        self.polled_groups.add(group)
        # Persist only this group. The accumulator is cumulative; replaying
        # earlier groups would insert duplicate status/telemetry with a new ts
        # and fake +0 packet deltas.
        res = replace(self.to_result(unit), polled_groups=frozenset({group}))
        record_poll(
            ctx.conn,
            unit=unit,
            res=res,
            site_loc=site_loc_for_unit(unit, ctx.nodes.get(unit), ctx.sites),
        )


def _attempt_cap(ctx: WorkerContext) -> int | None:
    return ctx.max_attempts if ctx.max_attempts else None


def _sample_site_sun(
    sample: dict[str, Any], site_loc: tuple[float, float] | None
) -> None:
    if not site_loc:
        return
    sample["lat"] = site_loc[0]
    sample["lon"] = site_loc[1]
    stamp_sun(sample)


def job_sample(
    job: RadioJob,
    uq: UnitQueue,
    outcome: JobOutcome,
    payload: Any | None,
    *,
    site_loc: tuple[float, float] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Build SSE sample for a successful GET group."""
    if outcome not in (JobOutcome.HEARD, JobOutcome.TIMER_DONE):
        return None
    if job.kind == "get:neighbors_wait":
        return None
    ts = int(time.time())
    acc = _poll_acc(uq)

    if job.kind == "login":
        return None
    effective_kind = job.kind
    if job.kind == "console:cli":
        captured = job.extra.get("captured_group")
        if not captured:
            return None
        effective_kind = f"get:{captured}"
    if effective_kind == "get:status" and acc.status:
        st = acc.status
        battery = st.get("battery_mv")
        sample = {
            "ts": ts,
            "battery_mv": battery,
            "voltage": round(battery / 1000.0, 3) if battery is not None else None,
            "packets_recv": st.get("packets_recv"),
            "packets_sent": st.get("packets_sent"),
            "uptime_secs": st.get("uptime_secs"),
            "noise_floor": st.get("noise_floor"),
        }
        _sample_site_sun(sample, site_loc)
        return ("status", sample)
    if effective_kind == "get:telemetry" and acc.telemetry:
        volt = None
        temp = None
        for item in acc.telemetry:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "voltage" and item.get("value") is not None:
                volt = float(item["value"])
            if item.get("type") == "temperature" and item.get("value") is not None:
                temp = float(item["value"])
        sample = {"ts": ts, "voltage": volt, "temperature": temp}
        _sample_site_sun(sample, site_loc)
        return ("telemetry", sample)
    if effective_kind == "get:neighbors" and acc.neighbors is not None:
        return (
            "neighbors",
            {"ts": ts, "count": len(acc.neighbors), "neighbors": acc.neighbors},
        )
    if effective_kind == "get:acl" and acc.acl is not None:
        return ("acl", {"ts": ts, "count": len(acc.acl), "acl": acc.acl})
    if effective_kind == "get:ota_status" and acc.ota is not None:
        return ("ota", {"ts": ts, "ota": acc.ota})
    if effective_kind == "get:ota_ls" and acc.ota is not None:
        heard = acc.ota.get("heard") if isinstance(acc.ota, dict) else None
        return ("ota", {"ts": ts, "ota": acc.ota, "heard_count": len(heard or [])})
    return None


def _poll_acc(uq: UnitQueue) -> PollAccumulator:
    acc = uq.session_extra.get("poll_acc")
    if acc is None:
        acc = PollAccumulator()
        uq.session_extra["poll_acc"] = acc
    return acc


def _needs_login(uq: UnitQueue, ctx: WorkerContext) -> bool:
    if not ctx.session.is_authed(uq.target.key):
        return True
    return False


def build_poll_jobs(
    target: RouterTarget,
    due_groups: list[str],
    *,
    do_apply: bool,
    apply_due: bool,
    force_apply: bool,
    skip_discover: bool,
    discover_wait: float = NEIGHBOR_DISCOVER_WAIT_S,
) -> list[RadioJob]:
    jobs: list[RadioJob] = []
    jobs.append(RadioJob(kind="login", unit_key=target.key))
    if do_apply and apply_due:
        # SET before GET so a long/retrying poll cannot starve profile apply.
        jobs.extend(build_apply_jobs(target.key, force=force_apply))
    for group in GET_GROUP_ORDER:
        if group not in due_groups:
            continue
        if group == "neighbors":
            if not skip_discover:
                jobs.append(RadioJob(kind="get:neighbors_discover", unit_key=target.key))
            jobs.append(
                RadioJob(
                    kind="get:neighbors_wait",
                    unit_key=target.key,
                    extra={"wait_s": discover_wait},
                )
            )
            jobs.append(RadioJob(kind="get:neighbors", unit_key=target.key))
        elif group == "ota_ls":
            jobs.append(RadioJob(kind="get:ota_ls_probe", unit_key=target.key))
            jobs.append(
                RadioJob(
                    kind="get:ota_ls_wait",
                    unit_key=target.key,
                    extra={"wait_s": OTA_LS_WAIT_S},
                )
            )
            jobs.append(RadioJob(kind="get:ota_ls", unit_key=target.key))
        else:
            jobs.append(RadioJob(kind=f"get:{group}", unit_key=target.key))
    return jobs


def build_apply_jobs(unit_key: str, *, force: bool) -> list[RadioJob]:
    jobs: list[RadioJob] = []
    if force:
        jobs.append(RadioJob(kind="apply:force_clear", unit_key=unit_key))
    for field in APPLY_FIELD_ORDER:
        jobs.append(RadioJob(kind=f"apply:{field}", unit_key=unit_key))
    jobs.append(RadioJob(kind="apply:clock", unit_key=unit_key))
    return jobs


def jobs_include_apply(jobs: Any) -> bool:
    return any(j.kind.startswith("apply:") for j in jobs)


def insert_apply_jobs(uq: UnitQueue, apply_jobs: list[RadioJob]) -> bool:
    """Queue SET after the in-flight head. No-op if apply is already queued."""
    if not apply_jobs or jobs_include_apply(uq.jobs):
        return False
    items = list(uq.jobs)
    if items:
        items[1:1] = apply_jobs
    else:
        items = list(apply_jobs)
    uq.jobs.clear()
    uq.jobs.extend(items)
    return True


def build_manual_jobs(
    target: RouterTarget,
    manual_job: str,
    *,
    do_poll: bool,
    do_apply: bool,
    apply_due: bool,
    skip_discover: bool,
    discover_wait: float = NEIGHBOR_DISCOVER_WAIT_S,
    stage_mid: str | None = None,
) -> list[RadioJob]:
    if manual_job == "stage":
        if not stage_mid:
            return []
        return build_stage_jobs(target, stage_mid)
    if manual_job == "install":
        return build_install_jobs(target)
    if manual_job == "push":
        return build_poll_jobs(
            target,
            [],
            do_apply=do_apply,
            apply_due=True,
            force_apply=True,
            skip_discover=skip_discover,
            discover_wait=discover_wait,
        )
    due: list[str] = []
    if do_poll:
        if manual_job == "pull":
            due = pull_due_groups()
        elif manual_job == "refresh":
            due = refresh_due_groups()
    return build_poll_jobs(
        target,
        due,
        do_apply=do_apply,
        apply_due=apply_due or manual_job == "push",
        force_apply=manual_job == "push",
        skip_discover=skip_discover,
        discover_wait=discover_wait,
    )


def seed_auto_unit(
    sched_units: dict[str, UnitQueue],
    target: RouterTarget,
    *,
    due_groups: list[str],
    apply_due: bool,
    ctx: WorkerContext,
) -> None:
    uq = sched_units.get(target.key)
    if uq is None:
        return
    if not due_groups and not apply_due:
        return
    jobs = build_poll_jobs(
        target,
        due_groups,
        do_apply=ctx.do_apply,
        apply_due=apply_due,
        force_apply=False,
        skip_discover=ctx.skip_discover,
    )
    uq.jobs.extend(jobs)
    uq.has_inventory_gap = any(
        g in INVENTORY_GROUPS for g in due_groups
    )


def build_stage_jobs(target: RouterTarget, mid: str) -> list[RadioJob]:
    """Manual stage: `ota pull <mid> flash`, wait, refresh status."""
    mid = mid.strip().lower()
    jobs: list[RadioJob] = [
        RadioJob(kind="login", unit_key=target.key),
        RadioJob(kind="cmd:ota_pull", unit_key=target.key, extra={"mid": mid}),
        RadioJob(
            kind="get:ota_ls_wait",
            unit_key=target.key,
            extra={"wait_s": OTA_LS_WAIT_S},
        ),
        RadioJob(kind="get:ota_status", unit_key=target.key),
    ]
    return jobs


def build_install_jobs(target: RouterTarget) -> list[RadioJob]:
    """Manual install: `ota install`, wait for reboot, liveness GET."""
    return [
        RadioJob(kind="login", unit_key=target.key),
        RadioJob(kind="cmd:ota_install", unit_key=target.key),
        RadioJob(
            kind="get:post_install_wait",
            unit_key=target.key,
            extra={"wait_s": POST_INSTALL_WAIT_S},
        ),
        RadioJob(kind="get:firmware", unit_key=target.key),
        RadioJob(kind="get:ota_status", unit_key=target.key),
    ]


def _console_insert_command(
    ctx: WorkerContext,
    target: RouterTarget,
    cmd: str,
    reply: str | None,
    *,
    ok: bool,
) -> None:
    from envybot.commands.cmd import redact_snippet

    insert_command(
        ctx.conn,
        unit=target.key,
        argv=redact_snippet(cmd, max_len=500) or cmd,
        reply=redact_snippet(reply),
        ok=ok,
        source="console",
    )


# Console CLI that matches a fleet GET group → stamp sqlite like a poll.
_CONSOLE_POLL_CMDS: dict[str, str] = {
    "ota status": "ota_status",
    "ota st": "ota_status",
    "ota stats": "ota_status",
    "ota ls": "ota_ls",
    "ota n": "ota_ls",
    "ota neighbors": "ota_ls",
    "ota nbrs": "ota_ls",
    "ota updates": "ota_ls",
    "ota self": "ota",
    "ver": "firmware",
    "version": "firmware",
    "get bootloader.ver": "bootloader",
    "bootloader.ver": "bootloader",
    "get name": "name",
    "get lat": "lat",
    "get lon": "lon",
    "get advert.interval": "advert",
    "get flood.advert.interval": "flood_advert",
    "get acl": "acl",
}


def _console_poll_group(cmd: str) -> str | None:
    key = " ".join(cmd.lower().split())
    hit = _CONSOLE_POLL_CMDS.get(key)
    if hit:
        return hit
    for prefix, group in (
        ("ota ls ", "ota_ls"),
        ("ota n ", "ota_ls"),
        ("ota neighbors ", "ota_ls"),
    ):
        if key.startswith(prefix):
            return group
    return None


def _apply_cli_poll_reply(
    group: str,
    raw: str,
    *,
    acc: PollAccumulator,
    ctx: WorkerContext,
    target: RouterTarget,
) -> bool:
    """Parse a CLI GET reply and record_poll. True if stamped."""
    if group == "ota_status":
        if not ota_status_heard_empty(raw) and cli_error_reply(raw):
            return False
        parsed = parse_ota_stats(raw) or parse_ota_status(raw)
        if parsed is None:
            return False
        acc.ota_partial = merge_ota_snapshot(
            acc.ota_partial, status=parsed, raw_status=raw.strip()
        )
        acc.ota = acc.ota_partial
        acc.record_group(ctx, target.key, "ota_status")
        return True
    if group == "ota_ls":
        if not ota_ls_heard_empty(raw) and cli_error_reply(raw):
            return False
        heard = parse_ota_ls(raw)
        if heard is None:
            return False
        acc.ota_partial = merge_ota_snapshot(
            acc.ota_partial, heard=heard, raw_ls=raw.strip()
        )
        acc.ota = acc.ota_partial
        acc.record_group(ctx, target.key, "ota_ls")
        return True
    if group == "ota":
        if not ota_self_heard_empty(raw) and cli_error_reply(raw):
            return False
        parsed_ota = parse_ota_self(raw)
        if parsed_ota is None:
            return False
        acc.base_hash = parsed_ota
        acc.record_group(ctx, target.key, "ota")
        return True
    if cli_error_reply(raw) or raw.strip().upper().startswith("ERR"):
        return False
    if group == "firmware":
        fw, platform = parse_firmware(raw)
        if not fw:
            return False
        acc.fw = fw
        acc.platform = platform
        acc.raw_ver = raw
        acc.record_group(ctx, target.key, "firmware")
        return True
    if group == "bootloader":
        bl = parse_bootloader(raw) or ""
        acc.bl = bl
        acc.raw_bl = raw
        acc.record_group(ctx, target.key, "bootloader")
        return True
    if group == "name":
        acc.name = parse_get_value(raw) or ""
        acc.record_group(ctx, target.key, "name")
        return True
    if group == "lat":
        acc.lat = parse_coord(raw)
        if acc.lat is None:
            acc.lat = 0.0
        acc.record_group(ctx, target.key, "lat")
        return True
    if group == "lon":
        acc.lon = parse_coord(raw)
        if acc.lon is None:
            acc.lon = 0.0
        acc.record_group(ctx, target.key, "lon")
        return True
    if group == "advert":
        acc.advert_min = parse_int_get_value(raw) or 0
        acc.record_group(ctx, target.key, "advert")
        return True
    if group == "flood_advert":
        acc.flood_h = parse_int_get_value(raw) or 0
        acc.record_group(ctx, target.key, "flood_advert")
        return True
    if group == "acl":
        acl = parse_serial_acl(raw)
        acc.acl = acl
        acc.heard_acl = acl
        acc.record_group(ctx, target.key, "acl")
        return True
    return False


def _console_capture_tracked(
    ctx: WorkerContext,
    uq: UnitQueue,
    target: RouterTarget,
    cmd: str,
    raw: str,
) -> str | None:
    """If cmd is a tracked poll CLI, stamp history. Returns group or None."""
    group = _console_poll_group(cmd)
    if not group:
        return None
    acc = _poll_acc(uq)
    if _apply_cli_poll_reply(group, raw, acc=acc, ctx=ctx, target=target):
        ctx.log.step(f"console captured {group}")
        return group
    return None


async def _execute_console_cli(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
    attempt_num: int,
    attempt_cap: int | None,
) -> tuple[JobOutcome, Any | None]:
    target = uq.target
    cmd = str(job.extra.get("cmd") or "").strip()
    if not cmd:
        return JobOutcome.HARD_FAIL, "empty command"
    cancel_gen = int(job.extra.get("cancel_gen") or 0)
    tab_id = str(job.extra.get("tab_id") or "")
    if not ctx.session.is_authed(target.key):
        if not uq.jobs or uq.jobs[0].kind != "console:login":
            uq.jobs.insert(
                0,
                RadioJob(
                    kind="console:login",
                    unit_key=target.key,
                    manual=True,
                    manual_job="console",
                    extra={"cancel_gen": cancel_gen, "tab_id": tab_id},
                ),
            )
        return JobOutcome.TIMEOUT, "not authed"
    manager = uq.session_extra.get("console_manager")

    def cancel_check() -> bool:
        if manager is None or not tab_id:
            return False
        return bool(manager.cancel_check(tab_id, cancel_gen))

    cancelled: list[bool] = []
    raw = await send_cmd_once(
        ctx.client,
        target,
        cmd,
        timeout=ctx.cmd_timeout,
        session=ctx.session,
        log=ctx.log,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
        cancel_check=cancel_check,
        cancelled_out=cancelled,
    )
    if cancelled:
        return JobOutcome.CANCELLED, "cancelled"
    if raw is None:
        _console_insert_command(ctx, target, cmd, "command timeout", ok=False)
        return JobOutcome.TIMEOUT, "command timeout"
    if cli_suggests_auth_failure(raw):
        ctx.session.clear_auth(target.key)
        if not uq.jobs or uq.jobs[0].kind != "console:login":
            uq.jobs.insert(
                0,
                RadioJob(
                    kind="console:login",
                    unit_key=target.key,
                    manual=True,
                    manual_job="console",
                    extra={"cancel_gen": cancel_gen, "tab_id": tab_id},
                ),
            )
        return JobOutcome.TIMEOUT, "auth"
    ok = not (cli_error_reply(raw) or raw.strip().upper().startswith("ERR"))
    _console_insert_command(ctx, target, cmd, raw, ok=ok)
    if ok:
        captured = _console_capture_tracked(ctx, uq, target, cmd, raw)
        if captured:
            job.extra["captured_group"] = captured
    if not ok:
        return JobOutcome.HEARD, raw
    return JobOutcome.HEARD, raw


async def execute_job(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
) -> tuple[JobOutcome, Any | None]:
    target = uq.target
    attempt_num = job.attempt + 1
    attempt_cap = _attempt_cap(ctx)
    acc = _poll_acc(uq)
    node = ctx.nodes.get(target.key) or {}

    if job.kind == "login":
        ok, err, clock = await admin_login_attempt(
            ctx.client,
            target,
            login_timeout=ctx.login_timeout,
            session=ctx.session,
            log=ctx.log,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if ok:
            acc.node_clock = clock
            uq.session_extra["login_clock"] = clock
            return JobOutcome.HEARD, clock
        if err and "rejected" in str(err).lower():
            return JobOutcome.HARD_FAIL, err
        return JobOutcome.TIMEOUT, err

    if job.kind == "console:login":
        cancel_gen = int(job.extra.get("cancel_gen") or 0)
        tab_id = str(job.extra.get("tab_id") or "")
        manager = uq.session_extra.get("console_manager")

        def cancel_check() -> bool:
            if manager is None or not tab_id:
                return False
            return bool(manager.cancel_check(tab_id, cancel_gen))

        ok, err, clock = await admin_login_attempt(
            ctx.client,
            target,
            login_timeout=ctx.login_timeout,
            session=ctx.session,
            log=ctx.log,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
            cancel_check=cancel_check,
        )
        if ok:
            acc.node_clock = clock
            uq.session_extra["login_clock"] = clock
            return JobOutcome.HEARD, clock
        if err and "cancelled" in str(err).lower():
            return JobOutcome.CANCELLED, err
        if err and "rejected" in str(err).lower():
            return JobOutcome.HARD_FAIL, err
        return JobOutcome.TIMEOUT, err

    if job.kind == "console:cli":
        return await _execute_console_cli(job, uq, ctx, attempt_num, attempt_cap)

    if job.kind == "get:neighbors_wait":
        wait_s = float(job.extra.get("wait_s", ctx.discover_wait))
        ctx.log.step(f"discover wait {wait_s:g}s")
        await asyncio.sleep(wait_s)
        return JobOutcome.TIMER_DONE, None

    if job.kind == "get:ota_ls_wait":
        wait_s = float(job.extra.get("wait_s", OTA_LS_WAIT_S))
        ctx.log.step(f"ota ls wait {wait_s:g}s")
        await asyncio.sleep(wait_s)
        return JobOutcome.TIMER_DONE, None

    if job.kind == "get:post_install_wait":
        wait_s = float(job.extra.get("wait_s", POST_INSTALL_WAIT_S))
        ctx.log.step(f"post-install wait {wait_s:g}s")
        await asyncio.sleep(wait_s)
        return JobOutcome.TIMER_DONE, None

    if not ctx.session.is_authed(target.key):
        return JobOutcome.HARD_FAIL, "not authed"

    if job.kind == "get:neighbors_discover":
        ok = await trigger_neighbor_discover_once(
            ctx.client,
            target,
            cmd_timeout=ctx.cmd_timeout,
            session=ctx.session,
            log=ctx.log,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if ok:
            return JobOutcome.HEARD, True
        return JobOutcome.TIMEOUT, None

    if job.kind == "get:status":
        status = await pull_repeater_status_once(
            ctx, target, attempt_num=attempt_num, attempt_cap=attempt_cap
        )
        if status:
            acc.status = status
            acc.record_group(ctx, target.key, "status")
            return JobOutcome.HEARD, status
        return JobOutcome.TIMEOUT, None

    if job.kind == "get:telemetry":
        wait_cap = mesh_wait_seconds(6000, cap=ctx.cmd_timeout)
        telem = await binary_req_once(
            "GET_TELEMETRY",
            lambda dest_wait: ctx.client.commands.req_telemetry_sync(
                target.pubkey_hex, timeout=dest_wait, min_timeout=8
            ),
            client=ctx.client,
            target=target,
            log=ctx.log,
            session=ctx.session,
            wait_s=wait_cap,
            cap=ctx.cmd_timeout,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if telem is not None:
            acc.telemetry = telem if isinstance(telem, list) else None
            acc.record_group(ctx, target.key, "telemetry")
            return JobOutcome.HEARD, telem
        return JobOutcome.TIMEOUT, None

    if job.kind == "get:acl":
        wait_cap = mesh_wait_seconds(6000, cap=ctx.cmd_timeout)
        acl_raw = await binary_req_once(
            "GET_ACL",
            lambda dest_wait: ctx.client.commands.req_acl_sync(
                target.pubkey_hex, timeout=dest_wait, min_timeout=8
            ),
            client=ctx.client,
            target=target,
            log=ctx.log,
            session=ctx.session,
            wait_s=wait_cap,
            cap=ctx.cmd_timeout,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        acl = normalize_acl_payload(acl_raw)
        if acl is not None:
            acc.acl = acl
            acc.heard_acl = acl
            uq.session_extra["heard_acl"] = acl
            acc.record_group(ctx, target.key, "acl")
            return JobOutcome.HEARD, acl
        return JobOutcome.TIMEOUT, None

    if job.kind == "get:neighbors":
        wait_cap = mesh_wait_seconds(6000, cap=ctx.cmd_timeout)
        neigh_raw = await binary_req_once(
            "GET_NEIGHBOURS",
            lambda dest_wait: ctx.client.commands.fetch_all_neighbours(
                target.pubkey_hex, timeout=dest_wait, min_timeout=8
            ),
            client=ctx.client,
            target=target,
            log=ctx.log,
            session=ctx.session,
            wait_s=wait_cap,
            cap=ctx.cmd_timeout,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        neighbors = normalize_neighbors_payload(neigh_raw)
        if neighbors is not None:
            acc.neighbors = neighbors
            acc.record_group(ctx, target.key, "neighbors")
            return JobOutcome.HEARD, neighbors
        return JobOutcome.TIMEOUT, None

    if job.kind == "get:ota_status":
        return await _execute_ota_status(job, uq, ctx, attempt_num, attempt_cap)

    if job.kind in ("get:ota_ls_probe", "get:ota_ls"):
        return await _execute_ota_ls(job, uq, ctx, attempt_num, attempt_cap)

    if job.kind == "cmd:ota_pull":
        selector = str(job.extra.get("mid") or job.extra.get("selector") or "").strip()
        if not selector:
            return JobOutcome.HARD_FAIL, "missing stage selector"
        raw = await send_cmd_once(
            ctx.client,
            target,
            f"ota pull {selector} flash",
            timeout=ctx.cmd_timeout,
            session=ctx.session,
            log=ctx.log,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if raw is None:
            return JobOutcome.TIMEOUT, None
        if cli_suggests_auth_failure(raw):
            ctx.session.clear_auth(target.key)
            return JobOutcome.HARD_FAIL, raw
        upper = raw.strip().upper()
        if upper.startswith("ERR") or cli_error_reply(raw):
            return JobOutcome.HARD_FAIL, raw
        ctx.log.step(f"ota pull {selector} flash: {raw.strip()[:80]}")
        return JobOutcome.HEARD, raw

    if job.kind == "cmd:ota_install":
        raw = await send_cmd_once(
            ctx.client,
            target,
            "ota install",
            timeout=ctx.cmd_timeout,
            session=ctx.session,
            log=ctx.log,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if raw is None:
            return JobOutcome.TIMEOUT, None
        if cli_suggests_auth_failure(raw):
            ctx.session.clear_auth(target.key)
            return JobOutcome.HARD_FAIL, raw
        upper = raw.strip().upper()
        if upper.startswith("ERR") or cli_error_reply(raw):
            return JobOutcome.HARD_FAIL, raw
        ctx.log.step(f"ota install: {raw.strip()[:80]}")
        return JobOutcome.HEARD, raw

    if job.kind.startswith("get:"):
        group = job.kind.split(":", 1)[1]
        if group in ("ota_ls_probe", "ota_ls_wait", "post_install_wait"):
            return JobOutcome.HARD_FAIL, f"unhandled timer job {job.kind}"
        return await _execute_get_cli(job, uq, ctx, group, attempt_num, attempt_cap)

    if job.kind.startswith("apply:"):
        return await _execute_apply(job, uq, ctx, node, attempt_num)

    return JobOutcome.HARD_FAIL, f"unknown job {job.kind}"


async def pull_repeater_status_once(
    ctx: WorkerContext,
    target: RouterTarget,
    *,
    attempt_num: int,
    attempt_cap: int | None = None,
) -> dict[str, Any] | None:
    wait_cap = mesh_wait_seconds(6000, cap=ctx.cmd_timeout)

    raw = await binary_req_once(
        "GET_STATUS",
        lambda dest_wait: ctx.client.commands.req_status_sync(
            target.pubkey_hex, timeout=dest_wait, min_timeout=8
        ),
        client=ctx.client,
        target=target,
        log=ctx.log,
        session=ctx.session,
        wait_s=wait_cap,
        cap=ctx.cmd_timeout,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    from envybot.radio import normalize_status_payload

    return normalize_status_payload(raw)


async def _execute_ota_status(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
    attempt_num: int,
    attempt_cap: int | None = None,
) -> tuple[JobOutcome, Any | None]:
    target = uq.target
    acc = _poll_acc(uq)
    raw = await send_cmd_once(
        ctx.client,
        target,
        "ota status",
        timeout=ctx.cmd_timeout,
        session=ctx.session,
        log=ctx.log,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        return JobOutcome.TIMEOUT, None
    if cli_suggests_auth_failure(raw):
        ctx.session.clear_auth(target.key)
        return JobOutcome.HARD_FAIL, raw
    if not _apply_cli_poll_reply("ota_status", raw, acc=acc, ctx=ctx, target=target):
        if not ota_status_heard_empty(raw) and cli_error_reply(raw):
            return JobOutcome.HARD_FAIL, raw
        return JobOutcome.TIMEOUT, None
    return JobOutcome.HEARD, acc.ota


async def _execute_ota_ls(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
    attempt_num: int,
    attempt_cap: int | None = None,
) -> tuple[JobOutcome, Any | None]:
    target = uq.target
    acc = _poll_acc(uq)
    raw = await send_cmd_once(
        ctx.client,
        target,
        "ota ls",
        timeout=ctx.cmd_timeout,
        session=ctx.session,
        log=ctx.log,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        return JobOutcome.TIMEOUT, None
    if cli_suggests_auth_failure(raw):
        ctx.session.clear_auth(target.key)
        return JobOutcome.HARD_FAIL, raw
    if job.kind == "get:ota_ls_probe":
        ctx.log.step("ota ls probe (catalog query started)")
        return JobOutcome.HEARD, raw
    if not _apply_cli_poll_reply("ota_ls", raw, acc=acc, ctx=ctx, target=target):
        if not ota_ls_heard_empty(raw) and cli_error_reply(raw):
            return JobOutcome.HARD_FAIL, raw
        return JobOutcome.TIMEOUT, None
    return JobOutcome.HEARD, (acc.ota or {}).get("heard")


async def _execute_get_cli(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
    group: str,
    attempt_num: int,
    attempt_cap: int | None = None,
) -> tuple[JobOutcome, Any | None]:
    target = uq.target
    acc = _poll_acc(uq)
    cmd_map = {
        "firmware": "ver",
        "bootloader": "get bootloader.ver",
        "ota": "ota self",
        "name": "get name",
        "lat": "get lat",
        "lon": "get lon",
        "advert": "get advert.interval",
        "flood_advert": "get flood.advert.interval",
    }
    cmd = cmd_map.get(group)
    if not cmd:
        return JobOutcome.HARD_FAIL, f"unknown get group {group}"

    raw = await send_cmd_once(
        ctx.client,
        target,
        cmd,
        timeout=ctx.cmd_timeout,
        session=ctx.session,
        log=ctx.log,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        return JobOutcome.TIMEOUT, None
    if cli_suggests_auth_failure(raw):
        ctx.session.clear_auth(target.key)
        return JobOutcome.HARD_FAIL, raw
    if group == "ota":
        if not _apply_cli_poll_reply("ota", raw, acc=acc, ctx=ctx, target=target):
            if not ota_self_heard_empty(raw) and cli_error_reply(raw):
                return JobOutcome.HARD_FAIL, raw
            return JobOutcome.TIMEOUT, None
        if acc.base_hash:
            ctx.log.step(f"ota base_hash={acc.base_hash}")
        else:
            ctx.log.step("ota self: empty (no OTA or no EndF)")
        return JobOutcome.HEARD, acc.base_hash
    if cli_error_reply(raw):
        return JobOutcome.HARD_FAIL, raw
    if not _apply_cli_poll_reply(group, raw, acc=acc, ctx=ctx, target=target):
        return JobOutcome.TIMEOUT, None
    if group == "firmware":
        return JobOutcome.HEARD, acc.fw
    if group == "bootloader":
        return JobOutcome.HEARD, acc.bl
    if group == "name":
        return JobOutcome.HEARD, acc.name
    if group == "lat":
        return JobOutcome.HEARD, acc.lat
    if group == "lon":
        return JobOutcome.HEARD, acc.lon
    if group == "advert":
        return JobOutcome.HEARD, acc.advert_min
    if group == "flood_advert":
        return JobOutcome.HEARD, acc.flood_h
    return JobOutcome.TIMEOUT, None


async def _execute_apply(
    job: RadioJob,
    uq: UnitQueue,
    ctx: WorkerContext,
    node: dict[str, Any],
    attempt_num: int,
) -> tuple[JobOutcome, Any | None]:
    if uq.apply_aborted:
        return JobOutcome.HEARD, "skipped"

    target = uq.target
    field = job.kind.split(":", 1)[1]
    force = bool(uq.session_extra.get("force_apply"))
    applicable = applicable_field_desireds(
        node, ctx.sites, doc=ctx.doc, keys=ctx.keys, key=target.key
    )

    def stamp(f: str) -> None:
        des = applicable.get(f)
        if des is not None:
            stamp_apply(ctx.conn, unit=target.key, field=f, desired=des, ok=True)

    def due_fields() -> frozenset[str]:
        return frozenset(
            apply_due_fields(
                ctx.conn, target.key, node, ctx.sites, force=force, doc=ctx.doc, keys=ctx.keys
            )
        )

    if field == "force_clear":
        if force:
            clear_apply_stamps(ctx.conn, target.key)
        return JobOutcome.HEARD, None

    due = due_fields()
    if field not in due and field != "clock":
        return JobOutcome.HEARD, "skip"

    if field == "clock":
        login_clock = uq.session_extra.get("login_clock")
        seen = get_last_seen(ctx.conn, target.key)
        stored = int(seen["node_clock"]) if seen and seen.get("node_clock") is not None else None
        clock = await maybe_sync_repeater_clock(
            ctx.client,
            target,
            login_clock=login_clock,
            stored_clock=stored,
            cmd_timeout=ctx.cmd_timeout,
            attempts=1,
            log=ctx.log,
            session=ctx.session,
        )
        return JobOutcome.HEARD, clock

    public = is_public(node)
    attempt_cap = _attempt_cap(ctx)
    send: SetSend = "timeout"

    if field == "name":
        name = public_radio_name(target.key, node, ctx.sites) if public else MASK_NAME
        send = await _set_cli(
            ctx.client, target, f"set name {name}",
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            field="name", attempt_num=attempt_num, attempt_cap=attempt_cap,
        )
    elif field == "lat":
        val = float(resolve_book_position(node, ctx.sites, key=target.key)["lat"]) if public and resolve_book_position(node, ctx.sites, key=target.key) else 0.0
        send = "ok" if await set_book_coord(
            ctx.client, target, "lat", val,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            attempt_num=attempt_num, attempt_cap=attempt_cap,
        ) is not None else "timeout"
    elif field == "lon":
        val = float(resolve_book_position(node, ctx.sites, key=target.key)["lon"]) if public and resolve_book_position(node, ctx.sites, key=target.key) else 0.0
        send = "ok" if await set_book_coord(
            ctx.client, target, "lon", val,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            attempt_num=attempt_num, attempt_cap=attempt_cap,
        ) is not None else "timeout"
    elif field == "advert":
        if public and node.get("advert_interval_min") is not None:
            cmd = f"set advert.interval {int(node['advert_interval_min'])}"
        else:
            cmd = "set advert.interval 0"
        send = await _set_cli(
            ctx.client, target, cmd,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            field="advert", attempt_num=attempt_num, attempt_cap=attempt_cap,
        )
    elif field == "flood":
        if public and node.get("flood_advert_interval_h") is not None:
            cmd = f"set flood.advert.interval {int(node['flood_advert_interval_h'])}"
        else:
            cmd = "set flood.advert.interval 0"
        send = await _set_cli(
            ctx.client, target, cmd,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            field="flood_advert", attempt_num=attempt_num, attempt_cap=attempt_cap,
        )
    elif field == "guest":
        guest = _ensure_guest_password(node, ctx.doc, target.key)
        send = await _set_cli(
            ctx.client, target, f"set guest.password {guest}",
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            field="guest", attempt_num=attempt_num, attempt_cap=attempt_cap,
        )
    elif field == "admin":
        admin = node.get("admin_password")
        if password_is_strong(admin):
            admin_pw = normalize_password(admin)
            send = await _set_cli(
                ctx.client, target, f"password {admin_pw}",
                cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
                field="admin", expected=admin_pw,
                attempt_num=attempt_num, attempt_cap=attempt_cap,
            )
        else:
            send = "ok"
    elif field == "path_hash":
        send = "ok" if await set_path_hash_policy(
            ctx.client, target, cmd_timeout=ctx.cmd_timeout, attempts=1,
            log=ctx.log, session=ctx.session, mode=desired_path_hash_mode(node),
            attempt_num=attempt_num, attempt_cap=attempt_cap,
        ) is not None else "timeout"
    elif field == "dutycycle":
        acc = _poll_acc(uq)
        fw = acc.fw or node.get("firmware_version")
        send = "ok" if await set_dutycycle_policy(
            ctx.client, target, cmd_timeout=ctx.cmd_timeout, attempts=1,
            log=ctx.log, session=ctx.session,
            firmware_version=fw, pct=float(desired_dutycycle(node)),
            attempt_num=attempt_num, attempt_cap=attempt_cap,
        ) is not None else "timeout"
    elif field == "ota_autofetch":
        try:
            applied = await set_ota_autofetch_policy(
                ctx.client, target, cmd_timeout=ctx.cmd_timeout, attempts=1,
                log=ctx.log, session=ctx.session,
                mode=desired_ota_autofetch(node),
                attempt_num=attempt_num, attempt_cap=attempt_cap,
            )
        except OtaAutofetchUnsupported:
            ctx.log.step("ota autofetch: skip (unsupported)")
            send = "ok"
        else:
            send = "ok" if applied is not None else "timeout"
    elif field == "acl":
        try:
            want = resolve_node_acl(ctx.doc, node, ctx.keys or {})
        except UnknownPerson as exc:
            ctx.log.step(f"acl: unknown person {exc}")
            want = []
        if not want:
            return JobOutcome.HEARD, None
        heard = uq.session_extra.get("heard_acl") or _poll_acc(uq).heard_acl
        if heard is None:
            wait_cap = mesh_wait_seconds(6000, cap=ctx.cmd_timeout)
            acl_raw = await binary_req_once(
                "GET_ACL",
                lambda dest_wait: ctx.client.commands.req_acl_sync(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                client=ctx.client,
                target=target,
                log=ctx.log,
                session=ctx.session,
                wait_s=wait_cap,
                cap=ctx.cmd_timeout,
                attempt_num=attempt_num,
                attempt_cap=attempt_cap,
            )
            heard = normalize_acl_payload(acl_raw)
            if heard is None:
                return JobOutcome.TIMEOUT, None
            uq.session_extra["heard_acl"] = heard
        send = await _apply_acl(
            ctx.client, target, want, heard,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
            attempt_num=attempt_num, attempt_cap=attempt_cap,
        )
    else:
        return JobOutcome.HARD_FAIL, field

    if send == "ok":
        stamp_key = "flood" if field == "flood" else field
        if stamp_key in applicable:
            stamp(stamp_key)
        remaining = apply_due_fields(ctx.conn, target.key, node, ctx.sites, doc=ctx.doc, keys=ctx.keys)
        if not radio_apply_due_fields(remaining):
            if "identity" in applicable and "identity" in remaining:
                stamp("identity")
        return JobOutcome.HEARD, True

    if send == "timeout":
        return JobOutcome.TIMEOUT, field

    ctx.log.step(f"apply aborted: {field}")
    return JobOutcome.HARD_FAIL, field


def unit_policy_for_manual(
    manual_job: str | None,
    args_force: bool,
    args_live: bool,
    args_group: list[str] | None,
    min_interval: float,
) -> PollPolicy:
    if args_force:
        return PollPolicy(force=True, live_only=False, force_groups=frozenset(args_group or ()), min_interval=min_interval)
    if manual_job == "pull":
        return PollPolicy(force=False, force_groups=frozenset(GET_GROUP_ORDER), min_interval=min_interval)
    if manual_job == "refresh":
        return PollPolicy(force=False, force_groups=frozenset(refresh_due_groups()), min_interval=min_interval)
    return PollPolicy(force=False, live_only=args_live, force_groups=frozenset(args_group or ()), min_interval=min_interval)
