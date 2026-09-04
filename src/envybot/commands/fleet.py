#!/usr/bin/env python3
"""Localhost fleet manager: map, edit, poll GET, apply desired radio profile."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path
from typing import Any

from envybot.apply import apply_is_due, format_apply_plan, persist_guest_if_new
from envybot.fleet_worker import (
    WorkerContext,
    build_apply_jobs,
    build_manual_jobs,
    build_poll_jobs,
    execute_job,
    insert_apply_jobs,
    job_sample,
)
from envybot.history import migrate_legacy
from envybot.jobs import (
    TIMER_JOB_KINDS,
    FleetScheduler,
    JobOutcome,
    DEFAULT_MISS_COOLDOWN_S,
    DEFAULT_RETRY_DELAY_S,
    drop_console_jobs,
    is_console_job,
    is_login_job,
    job_still_queued,
    keep_console_jobs,
)
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import (
    is_paused,
    load_nodes_doc,
    load_sites_for_book,
    migrate_desired,
    sync_paused,
    write_nodes_doc,
)
from envybot.routing import resolve_routing, routing_explicit
from envybot.poll import (
    PollPolicy,
    due_groups,
    format_get_plan,
    format_interval,
    gaps_from_poll,
    in_flight_session,
    partition_due,
    partition_paused,
    poll_summary,
)
from envybot.position import site_loc_for_unit
from envybot.radio import (
    DEFAULT_MIN_POLL_INTERVAL,
    FleetSession,
    PollLog,
    add_companion_args,
    companion_identity,
    connect,
    load_targets,
    probe_only,
    sync_fleet_contacts,
    target_label,
)
from envybot.web.snapshot import DEFAULT_STALE_SECS

CADENCE_CHECK_S = 60.0


def _lane_tag(job: Any, uq: Any) -> str | None:
    if is_console_job(job):
        return "console"
    if uq.manual and uq.manual_job:
        return str(uq.manual_job)
    return None


def _print_lane_heading(target: Any, job: Any, uq: Any, current_lane: str | None) -> str:
    """Unindented heading when the radio moves to a different unit."""
    key = target.key
    if key == current_lane:
        return key
    heading = target_label(target)
    tag = _lane_tag(job, uq)
    if tag:
        heading = f"{heading}  ({tag})"
    print(f"{heading} …", flush=True)
    return key


async def _serve_web_until_stop(web_ctx: Any, poll_exit: int) -> int:
    print(f"Fleet UI still running at {web_ctx.url} (Ctrl+C to exit).")
    await web_ctx.start_yaml_watch()
    try:
        await web_ctx.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await web_ctx.shutdown()
    return poll_exit


def _migrate_book(nodes_path: Path) -> tuple[dict[str, Any], Any]:
    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        nodes = {}
        doc["nodes"] = nodes
    sites = load_sites_for_book(nodes_path)
    conn = migrate_legacy(nodes_path.parent, nodes)
    if migrate_desired(doc, sites):
        write_nodes_doc(nodes_path, doc)
        print("Migrated nodes.yaml (stripped observed keys / routing / leftover node GPS).")
    return doc, conn


def _seed_auto_work(
    scheduler: FleetScheduler,
    *,
    auto_targets: list[Any],
    conn: Any,
    nodes: dict[str, Any],
    sites: dict[str, Any],
    doc: dict[str, Any],
    keys: dict[str, list[str]],
    policy: PollPolicy,
    do_poll: bool,
    do_apply: bool,
    force: bool,
    now: int,
    skip_discover: bool = False,
    discover_wait: float = 12.0,
    session_states: dict[str, dict[str, Any]] | None = None,
    bypass_cooldown: bool = False,
) -> int:
    """Queue due GET/apply. Busy units keep their GET lane; apply is spliced in."""
    queued = 0
    for target in auto_targets:
        if not bypass_cooldown and scheduler.is_on_cooldown(target.key):
            continue
        due = due_groups(conn, target.key, policy=policy, now=now) if do_poll else []
        target.due_groups = due
        apply_due = do_apply and apply_is_due(
            conn, target.key, nodes.get(target.key) or {}, sites, force=force, doc=doc, keys=keys
        )
        existing = scheduler.units.get(target.key)
        if existing and existing.jobs:
            if not apply_due:
                continue
            applied = insert_apply_jobs(
                existing, build_apply_jobs(target.key, force=force)
            )
            if not applied:
                continue
            if session_states is not None:
                prev = session_states.get(target.key) or {}
                session_states[target.key] = in_flight_session(
                    manual_job=existing.manual_job if existing.manual else None,
                    job_kind=str(prev.get("kind") or "login"),
                    due_groups=list(due),
                    apply=True,
                    queued=True,
                )
            queued += 1
            continue
        if not due and not apply_due:
            continue
        jobs = build_poll_jobs(
            target,
            due,
            do_apply=do_apply,
            apply_due=apply_due,
            force_apply=force,
            skip_discover=skip_discover,
            discover_wait=discover_wait,
        )
        if not jobs:
            continue
        scheduler.enqueue_jobs(target, jobs)
        if session_states is not None:
            session_states[target.key] = in_flight_session(
                manual_job=None,
                job_kind="login",
                due_groups=list(due),
                apply=apply_due,
                queued=True,
            )
        queued += 1
    return queued


async def run(args: argparse.Namespace) -> int:
    nodes_path: Path = args.nodes
    doc, conn = _migrate_book(nodes_path)
    nodes = doc.get("nodes") or {}
    sites = load_sites_for_book(nodes_path)
    keys = load_keys(keys_path(nodes_path))

    web_ctx: Any | None = None
    session_states: dict[str, dict[str, Any]] = {}
    use_web = not args.no_web

    scheduler = FleetScheduler(
        max_attempts=args.attempts or 0,
        retry_delay=args.retry_delay,
        miss_cooldown=args.miss_cooldown,
        round_delay=args.round_delay,
    )
    manual_keys: set[str] = set()

    if use_web:
        from envybot.web.server import start_monitor_web

        web_ctx = await start_monitor_web(
            nodes_path=nodes_path,
            host=args.bind,
            port=args.port,
            stale_secs=DEFAULT_STALE_SECS,
            open_browser=args.open,
        )
        web_ctx.bind_scheduler(
            scheduler,
            manual_keys=manual_keys,
            conn=conn,
            nodes=nodes,
            sites=sites,
            doc=doc,
            keys=keys,
            do_poll=not args.apply_only,
            do_apply=not args.poll_only,
            skip_discover=args.no_discover,
            discover_wait=args.discover_wait,
        )

    if args.web_only:
        if web_ctx is None:
            print("envybot fleet: --web-only conflicts with --no-web", file=sys.stderr)
            return 2
        print("Web-only mode — watching nodes.yaml. Ctrl+C to exit.")
        return await _serve_web_until_stop(web_ctx, 0)

    include = {u.lower() for u in args.unit} if args.unit else None
    skip = {u.lower() for u in args.skip} if args.skip else None
    bypass_cooldown = bool(args.force or include)
    all_targets = load_targets(
        nodes_path, deployed_only=args.deployed_only, include=include, skip=skip
    )
    auto_targets, paused_targets = partition_paused(all_targets, nodes)
    if skip and not args.quiet:
        print(f"Excluding {len(skip)} unit(s): {', '.join(sorted(skip))}")
    if paused_targets and not args.quiet:
        print(
            f"Paused {len(paused_targets)} unit(s): "
            f"{', '.join(t.key for t in paused_targets)} (Refresh, Pull, and Push still work)"
        )
    if not all_targets:
        print("No pollable routers matched filters.", file=sys.stderr)
        if web_ctx:
            await web_ctx.refresh_snapshot(poll={"phase": "idle", "error": "no targets"})
            return await _serve_web_until_stop(web_ctx, 1)
        return 1

    do_poll = not args.apply_only
    do_apply = not args.poll_only
    policy = PollPolicy(
        force=args.force,
        live_only=args.live and not args.force,
        force_groups=frozenset(args.group or ()),
        min_interval=args.min_interval,
    )
    now = int(time.time())
    if do_poll:
        targets, skipped = partition_due(auto_targets, conn, policy=policy, now=now)
    else:
        targets, skipped = list(auto_targets), []
        for t in targets:
            t.due_groups = []

    if skipped and not args.quiet and do_poll:
        print(
            f"Skipping {len(skipped)} up-to-date poll(s) "
            f"(live periodic within {format_interval(policy.min_interval)})"
        )

    for target in paused_targets:
        session_states[target.key] = {"state": "paused"}
    for target in auto_targets:
        due = target.due_groups if hasattr(target, "due_groups") else []
        apply_due = do_apply and apply_is_due(
            conn, target.key, nodes.get(target.key) or {}, sites, force=args.force, doc=doc, keys=keys
        )
        if due or apply_due:
            session_states[target.key] = in_flight_session(
                manual_job=None,
                job_kind="login",
                due_groups=list(due),
                apply=apply_due,
                queued=True,
            )

    _seed_auto_work(
        scheduler,
        auto_targets=auto_targets,
        conn=conn,
        nodes=nodes,
        sites=sites,
        doc=doc,
        keys=keys,
        policy=policy,
        do_poll=do_poll,
        do_apply=do_apply,
        force=args.force,
        now=now,
        skip_discover=args.no_discover,
        discover_wait=args.discover_wait,
        session_states=session_states,
        bypass_cooldown=bypass_cooldown,
    )

    initial_units = scheduler.active_unit_count()
    if web_ctx:
        web_ctx.set_worker_active(True)
        web_ctx.console.bind_publish(web_ctx.hub.publish_console)
        await web_ctx.refresh_snapshot(
            session_states=session_states,
            poll={
                "phase": "starting" if initial_units else "idle",
                "pending": scheduler.pending_count(),
                "units": initial_units,
                "total": len(all_targets),
                "accepting": True,
            },
        )

    if initial_units == 0 and args.once:
        print(f"All {len(all_targets)} router(s) up to date.")
        if web_ctx:
            await web_ctx.publish_session(
                {"phase": "done", "pending": 0, "ok": 0, "total": len(all_targets)}
            )
            return await _serve_web_until_stop(web_ctx, 0)
        return 0

    if initial_units:
        cmd_count = scheduler.pending_count()
        print(
            f"Fleet {initial_units} unit(s) queued ({cmd_count} command(s))"
            + (
                " — retries until all succeed (Ctrl+C to stop) …"
                if not args.once
                else " — single pass …"
            )
        )
    elif not args.once:
        print("Fleet idle — Refresh, Pull, or Push units in the UI or Ctrl+C to exit.")

    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    session = FleetSession()
    session.conn = conn
    client = await connect(args)
    companion_label = companion_identity(client)
    companion_short = companion_label[:12] if companion_label else None
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)
    session.enable_companion_recovery(client, all_targets)

    worker_ctx = WorkerContext(
        client=client,
        conn=conn,
        nodes=nodes,
        sites=sites,
        doc=doc,
        keys=keys,
        session=session,
        log=log,
        cmd_timeout=args.timeout,
        login_timeout=args.login_timeout,
        discover_wait=args.discover_wait,
        skip_discover=args.no_discover,
        do_poll=do_poll,
        do_apply=do_apply,
        max_attempts=args.attempts or 10,
    )
    worker_ctx.skip_discover = args.no_discover

    if web_ctx:
        web_ctx._fleet_session = session
    if initial_units:
        work_targets = [t for t in auto_targets if scheduler.units.get(t.key, None) and scheduler.units[t.key].jobs]
        if work_targets:
            await sync_fleet_contacts(client, work_targets, log=log)

    succeeded: dict[str, bool] = {}
    interrupted = False
    yaml_dirty = False
    round_num = 0
    total = max(initial_units, len(all_targets))
    current_lane: str | None = None
    planned_lanes: set[str] = set()

    async def on_job_start(uq: Any, job: Any) -> None:
        nonlocal current_lane
        target = uq.target
        uq.session_extra["console_manager"] = web_ctx.console if web_ctx else None
        if is_console_job(job):
            session.audit_source = "console"
        elif session.audit_source == "console":
            session.audit_source = None
        tab_id = str(job.extra.get("tab_id") or "")
        if web_ctx and tab_id and is_console_job(job):
            if job.kind == "console:login":
                await web_ctx.console.on_login_start(
                    tab_id,
                    job.attempt + 1,
                    scheduler.max_attempts or worker_ctx.max_attempts,
                )
            elif job.kind == "console:cli":
                cmd = str(job.extra.get("cmd") or "")
                await web_ctx.console.on_cli_start(
                    tab_id,
                    job.attempt + 1,
                    scheduler.max_attempts or worker_ctx.max_attempts,
                    cmd,
                )
        node_record = nodes.get(target.key) or {}
        job_name = uq.manual_job
        unit_force = args.force or job_name == "push"
        apply_due_now = job_name == "push" or apply_is_due(
            conn, target.key, node_record, sites, force=unit_force, doc=doc, keys=keys
        )
        if not args.quiet and job.kind not in TIMER_JOB_KINDS:
            entered = current_lane != target.key
            current_lane = _print_lane_heading(target, job, uq, current_lane)
            if entered and target.key not in planned_lanes and job.kind == "login":
                planned_lanes.add(target.key)
                if do_poll and target.due_groups:
                    need, skip_plan = format_get_plan(
                        conn, target.key, target.due_groups, policy=policy, now=int(time.time())
                    )
                    print(f"  poll need: {need}")
                    print(f"  poll skip: {skip_plan}")
                if do_apply and apply_due_now:
                    apply_need, apply_have = format_apply_plan(
                        conn, target.key, node_record, sites, force=unit_force, doc=doc, keys=keys
                    )
                    print(f"  apply need: {apply_need}")
                    print(f"  apply skip: {apply_have}")
        session_states[target.key] = in_flight_session(
            manual_job=job_name if uq.manual else None,
            job_kind=job.kind,
            attempt=job.attempt + 1,
            max_attempts=scheduler.max_attempts,
            due_groups=list(getattr(target, "due_groups", [])),
            apply=apply_due_now,
        )
        if is_console_job(job):
            session_states[target.key] = {
                "state": "console",
                "kind": job.kind,
                "attempt": job.attempt + 1,
                "max_attempts": scheduler.max_attempts,
            }
        if web_ctx:
            active_poll = {
                "phase": "polling",
                "accepting": True,
                "round": round_num,
                "unit": target.key,
                "pending": scheduler.pending_count(),
                "units": scheduler.active_unit_count(),
                "total": total,
                "companion": companion_short,
            }
            web_ctx.sync_worker_state(
                session_states=session_states,
                poll=active_poll,
                companion=companion_short,
            )
            await web_ctx.publish_unit(
                target.key,
                session=session_states[target.key],
                session_states=session_states,
                companion=companion_short,
                poll=active_poll,
            )

    async def on_job_done(uq: Any, job: Any, outcome: JobOutcome, payload: Any) -> None:
        nonlocal yaml_dirty, round_num
        target = uq.target
        tab_id = str(job.extra.get("tab_id") or "")
        if web_ctx and tab_id:
            job_gen = int(job.extra.get("cancel_gen") or 0)
            same_tab = [
                j
                for j in uq.jobs
                if is_console_job(j) and str(j.extra.get("tab_id") or "") == tab_id
            ]
            if is_console_job(job) and web_ctx.console.cancel_check(tab_id, job_gen):
                pass
            elif job.kind == "console:login" and outcome == JobOutcome.HEARD:
                await web_ctx.console.on_login_ready(tab_id)
            elif job.kind == "console:login" and outcome == JobOutcome.CANCELLED and not same_tab:
                await web_ctx.console.on_cli_done(
                    tab_id,
                    ok=False,
                    reply=None,
                    error="cancelled",
                )
            elif job.kind == "console:login" and outcome in (
                JobOutcome.TIMEOUT,
                JobOutcome.HARD_FAIL,
            ):
                if outcome == JobOutcome.HARD_FAIL or not job_still_queued(uq, job):
                    drop_console_jobs(uq, tab_id or None)
                    await web_ctx.console.on_cli_done(
                        tab_id,
                        ok=False,
                        reply=None,
                        error=str(payload or "login failed"),
                        drop_pending=True,
                        drain=False,
                    )
            elif job.kind == "console:cli":
                if outcome == JobOutcome.CANCELLED:
                    await web_ctx.console.on_cli_done(
                        tab_id,
                        ok=False,
                        reply=None,
                        error="cancelled",
                    )
                elif outcome == JobOutcome.HEARD:
                    err = None
                    reply = str(payload) if payload is not None else None
                    if reply and (
                        reply.strip().upper().startswith("ERR")
                        or "unknown command" in reply.lower()
                    ):
                        err = reply
                    await web_ctx.console.on_cli_done(
                        tab_id,
                        ok=err is None,
                        reply=reply,
                        error=err,
                    )
                elif outcome == JobOutcome.HARD_FAIL:
                    await web_ctx.console.on_cli_done(
                        tab_id,
                        ok=False,
                        reply=str(payload) if payload is not None else None,
                        error=str(payload or "failed"),
                        drain=False,
                    )
                elif outcome == JobOutcome.TIMEOUT:
                    # Settle already incremented attempt; job stays queued
                    # until --attempts. Parking here used to drop after 1/10.
                    if not any(j is job for j in uq.jobs):
                        drop_console_jobs(uq, tab_id or None)
                        await web_ctx.console.on_cli_done(
                            tab_id,
                            ok=False,
                            reply=None,
                            error=str(payload or "command timeout"),
                            drain=False,
                        )
        if (
            web_ctx
            and job.kind == "login"
            and outcome in (JobOutcome.TIMEOUT, JobOutcome.HARD_FAIL)
            and not job_still_queued(uq, job)
        ):
            await web_ctx.console.fail_sending(
                target.key, error=str(payload or "login failed")
            )
        node_record = nodes.get(target.key) or {}
        guest_before = str(node_record.get("guest_password") or "")

        prev = session_states.get(target.key) or {}
        due = list(getattr(target, "due_groups", []))
        apply_job = str(job.kind).startswith("apply:")
        if prev.get("state") == "paused":
            pass
        elif (
            not is_console_job(job)
            and not apply_job
            and (
                outcome == JobOutcome.HARD_FAIL
                or (outcome == JobOutcome.TIMEOUT and not uq.jobs)
            )
        ):
            session_states[target.key] = {
                "state": "unreachable",
                "error": str(payload or "failed"),
                "due_groups": due,
            }
            if not args.quiet:
                print(f"  unreachable: {payload}")
            manual_keys.discard(target.key)
        elif is_console_job(job) and uq.jobs and is_console_job(uq.jobs[0]):
            session_states[target.key] = {
                "state": "console",
                "kind": uq.jobs[0].kind,
            }
        elif uq.jobs:
            head = uq.jobs[0]
            session_states[target.key] = in_flight_session(
                manual_job=uq.manual_job,
                job_kind=head.kind,
                attempt=head.attempt + 1,
                max_attempts=scheduler.max_attempts,
                due_groups=due,
                apply=bool(prev.get("apply")),
                error=str(payload) if outcome == JobOutcome.TIMEOUT and payload else None,
                queued=True,
            )
            if not args.quiet and outcome == JobOutcome.TIMEOUT:
                still = job_still_queued(uq, job)
                if still and uq.jobs[0].kind == job.kind:
                    if payload:
                        print(f"  retrying: {payload}")
                    elif scheduler.max_attempts:
                        print(
                            f"  retrying {job.kind} "
                            f"({uq.jobs[0].attempt + 1}/{scheduler.max_attempts})"
                        )
                elif not still:
                    n = job.attempt or scheduler.max_attempts
                    if is_login_job(job):
                        print(f"  {job.kind}: gave up after {n}, stopping")
                    elif scheduler.max_attempts:
                        print(f"  {job.kind}: gave up after {n}, continuing")
        else:
            session_states[target.key] = {"state": "ok", "due_groups": []}
            dropped = uq.session_extra.get("dropped_jobs") or []
            if (
                not args.quiet
                and apply_job
                and outcome in (JobOutcome.HARD_FAIL, JobOutcome.TIMEOUT)
            ):
                print(f"  apply aborted: {payload}")
            if not args.quiet and job.kind != "get:neighbors_wait":
                acc = uq.session_extra.get("poll_acc")
                if acc is not None:
                    res = acc.to_result(target.key)
                    remaining = gaps_from_poll(res)
                    if dropped or remaining:
                        gaps = list(remaining)
                        if dropped:
                            gaps.extend(str(k) for k in dropped)
                        print(f"  partial OK {poll_summary(res)} (gaps: {', '.join(gaps)})")
                    else:
                        print(f"  OK {poll_summary(res)}")
            uq.session_extra.pop("dropped_jobs", None)
            if uq.manual_job != "console":
                manual_keys.discard(target.key)

        if str(node_record.get("guest_password") or "") != guest_before:
            yaml_dirty = True

        if web_ctx:
            sample_evt = job_sample(
                job,
                uq,
                outcome,
                payload,
                site_loc=site_loc_for_unit(
                    target.key, node_record, sites, doc=doc
                ),
            )
            sess = dict(session_states.get(target.key, {}))
            live_route = uq.session_extra.get("live_route")
            if live_route:
                sess["live_route"] = live_route
            await web_ctx.publish_unit(
                target.key,
                session=sess,
                session_states=session_states,
                companion=companion_short,
                poll=web_ctx._poll_state,
                sample=sample_evt,
            )

    async def execute_one(job: Any, uq: Any) -> tuple[JobOutcome, Any | None]:
        if not await session.ensure_companion_connected(log=log):
            return JobOutcome.TIMEOUT, "companion disconnected"
        sync_paused(nodes_path, nodes)
        node_row = nodes.get(uq.target.key) or {}
        uq.target.routing = resolve_routing(node_row)
        uq.target.routing_explicit = routing_explicit(node_row)
        if (
            is_paused(nodes.get(uq.target.key))
            and uq.target.key not in manual_keys
            and not is_console_job(job)
        ):
            keep_console_jobs(uq)
            if not uq.jobs:
                session_states[uq.target.key] = {"state": "paused"}
            return JobOutcome.HARD_FAIL, "paused"
        uq.session_extra["force_apply"] = args.force or uq.manual_job == "push"
        return await execute_job(job, uq, worker_ctx)

    async def pause_watch() -> None:
        while not scheduler._stop:
            paused_keys = {
                t.key for t in auto_targets if is_paused(nodes.get(t.key)) and t.key not in manual_keys
            }
            dropped = scheduler.drop_auto_paused(paused_keys, manual_keys=manual_keys)
            for key in dropped:
                session_states[key] = {"state": "paused"}
            await asyncio.sleep(0.5)

    pause_task = asyncio.create_task(pause_watch())

    try:
        while True:
            if scheduler.pending_count() == 0:
                if args.once or web_ctx is None:
                    break
                idle_poll = {
                    "phase": "idle",
                    "accepting": True,
                    "companion": companion_short,
                }
                web_ctx.sync_worker_state(
                    session_states=session_states,
                    poll=idle_poll,
                    companion=companion_short,
                )
                await web_ctx.publish_session(idle_poll)
                await scheduler.wait_for_work(timeout=CADENCE_CHECK_S)
                if scheduler._stop:
                    break
                sync_paused(nodes_path, nodes)
                cadence_policy = PollPolicy(
                    force=False,
                    live_only=args.live and not args.force,
                    force_groups=frozenset(args.group or ()),
                    min_interval=args.min_interval,
                )
                seeded = _seed_auto_work(
                    scheduler,
                    auto_targets=auto_targets,
                    conn=conn,
                    nodes=nodes,
                    sites=sites,
                    doc=doc,
                    keys=keys,
                    policy=cadence_policy,
                    do_poll=do_poll,
                    do_apply=do_apply,
                    force=False,
                    now=int(time.time()),
                    skip_discover=args.no_discover,
                    discover_wait=args.discover_wait,
                    session_states=session_states,
                )
                if scheduler.pending_count() == 0:
                    continue
                round_num += 1
                if args.max_rounds and round_num > args.max_rounds:
                    break
                new_units = [uq.target for uq in scheduler.units.values() if uq.jobs]
                if new_units:
                    await sync_fleet_contacts(client, new_units, log=log)
                if not args.quiet:
                    units = scheduler.active_unit_count()
                    cmds = scheduler.pending_count()
                    kind = "Cadence" if seeded else "Manual jobs"
                    print(f"{kind}: {units} unit(s), {cmds} command(s) queued")
                continue

            round_num += 1
            batch = await scheduler.run(
                execute_one,
                on_job_start=on_job_start,
                on_job_done=on_job_done,
                once=True,
            )
            succeeded.update(batch)

            if args.once:
                break
            if scheduler.pending_count() == 0:
                continue
            cadence_policy = PollPolicy(
                force=False,
                live_only=args.live and not args.force,
                force_groups=frozenset(args.group or ()),
                min_interval=args.min_interval,
            )
            before_keys = {k for k, uq in scheduler.units.items() if uq.jobs}
            seeded = _seed_auto_work(
                scheduler,
                auto_targets=auto_targets,
                conn=conn,
                nodes=nodes,
                sites=sites,
                doc=doc,
                keys=keys,
                policy=cadence_policy,
                do_poll=do_poll,
                do_apply=do_apply,
                force=False,
                now=int(time.time()),
                skip_discover=args.no_discover,
                discover_wait=args.discover_wait,
                session_states=session_states,
            )
            if seeded:
                new_units = [
                    uq.target
                    for k, uq in scheduler.units.items()
                    if uq.jobs and k not in before_keys
                ]
                if new_units:
                    await sync_fleet_contacts(client, new_units, log=log)
                if not args.quiet:
                    print(
                        f"Cadence: {scheduler.active_unit_count()} unit(s), "
                        f"{scheduler.pending_count()} command(s) queued"
                    )
            if args.round_delay > 0:
                if not args.quiet:
                    print(
                        f"Waiting {args.round_delay:.0f}s before retrying "
                        f"{scheduler.active_unit_count()} unit(s), "
                        f"{scheduler.pending_count()} command(s) …"
                    )
                await asyncio.sleep(args.round_delay)

    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — keeping progress from successful units.")
    finally:
        scheduler.stop()
        pause_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pause_task
        if web_ctx:
            web_ctx.set_worker_active(False)
        await client.stop_auto_message_fetching()
        await client.disconnect()
        if yaml_dirty:
            persist_guest_if_new(nodes_path, doc)

    ok_count = len(succeeded)
    pending_count = scheduler.pending_count()
    print(f"fleet {ok_count}/{total} router(s)")
    if pending_count:
        pending_units = [
            uq.target.unit_id for uq in scheduler.units.values() if uq.jobs
        ]
        print(f"Still pending ({len(pending_units)}): {', '.join(pending_units)}")
    poll_exit = 130 if interrupted else (0 if pending_count == 0 else 2)
    if web_ctx:
        await web_ctx.publish_session(
            {
                "phase": "done",
                "ok": ok_count,
                "pending": pending_count,
                "total": total,
                "round": round_num,
                "companion": companion_short,
            }
        )
        return await _serve_web_until_stop(web_ctx, poll_exit)
    return poll_exit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    add_companion_args(parser)
    parser.add_argument("--probe", action="store_true", help="List companions; do not touch fleet")
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_S,
        metavar="SEC",
        help="Auto poll/apply only: park unit after timeout before retry (default 60). "
        "Console and manual Refresh/Pull/Push are exempt.",
    )
    parser.add_argument(
        "--miss-cooldown",
        type=float,
        default=DEFAULT_MISS_COOLDOWN_S,
        metavar="SEC",
        help="Auto poll/apply only: after max attempts, skip re-seed until cooldown (default 3600). "
        "Use --force, --unit, or manual UI to bypass.",
    )
    parser.add_argument("--round-delay", type=float, default=0.0)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--once", action="store_true", help="Single pass only")
    parser.add_argument(
        "--deployed-only",
        action="store_true",
        help="Poll only site-bound units (skip bag/bench)",
    )
    parser.add_argument("--unit", action="append", metavar="me0003")
    parser.add_argument("--skip", action="append", metavar="me0001")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_POLL_INTERVAL,
        metavar="SECS",
        help="Live GET interval for status/telemetry (default 3600). Neighbors stay 24h.",
    )
    parser.add_argument("--force", action="store_true", help="Pull every GET group and Push profile")
    parser.add_argument("--live", action="store_true", help="Periodic GET groups only")
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Skip remote discover.neighbors (GET stored table only)",
    )
    parser.add_argument(
        "--discover-wait",
        type=float,
        default=12.0,
        metavar="SEC",
        help="Wait after discover.neighbors before GET (default 12)",
    )
    parser.add_argument("--group", action="append", choices=None)
    parser.add_argument("--poll-only", action="store_true", help="GET only; do not apply")
    parser.add_argument("--apply-only", action="store_true", help="SET only; skip GET cadence")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--web-only", action="store_true")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    from envybot.poll import GET_GROUP_ORDER

    for action in parser._actions:
        if action.dest == "group":
            action.choices = list(GET_GROUP_ORDER)
    if args.probe:
        return asyncio.run(probe_only(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
