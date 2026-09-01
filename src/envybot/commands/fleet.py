#!/usr/bin/env python3
"""Localhost fleet manager: map, edit, poll GET, apply desired radio profile."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from envybot.apply import apply_is_due, apply_one, format_apply_plan, persist_guest_if_new
from envybot.history import migrate_legacy, record_poll
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import (
    is_paused,
    load_nodes_doc,
    load_sites_for_book,
    migrate_desired,
    sync_paused,
    write_nodes_doc,
)
from envybot.poll import (
    GET_GROUP_ORDER,
    PollPolicy,
    due_groups,
    format_get_plan,
    format_interval,
    gaps_from_poll,
    manual_job_session_state,
    partition_due,
    partition_paused,
    poll_one,
    poll_summary,
    pull_due_groups,
    refresh_due_groups,
)
from envybot.radio import (
    DEFAULT_MESH_ATTEMPTS,
    DEFAULT_MIN_POLL_INTERVAL,
    NEIGHBOR_DISCOVER_WAIT_S,
    FleetSession,
    PollLog,
    add_companion_args,
    admin_login,
    companion_identity,
    connect,
    load_targets,
    poll_staleness_key,
    probe_only,
    sync_fleet_contacts,
    target_label,
)


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
        print("Migrated nodes.yaml (stripped observed keys / leftover node GPS).")
    return doc, conn


def _manual_job_targets(
    jobs: list[tuple[str, str]],
    *,
    nodes_path: Path,
    conn: Any,
    nodes: dict[str, Any],
    sites: dict[str, Any],
    doc: dict[str, Any],
    keys: dict[str, list[str]],
    session_states: dict[str, dict[str, Any]],
    apply_keys: set[str],
    manual_keys: set[str],
    manual_jobs: dict[str, str],
    do_poll: bool,
    do_apply: bool,
    attempt_counts: dict[str, int],
) -> list[Any]:
    out: list[Any] = []
    include = {k for k, _ in jobs}
    for target in load_targets(nodes_path, deployed_only=False, include=include, skip=None):
        job = next(j for k, j in jobs if k == target.key)
        manual_jobs[target.key] = job
        manual_keys.add(target.key)
        if do_poll:
            if job == "pull":
                target.due_groups = pull_due_groups()
            elif job == "refresh":
                target.due_groups = refresh_due_groups()
            else:
                target.due_groups = []
        else:
            target.due_groups = []
        if do_apply and job == "push":
            apply_keys.add(target.key)
        elif do_apply and job in ("refresh", "pull") and apply_is_due(
            conn, target.key, nodes.get(target.key) or {}, sites, doc=doc, keys=keys
        ):
            apply_keys.add(target.key)
        session_states[target.key] = {
            "state": manual_job_session_state(job),
            "manual": True,
            "job": job,
        }
        attempt_counts.pop(target.key, None)
        out.append(target)
    return out


async def _drain_manual_into(
    web_ctx: Any | None,
    pending: list[Any],
    *,
    nodes_path: Path,
    conn: Any,
    nodes: dict[str, Any],
    sites: dict[str, Any],
    doc: dict[str, Any],
    keys: dict[str, list[str]],
    session_states: dict[str, dict[str, Any]],
    apply_keys: set[str],
    manual_keys: set[str],
    manual_jobs: dict[str, str],
    do_poll: bool,
    do_apply: bool,
    attempt_counts: dict[str, int],
) -> None:
    if web_ctx is None:
        return
    jobs = await web_ctx.drain_manual_queue()
    if not jobs:
        return
    existing = {t.key for t in pending}
    for target in _manual_job_targets(
        jobs,
        nodes_path=nodes_path,
        conn=conn,
        nodes=nodes,
        sites=sites,
        doc=doc,
        keys=keys,
        session_states=session_states,
        apply_keys=apply_keys,
        manual_keys=manual_keys,
        manual_jobs=manual_jobs,
        do_poll=do_poll,
        do_apply=do_apply,
        attempt_counts=attempt_counts,
    ):
        if target.key not in existing:
            pending.append(target)
            existing.add(target.key)


def _drop_auto_paused(
    pending: list[Any],
    *,
    nodes_path: Path,
    nodes: dict[str, Any],
    manual_keys: set[str],
    session_states: dict[str, dict[str, Any]],
) -> tuple[list[Any], list[Any]]:
    """Drop book-paused units from auto work. Manual Refresh/Pull/Push stays."""
    sync_paused(nodes_path, nodes)
    kept, dropped = partition_paused(pending, nodes, forced_keys=manual_keys)
    for target in dropped:
        session_states[target.key] = {"state": "paused"}
    return kept, dropped


async def run(args: argparse.Namespace) -> int:
    nodes_path: Path = args.nodes
    doc, conn = _migrate_book(nodes_path)
    nodes = doc.get("nodes") or {}
    sites = load_sites_for_book(nodes_path)
    keys = load_keys(keys_path(nodes_path))

    web_ctx: Any | None = None
    session_states: dict[str, dict[str, Any]] = {}
    use_web = not args.no_web

    if use_web:
        from envybot.web.server import start_monitor_web

        web_ctx = await start_monitor_web(
            nodes_path=nodes_path,
            host=args.bind,
            port=args.port,
            stale_secs=args.min_interval,
            open_browser=args.open,
        )

    if args.web_only:
        if web_ctx is None:
            print("envybot fleet: --web-only conflicts with --no-web", file=sys.stderr)
            return 2
        print("Web-only mode — watching nodes.yaml. Ctrl+C to exit.")
        return await _serve_web_until_stop(web_ctx, 0)

    include = {u.lower() for u in args.unit} if args.unit else None
    skip = {u.lower() for u in args.skip} if args.skip else None
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
    apply_due = [
        t
        for t in auto_targets
        if do_apply and apply_is_due(
            conn, t.key, nodes.get(t.key) or {}, sites, force=args.force, doc=doc, keys=keys
        )
    ]
    apply_keys = {t.key for t in apply_due}
    if skipped and not args.quiet and do_poll:
        print(
            f"Skipping {len(skipped)} up-to-date poll(s) "
            f"(live periodic within {format_interval(policy.min_interval)})"
        )
    work_keys = {t.key for t in targets} | apply_keys
    work = [t for t in auto_targets if t.key in work_keys]
    for target in paused_targets:
        session_states[target.key] = {"state": "paused"}
    for target in work:
        session_states[target.key] = {
            "state": "queued",
            "due_groups": list(target.due_groups),
            "apply": target.key in apply_keys,
        }
    if web_ctx:
        await web_ctx.refresh_snapshot(
            session_states=session_states,
            poll={"phase": "starting", "pending": len(work), "total": len(all_targets)},
        )
    if not work and args.once:
        print(f"All {len(all_targets)} router(s) up to date.")
        if web_ctx:
            await web_ctx.publish_session(
                {"phase": "done", "pending": 0, "ok": 0, "total": len(all_targets)}
            )
            return await _serve_web_until_stop(web_ctx, 0)
        return 0
    if not work:
        if paused_targets and not auto_targets:
            print(
                f"All {len(paused_targets)} matched unit(s) are paused — idle for manual Refresh/Pull/Push."
            )
        else:
            print(f"All {len(all_targets)} router(s) up to date — idle for manual Refresh/Pull/Push.")

    retry_mode = not args.once
    if work:
        print(
            f"Fleet {len(work)} router(s)"
            + (" — retries until all succeed (Ctrl+C to stop) …" if retry_mode else " — single pass …")
        )
    elif retry_mode:
        print("Fleet idle — Refresh, Pull, or Push units in the UI or Ctrl+C to exit.")

    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    session = FleetSession()
    client = await connect(args)
    companion_label = companion_identity(client)
    companion_short = companion_label[:12] if companion_label else None
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)
    session.enable_companion_recovery(client, all_targets)
    if web_ctx:
        web_ctx.set_worker_active(True)
    if work:
        await sync_fleet_contacts(client, work, log=log)
    pending = list(work)
    manual_keys: set[str] = set()
    manual_jobs: dict[str, str] = {}
    succeeded: dict[str, bool] = {}
    attempt_counts: dict[str, int] = {}
    round_num = 0
    interrupted = False
    yaml_dirty = False
    total = len(work) if work else len(all_targets)

    try:
        while True:
            if not pending:
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
                manual_jobs_list = await web_ctx.wait_manual_queue()
                if not manual_jobs_list:
                    break
                manual = _manual_job_targets(
                    manual_jobs_list,
                    nodes_path=nodes_path,
                    conn=conn,
                    nodes=nodes,
                    sites=sites,
                    doc=doc,
                    keys=keys,
                    session_states=session_states,
                    apply_keys=apply_keys,
                    manual_keys=manual_keys,
                    manual_jobs=manual_jobs,
                    do_poll=do_poll,
                    do_apply=do_apply,
                    attempt_counts=attempt_counts,
                )
                if not manual:
                    continue
                pending = manual
                total = max(total, len({t.key for t in pending} | set(succeeded)))
                await sync_fleet_contacts(client, manual, log=log)
                print(f"Manual jobs: {len(manual)} unit(s)")
                continue

            pending, newly_paused = _drop_auto_paused(
                pending,
                nodes_path=nodes_path,
                nodes=nodes,
                manual_keys=manual_keys,
                session_states=session_states,
            )
            if newly_paused and not args.quiet:
                print(
                    f"Paused {len(newly_paused)} pending unit(s): "
                    f"{', '.join(t.key for t in newly_paused)}"
                )
            if web_ctx:
                for target in newly_paused:
                    await web_ctx.publish_unit(
                        target.key,
                        session=session_states[target.key],
                        session_states=session_states,
                        companion=companion_short,
                        poll=web_ctx._poll_state,
                    )
            if not pending:
                continue
            round_num += 1
            if args.max_rounds and round_num > args.max_rounds:
                break
            if retry_mode and round_num > 1:
                print(f"\n--- round {round_num}: {len(pending)} still pending ---")
            next_pending = []
            for target in pending:
                sync_paused(nodes_path, nodes)
                job = manual_jobs.get(target.key)
                if is_paused(nodes.get(target.key)) and target.key not in manual_keys:
                    session_states[target.key] = {"state": "paused"}
                    print(f"{target_label(target)} paused — skip")
                    if web_ctx:
                        await web_ctx.publish_unit(
                            target.key,
                            session=session_states[target.key],
                            session_states=session_states,
                            companion=companion_short,
                            poll=web_ctx._poll_state,
                        )
                    await _drain_manual_into(
                        web_ctx,
                        next_pending,
                        nodes_path=nodes_path,
                        conn=conn,
                        nodes=nodes,
                        sites=sites,
                        doc=doc,
                        keys=keys,
                        session_states=session_states,
                        apply_keys=apply_keys,
                        manual_keys=manual_keys,
                        manual_jobs=manual_jobs,
                        do_poll=do_poll,
                        do_apply=do_apply,
                        attempt_counts=attempt_counts,
                    )
                    continue
                unit_force_apply = args.force or job == "push"
                if args.force:
                    unit_policy = PollPolicy(
                        force=True,
                        live_only=False,
                        force_groups=frozenset(args.group or ()),
                        min_interval=args.min_interval,
                    )
                elif job == "pull":
                    unit_policy = PollPolicy(
                        force=False,
                        force_groups=frozenset(GET_GROUP_ORDER),
                        min_interval=args.min_interval,
                    )
                elif job == "refresh":
                    unit_policy = PollPolicy(
                        force=False,
                        force_groups=frozenset(refresh_due_groups()),
                        min_interval=args.min_interval,
                    )
                else:
                    unit_policy = PollPolicy(
                        force=False,
                        live_only=args.live,
                        force_groups=frozenset(args.group or ()),
                        min_interval=args.min_interval,
                    )
                attempt_counts[target.key] = attempt_counts.get(target.key, 0) + 1
                n = attempt_counts[target.key]
                if args.max_attempts and n > args.max_attempts:
                    print(f"[{target.key}] gave up after {args.max_attempts} attempts")
                    continue
                prefix = f"[{n}] " if retry_mode and n > 1 else ""
                print(f"{prefix}{target_label(target)} …", flush=True)
                node_record = nodes.get(target.key) or {}
                apply_due_now = job == "push" or apply_is_due(
                    conn,
                    target.key,
                    node_record,
                    sites,
                    force=unit_force_apply,
                    doc=doc,
                    keys=keys,
                )
                if not args.quiet:
                    if do_poll and target.due_groups:
                        need, skip_plan = format_get_plan(
                            conn,
                            target.key,
                            target.due_groups,
                            policy=unit_policy,
                            now=int(time.time()),
                        )
                        print(f"  poll need: {need}")
                        print(f"  poll skip: {skip_plan}")
                    if do_apply and apply_due_now:
                        apply_need, apply_have = format_apply_plan(
                            conn,
                            target.key,
                            node_record,
                            sites,
                            force=unit_force_apply,
                            doc=doc,
                            keys=keys,
                        )
                        print(f"  apply need: {apply_need}")
                        print(f"  apply skip: {apply_have}")
                    elif do_apply:
                        _, apply_have = format_apply_plan(
                            conn,
                            target.key,
                            node_record,
                            sites,
                            force=False,
                            doc=doc,
                            keys=keys,
                        )
                        print("  apply need: none")
                        print(f"  apply skip: {apply_have}")
                if not await session.ensure_companion_connected(log=log):
                    print("  companion disconnected (reconnect failed)")
                    next_pending.append(target)
                    await _drain_manual_into(
                        web_ctx,
                        next_pending,
                        nodes_path=nodes_path,
                        conn=conn,
                        nodes=nodes,
                        sites=sites,
                        doc=doc,
                        keys=keys,
                        session_states=session_states,
                        apply_keys=apply_keys,
                        manual_keys=manual_keys,
                        manual_jobs=manual_jobs,
                        do_poll=do_poll,
                        do_apply=do_apply,
                        attempt_counts=attempt_counts,
                    )
                    continue
                guest_before = str(node_record.get("guest_password") or "")
                session_states[target.key] = {
                    "state": "polling",
                    "due_groups": list(target.due_groups),
                    "apply": apply_due_now,
                    "job": job,
                }
                if web_ctx:
                    active_poll = {
                        "phase": "polling",
                        "accepting": True,
                        "round": round_num,
                        "unit": target.key,
                        "pending": len(pending),
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
                heard_acl = None
                fw = node_record.get("firmware_version")
                login_clock = None
                poll_ok = True
                if do_poll and target.due_groups:
                    res = await poll_one(
                        client,
                        target,
                        node=node_record,
                        due_groups=frozenset(target.due_groups),
                        cmd_timeout=args.timeout,
                        login_timeout=args.login_timeout,
                        attempts=args.attempts,
                        session=session,
                        log=log,
                        sites=sites,
                        doc=doc,
                        keys=keys,
                        discover_wait=args.discover_wait,
                        skip_discover=args.no_discover,
                    )
                    if not res.ok:
                        session_states[target.key] = {
                            "state": "unreachable",
                            "error": res.error,
                            "due_groups": list(target.due_groups),
                        }
                        print(f"  unreachable: {res.error}")
                        next_pending.append(target)
                        if args.retry_delay > 0:
                            await asyncio.sleep(args.retry_delay)
                        if web_ctx:
                            await web_ctx.publish_unit(
                                target.key,
                                session=session_states[target.key],
                                session_states=session_states,
                                companion=companion_short,
                                poll={
                                    "phase": "polling",
                                    "accepting": True,
                                    "round": round_num,
                                    "pending": len(pending),
                                    "total": total,
                                    "companion": companion_short,
                                },
                            )
                        await _drain_manual_into(
                            web_ctx,
                            next_pending,
                            nodes_path=nodes_path,
                            conn=conn,
                            nodes=nodes,
                            sites=sites,
                            doc=doc,
                            keys=keys,
                            session_states=session_states,
                            apply_keys=apply_keys,
                            manual_keys=manual_keys,
                            manual_jobs=manual_jobs,
                            do_poll=do_poll,
                            do_apply=do_apply,
                            attempt_counts=attempt_counts,
                        )
                        continue
                    record_poll(conn, unit=target.key, res=res)
                    heard_acl = res.acl
                    fw = res.firmware_version or fw
                    login_clock = res.node_clock
                    remaining = gaps_from_poll(res)
                    target.due_groups = remaining
                    print(f"  OK {poll_summary(res)}")
                    poll_ok = not remaining
                sync_paused(nodes_path, nodes)
                if is_paused(nodes.get(target.key)) and target.key not in manual_keys:
                    apply_keys.discard(target.key)
                    session_states[target.key] = {"state": "paused"}
                    print("  paused — skip apply")
                    if web_ctx:
                        await web_ctx.publish_unit(
                            target.key,
                            session=session_states[target.key],
                            session_states=session_states,
                            companion=companion_short,
                            poll=web_ctx._poll_state,
                        )
                    await _drain_manual_into(
                        web_ctx,
                        next_pending,
                        nodes_path=nodes_path,
                        conn=conn,
                        nodes=nodes,
                        sites=sites,
                        doc=doc,
                        keys=keys,
                        session_states=session_states,
                        apply_keys=apply_keys,
                        manual_keys=manual_keys,
                        manual_jobs=manual_jobs,
                        do_poll=do_poll,
                        do_apply=do_apply,
                        attempt_counts=attempt_counts,
                    )
                    continue
                apply_ok = True
                if do_apply and apply_due_now:
                    apply_clock = login_clock
                    if apply_clock is None:
                        ok, err, apply_clock = await admin_login(
                            client,
                            target,
                            login_timeout=args.login_timeout,
                            attempts=args.attempts,
                            session=session,
                            log=log,
                        )
                        if not ok:
                            print(f"  unreachable: {err or 'apply login failed'}")
                            apply_ok = False
                    if apply_ok:
                        apply_ok = await apply_one(
                            client,
                            target,
                            node=node_record,
                            doc=doc,
                            sites=sites,
                            cmd_timeout=args.timeout,
                            attempts=args.attempts,
                            session=session,
                            log=log,
                            conn=conn,
                            heard_acl=heard_acl,
                            firmware_version=fw,
                            login_clock=apply_clock,
                            keys=keys,
                            force=unit_force_apply,
                        )
                    if str(node_record.get("guest_password") or "") != guest_before:
                        yaml_dirty = True
                    if apply_ok:
                        apply_keys.discard(target.key)
                job_done = poll_ok and (not apply_due_now or apply_ok)
                if job_done:
                    manual_keys.discard(target.key)
                    manual_jobs.pop(target.key, None)
                if poll_ok and apply_ok:
                    succeeded[target.key] = True
                    session_states[target.key] = {"state": "ok", "due_groups": list(target.due_groups)}
                else:
                    session_states[target.key] = {
                        "state": "unreachable" if not poll_ok else "ok",
                        "due_groups": list(target.due_groups),
                        "apply": not apply_ok,
                    }
                    next_pending.append(target)
                if web_ctx:
                    await web_ctx.publish_unit(
                        target.key,
                        session=session_states[target.key],
                        session_states=session_states,
                        companion=companion_short,
                        poll={
                            "phase": "polling",
                            "accepting": True,
                            "round": round_num,
                            "pending": len(pending),
                            "total": total,
                            "companion": companion_short,
                        },
                    )
                await _drain_manual_into(
                    web_ctx,
                    next_pending,
                    nodes_path=nodes_path,
                    conn=conn,
                    nodes=nodes,
                    sites=sites,
                    doc=doc,
                    keys=keys,
                    session_states=session_states,
                    apply_keys=apply_keys,
                    manual_keys=manual_keys,
                    manual_jobs=manual_jobs,
                    do_poll=do_poll,
                    do_apply=do_apply,
                    attempt_counts=attempt_counts,
                )
            pending, _ = _drop_auto_paused(
                next_pending,
                nodes_path=nodes_path,
                nodes=nodes,
                manual_keys=manual_keys,
                session_states=session_states,
            )
            pending = sorted(pending, key=poll_staleness_key)
            if not pending or args.once:
                pending = []
                if args.once:
                    break
                continue
            if args.round_delay > 0:
                print(f"Waiting {args.round_delay:.0f}s before retrying {len(pending)} node(s) …")
                await asyncio.sleep(args.round_delay)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — keeping progress from successful units.")
    finally:
        if web_ctx:
            web_ctx.set_worker_active(False)
        await client.stop_auto_message_fetching()
        await client.disconnect()
        if yaml_dirty:
            persist_guest_if_new(nodes_path, doc)

    ok_count = len(succeeded)
    print(f"fleet {ok_count}/{total} router(s)")
    if pending:
        print(f"Still pending ({len(pending)}): {', '.join(t.unit_id for t in pending)}")
    poll_exit = 130 if interrupted else (0 if not pending else 2)
    if web_ctx:
        await web_ctx.publish_session(
            {
                "phase": "done",
                "ok": ok_count,
                "pending": len(pending),
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
    parser.add_argument("--retry-delay", type=float, default=0.0)
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
        default=NEIGHBOR_DISCOVER_WAIT_S,
        metavar="SEC",
        help="Wait after discover.neighbors before GET (default 12)",
    )
    parser.add_argument("--group", action="append", choices=GET_GROUP_ORDER)
    parser.add_argument("--poll-only", action="store_true", help="GET only; do not apply")
    parser.add_argument("--apply-only", action="store_true", help="SET only; skip GET cadence")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--web-only", action="store_true")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    if args.probe:
        return asyncio.run(probe_only(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
