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
    desired_path_hash_mode,
    profile_id,
    radio_apply_due_fields,
)
from envybot.apply import (
    _apply_acl,
    _ensure_guest_password,
    _set_cli,
)
from envybot.history import get_last_seen, insert_apply, record_poll
from envybot.jobs import INVENTORY_GROUPS, JobOutcome, RadioJob, UnitQueue
from envybot.keys_doc import UnknownPerson, resolve_node_acl
from envybot.nodes_doc import MASK_NAME, is_public
from envybot.passwords import normalize_password, password_is_strong
from envybot.poll import GET_GROUP_ORDER, PollPolicy, pull_due_groups, refresh_due_groups
from envybot.position import public_radio_name, resolve_book_position, site_loc_for_unit
from envybot.sun import stamp_sun
from envybot.radio import (
    FleetSession,
    NEIGHBOR_DISCOVER_WAIT_S,
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
    pull_repeater_status,
    send_cmd_once,
    set_book_coord,
    set_dutycycle_policy,
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
    raw_ver: str | None = None
    raw_bl: str | None = None
    lat: float | None = None
    lon: float | None = None
    advert_min: int | None = None
    flood_h: int | None = None
    acl: list[dict[str, Any]] | None = None
    neighbors: list[dict[str, Any]] | None = None
    heard_acl: list[dict[str, Any]] | None = None

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
    if job.kind == "get:status" and acc.status:
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
    if job.kind == "get:telemetry" and acc.telemetry:
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
    if job.kind == "get:neighbors" and acc.neighbors is not None:
        return (
            "neighbors",
            {"ts": ts, "count": len(acc.neighbors), "neighbors": acc.neighbors},
        )
    if job.kind == "get:acl" and acc.acl is not None:
        return ("acl", {"ts": ts, "count": len(acc.acl), "acl": acc.acl})
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
) -> list[RadioJob]:
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

    if job.kind == "get:neighbors_wait":
        wait_s = float(job.extra.get("wait_s", ctx.discover_wait))
        ctx.log.step(f"discover wait {wait_s:g}s")
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

    if job.kind.startswith("get:"):
        group = job.kind.split(":", 1)[1]
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
    if cli_suggests_auth_failure(raw) or cli_error_reply(raw):
        if cli_suggests_auth_failure(raw):
            ctx.session.clear_auth(target.key)
        return JobOutcome.HARD_FAIL, raw

    if group == "firmware":
        fw, platform = parse_firmware(raw)
        if fw:
            acc.fw = fw
            acc.platform = platform
            acc.raw_ver = raw
            acc.record_group(ctx, target.key, "firmware")
            return JobOutcome.HEARD, fw
        return JobOutcome.TIMEOUT, None
    if group == "bootloader":
        bl = parse_bootloader(raw) or ""
        acc.bl = bl
        acc.raw_bl = raw
        acc.record_group(ctx, target.key, "bootloader")
        return JobOutcome.HEARD, bl
    if group == "name":
        acc.name = parse_get_value(raw) or ""
        acc.record_group(ctx, target.key, "name")
        return JobOutcome.HEARD, acc.name
    if group == "lat":
        acc.lat = parse_coord(raw)
        if acc.lat is None:
            acc.lat = 0.0
        acc.record_group(ctx, target.key, "lat")
        return JobOutcome.HEARD, acc.lat
    if group == "lon":
        acc.lon = parse_coord(raw)
        if acc.lon is None:
            acc.lon = 0.0
        acc.record_group(ctx, target.key, "lon")
        return JobOutcome.HEARD, acc.lon
    if group == "advert":
        acc.advert_min = parse_int_get_value(raw) or 0
        acc.record_group(ctx, target.key, "advert")
        return JobOutcome.HEARD, acc.advert_min
    if group == "flood_advert":
        acc.flood_h = parse_int_get_value(raw) or 0
        acc.record_group(ctx, target.key, "flood_advert")
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
            insert_apply(ctx.conn, unit=target.key, field=f, desired=des, ok=True)

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
    ok = False

    if field == "name":
        name = public_radio_name(target.key, node, ctx.sites) if public else MASK_NAME
        ok = await _set_cli(
            ctx.client, target, f"set name {name}",
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session, field="name",
        )
    elif field == "lat":
        val = float(resolve_book_position(node, ctx.sites, key=target.key)["lat"]) if public and resolve_book_position(node, ctx.sites, key=target.key) else 0.0
        ok = await set_book_coord(
            ctx.client, target, "lat", val,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
        ) is not None
    elif field == "lon":
        val = float(resolve_book_position(node, ctx.sites, key=target.key)["lon"]) if public and resolve_book_position(node, ctx.sites, key=target.key) else 0.0
        ok = await set_book_coord(
            ctx.client, target, "lon", val,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
        ) is not None
    elif field == "advert":
        if public and node.get("advert_interval_min") is not None:
            cmd = f"set advert.interval {int(node['advert_interval_min'])}"
        else:
            cmd = "set advert.interval 0"
        ok = await _set_cli(
            ctx.client, target, cmd,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session, field="advert",
        )
    elif field == "flood":
        if public and node.get("flood_advert_interval_h") is not None:
            cmd = f"set flood.advert.interval {int(node['flood_advert_interval_h'])}"
        else:
            cmd = "set flood.advert.interval 0"
        ok = await _set_cli(
            ctx.client, target, cmd,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session, field="flood_advert",
        )
    elif field == "guest":
        guest = _ensure_guest_password(node, ctx.doc, target.key)
        ok = await _set_cli(
            ctx.client, target, f"set guest.password {guest}",
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session, field="guest",
        )
    elif field == "admin":
        admin = node.get("admin_password")
        if password_is_strong(admin):
            admin_pw = normalize_password(admin)
            ok = await _set_cli(
                ctx.client, target, f"password {admin_pw}",
                cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
                field="admin", expected=admin_pw,
            )
        else:
            ok = True
    elif field == "path_hash":
        ok = await set_path_hash_policy(
            ctx.client, target, cmd_timeout=ctx.cmd_timeout, attempts=1,
            log=ctx.log, session=ctx.session, mode=desired_path_hash_mode(node),
        ) is not None
    elif field == "dutycycle":
        acc = _poll_acc(uq)
        fw = acc.fw or node.get("firmware_version")
        ok = await set_dutycycle_policy(
            ctx.client, target, cmd_timeout=ctx.cmd_timeout, attempts=1,
            log=ctx.log, session=ctx.session,
            firmware_version=fw, pct=float(desired_dutycycle(node)),
        ) is not None
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
            )
            heard = normalize_acl_payload(acl_raw)
            if heard is None:
                return JobOutcome.TIMEOUT, None
            uq.session_extra["heard_acl"] = heard
        ok = await _apply_acl(
            ctx.client, target, want, heard,
            cmd_timeout=ctx.cmd_timeout, attempts=1, log=ctx.log, session=ctx.session,
        )
    else:
        return JobOutcome.HARD_FAIL, field

    if ok:
        stamp_key = "flood" if field == "flood" else field
        if stamp_key in applicable:
            stamp(stamp_key)
        remaining = apply_due_fields(ctx.conn, target.key, node, ctx.sites, doc=ctx.doc, keys=ctx.keys)
        if not radio_apply_due_fields(remaining):
            if "identity" in applicable and "identity" in remaining:
                stamp("identity")
        return JobOutcome.HEARD, True

    uq.apply_aborted = True
    ctx.log.step(f"apply aborted: {field} unreachable")
    uq.jobs.clear()
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
