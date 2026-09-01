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
from envybot.nodes_doc import load_nodes_doc, load_sites_for_book, migrate_desired, write_nodes_doc
from envybot.poll import (
    GET_GROUP_ORDER,
    PollPolicy,
    due_groups,
    format_get_plan,
    format_interval,
    gaps_from_poll,
    partition_due,
    poll_one,
    poll_summary,
)
from envybot.radio import (
    DEFAULT_MESH_ATTEMPTS,
    DEFAULT_MIN_POLL_INTERVAL,
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
    if skip and not args.quiet:
        print(f"Excluding {len(skip)} unit(s): {', '.join(sorted(skip))}")
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
        targets, skipped = partition_due(all_targets, conn, policy=policy, now=now)
    else:
        targets, skipped = list(all_targets), []
        for t in targets:
            t.due_groups = []
    apply_due = [
        t
        for t in all_targets
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
    work = [t for t in all_targets if t.key in work_keys]
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
    if not work:
        print(f"All {len(all_targets)} router(s) up to date.")
        if web_ctx:
            await web_ctx.publish_session(
                {"phase": "done", "pending": 0, "ok": 0, "total": len(all_targets)}
            )
            return await _serve_web_until_stop(web_ctx, 0)
        return 0

    retry_mode = not args.once
    print(
        f"Fleet {len(work)} router(s)"
        + (" — retries until all succeed (Ctrl+C to stop) …" if retry_mode else " — single pass …")
    )

    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    session = FleetSession()
    client = await connect(args)
    companion_label = companion_identity(client)
    companion_short = companion_label[:12] if companion_label else None
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)
    session.enable_companion_recovery(client, all_targets)
    await sync_fleet_contacts(client, work, log=log)
    pending = list(work)
    succeeded: dict[str, bool] = {}
    attempt_counts: dict[str, int] = {}
    round_num = 0
    interrupted = False
    yaml_dirty = False

    try:
        while pending:
            round_num += 1
            if args.max_rounds and round_num > args.max_rounds:
                break
            if retry_mode and round_num > 1:
                print(f"\n--- round {round_num}: {len(pending)} still pending ---")
            next_pending = []
            for target in pending:
                attempt_counts[target.key] = attempt_counts.get(target.key, 0) + 1
                n = attempt_counts[target.key]
                if args.max_attempts and n > args.max_attempts:
                    print(f"[{target.key}] gave up after {args.max_attempts} attempts")
                    continue
                prefix = f"[{n}] " if retry_mode and n > 1 else ""
                print(f"{prefix}{target_label(target)} …", flush=True)
                if not args.quiet:
                    if do_poll and target.due_groups:
                        need, skip_plan = format_get_plan(
                            conn,
                            target.key,
                            target.due_groups,
                            policy=policy,
                            now=int(time.time()),
                        )
                        print(f"  poll need: {need}")
                        print(f"  poll skip: {skip_plan}")
                    if target.key in apply_keys:
                        apply_need, apply_have = format_apply_plan(
                            conn,
                            target.key,
                            nodes.get(target.key) or {},
                            sites,
                            force=args.force,
                            doc=doc,
                            keys=keys,
                        )
                        print(f"  apply need: {apply_need}")
                        print(f"  apply skip: {apply_have}")
                if not await session.ensure_companion_connected(log=log):
                    print("  companion disconnected (reconnect failed)")
                    next_pending.append(target)
                    continue
                node_record = nodes.get(target.key) or {}
                guest_before = str(node_record.get("guest_password") or "")
                session_states[target.key] = {
                    "state": "polling",
                    "due_groups": list(target.due_groups),
                    "apply": target.key in apply_keys,
                }
                if web_ctx:
                    await web_ctx.publish_unit(
                        target.key,
                        session=session_states[target.key],
                        session_states=session_states,
                        companion=companion_short,
                        poll={
                            "phase": "polling",
                            "round": round_num,
                            "unit": target.key,
                            "pending": len(pending),
                            "total": len(work),
                        },
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
                                    "round": round_num,
                                    "pending": len(pending),
                                    "total": len(work),
                                },
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
                apply_ok = True
                if do_apply and target.key in apply_keys:
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
                            force=args.force,
                        )
                    if str(node_record.get("guest_password") or "") != guest_before:
                        yaml_dirty = True
                    if apply_ok:
                        apply_keys.discard(target.key)
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
                            "round": round_num,
                            "pending": len(pending),
                            "total": len(work),
                        },
                    )
            pending = sorted(next_pending, key=poll_staleness_key)
            if not pending or args.once:
                break
            if args.round_delay > 0:
                print(f"Waiting {args.round_delay:.0f}s before retrying {len(pending)} node(s) …")
                await asyncio.sleep(args.round_delay)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — keeping progress from successful units.")
    finally:
        await client.stop_auto_message_fetching()
        await client.disconnect()
        if yaml_dirty:
            persist_guest_if_new(nodes_path, doc)

    total = len(work)
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
    parser.add_argument("--force", action="store_true", help="Poll every GET group; re-SET profile")
    parser.add_argument("--live", action="store_true", help="Periodic GET groups only")
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
