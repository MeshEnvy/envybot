#!/usr/bin/env python3
"""Import book contacts onto a companion and record this tag in keys.yaml."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from envybot.channels_doc import (
    DEFAULT_MAX_CHANNELS,
    ChannelsError,
    channels_path,
    heard_slot_from_payload,
    load_channels,
    plan_channel_ops,
    resolve_person_channels,
)
from envybot.keys_doc import (
    HEX_PUBKEY_RE,
    TrustError,
    TrustPolicy,
    add_book_role,
    apply_node_override,
    find_person_for_pubkey,
    keys_path,
    load_keys,
    parse_trust_policy,
    person_has_key,
    remember_person_key,
    write_keys,
)
from envybot.apply import profile_id, stamp_profile_after_trust
from envybot.history import migrate_legacy
from envybot.book_dal import load_book
from envybot.nodes_doc import UNIT_NUM_RE, load_nodes_doc, load_sites_for_book, write_nodes_doc
from envybot.position import lookup_site_name, public_radio_name, resolve_book_position, site_binding
from envybot.public_advert import resolve_public_apply_position
from envybot.radio import (
    CONTACT_FLAG_FAVORITE,
    CONTACT_TYPE_REPEATER,
    _default_out_path_fields,
    FleetSession,
    PollLog,
    RouterTarget,
    add_companion_args,
    admin_login,
    companion_identity,
    connect,
    ensure_companion_identity,
    drop_companion_contact,
    ensure_contact_favorited,
    forget_replaced_identities,
    load_targets,
)
from envybot.selector import format_candidates, normalize_adv_name, resolve_selector

try:
    from meshcore import EventType, MeshCore
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "meshcore not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot trust"
    ) from exc


def contact_adv_name(
    node: dict[str, Any],
    unit_id: str,
    sites: dict[str, dict[str, Any]] | None = None,
    *,
    key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> str:
    """Site advert name when bound; otherwise unit_id (bench / no site)."""
    bind = site_binding(key, node, sites)
    if bind and doc is not None:
        derived = public_radio_name(key, node, sites, doc=doc)
    else:
        derived = public_radio_name(key, node, sites)
    if derived:
        return derived
    return unit_id[:32]


def contact_payload(
    target: RouterTarget,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]],
    *,
    doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bind = site_binding(target.key, node, sites)
    if bind and doc is not None:
        pos = resolve_public_apply_position(node, sites, key=target.key, doc=doc)
    else:
        pos = resolve_book_position(node, sites, key=target.key)
    name = contact_adv_name(node, target.unit_id, sites, key=target.key, doc=doc)
    lat = float(pos["lat"]) if pos else 0.0
    lon = float(pos["lon"]) if pos else 0.0
    return {
        "public_key": target.pubkey_hex.lower(),
        "type": CONTACT_TYPE_REPEATER,
        "flags": CONTACT_FLAG_FAVORITE,
        **_default_out_path_fields(target),
        "adv_name": name,
        "last_advert": 0,
        "adv_lat": lat,
        "adv_lon": lon,
        "unit_id": target.unit_id,
        "site": target.site,
    }


def build_trust_rows(
    nodes_path: Path,
    *,
    include: set[str] | None = None,
) -> list[dict[str, Any]]:
    """All pollable MeshCore units, including bag/bench (no site)."""
    book = load_book(nodes_path)
    targets = load_targets(
        nodes_path, deployed_only=False, include=include, skip=None
    )
    return [
        contact_payload(t, book.nodes.get(t.key) or {}, book.sites, doc=book.doc)
        for t in targets
    ]


def write_export(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _coord(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def contact_needs_replace(existing: dict[str, Any], stub: dict[str, Any]) -> bool:
    """True when the stored advert name or loc is not the book value."""
    if normalize_adv_name(str(existing.get("adv_name") or "")) != normalize_adv_name(
        str(stub.get("adv_name") or "")
    ):
        return True
    if abs(_coord(existing.get("adv_lat")) - _coord(stub.get("adv_lat"))) > 1e-4:
        return True
    if abs(_coord(existing.get("adv_lon")) - _coord(stub.get("adv_lon"))) > 1e-4:
        return True
    return False


async def push_contacts(
    client: MeshCore,
    rows: list[dict[str, Any]],
    *,
    log: PollLog,
) -> int:
    prev_auto = client.auto_update_contacts
    client.auto_update_contacts = False
    await client.ensure_contacts(follow=True)
    now = int(time.time())
    try:
        return await _push_contact_rows(client, rows, log=log, now=now)
    finally:
        client.auto_update_contacts = prev_auto


async def _push_contact_rows(
    client: MeshCore,
    rows: list[dict[str, Any]],
    *,
    log: PollLog,
    now: int,
) -> int:
    ok = 0
    for row in rows:
        stub = {k: v for k, v in row.items() if k not in ("unit_id", "site")}
        stub["last_advert"] = now
        await forget_replaced_identities(
            client,
            keep_pubkey=row["public_key"],
            names=[str(row.get("adv_name") or ""), row["unit_id"]],
            unit_id=row["unit_id"],
            log=log,
        )
        existing = client.get_contact_by_key_prefix(row["public_key"][:12])
        if existing and not contact_needs_replace(existing, stub):
            await ensure_contact_favorited(client, existing, log=log)
            log.step(
                f"{row['unit_id']} {row.get('adv_name')} "
                f"{row['adv_lat']:.5f},{row['adv_lon']:.5f}"
            )
            ok += 1
            continue
        if existing and not await drop_companion_contact(
            client, row["public_key"], log=log, unit_id=row["unit_id"]
        ):
            continue
        res = await client.commands.add_contact(stub)
        if res.type == EventType.ERROR:
            log.step(f"{row['unit_id']}: add failed ({res.payload})")
            continue
        pk = row["public_key"].lower()
        client.contacts[pk] = stub
        await ensure_contact_favorited(client, stub, log=log)
        log.step(
            f"{row['unit_id']} {row.get('adv_name')} "
            f"{row['adv_lat']:.5f},{row['adv_lon']:.5f}"
        )
        ok += 1
    return ok


def persist_trust_policy(
    nodes_path: Path,
    policy: TrustPolicy,
    pubkey: str,
) -> tuple[bool, bool]:
    """Write keys.yaml + book/node trust. Returns (key_added, yaml_changed)."""
    kpath = keys_path(nodes_path)
    keys = load_keys(kpath)
    key_added = remember_person_key(keys, policy.person, pubkey)
    write_keys(kpath, keys)
    doc = load_nodes_doc(nodes_path)
    changed = add_book_role(doc, policy.person, policy.fleet_role)
    sites = load_sites_for_book(nodes_path)
    for selector, role in policy.overrides:
        resolved = resolve_selector(doc, selector, sites)
        if resolved.error or resolved.target is None:
            extra = ""
            if resolved.candidates:
                extra = "\n" + format_candidates(resolved.candidates)
            raise TrustError((resolved.error or f"unknown selector {selector!r}") + extra)
        node = (doc.get("nodes") or {}).get(resolved.target.key)
        if not isinstance(node, dict):
            raise TrustError(f"unknown unit {resolved.target.key}")
        if apply_node_override(doc, node, policy.person, role):
            changed = True
    if changed:
        write_nodes_doc(nodes_path, doc)
    return key_added, key_added or changed


async def grant_companion_acl(
    client: MeshCore,
    targets: list[RouterTarget],
    *,
    login_timeout: float,
    attempts: int,
    log: PollLog,
    conn: sqlite3.Connection | None = None,
) -> tuple[int, int, list[str]]:
    session = FleetSession()
    session.conn = conn
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)
    ok = 0
    fail = 0
    ok_keys: list[str] = []
    for target in targets:
        print(f"{target.unit_id} …", flush=True)
        logged, err, _clock = await admin_login(
            client,
            target,
            login_timeout=login_timeout,
            attempts=attempts,
            session=session,
            log=log,
        )
        if logged:
            ok += 1
            ok_keys.append(target.key)
        else:
            fail += 1
            log.step(err or "login failed")
    return ok, fail, ok_keys


async def _fetch_channel_slots(client: MeshCore, max_channels: int) -> list:
    from envybot.channels_doc import ChannelSlot

    slots: list[ChannelSlot] = []
    for idx in range(max_channels):
        res = await client.commands.get_channel(idx)
        if res.type == EventType.CHANNEL_INFO and isinstance(res.payload, dict):
            slots.append(heard_slot_from_payload(idx, res.payload))
    return slots


async def push_channels(
    client: MeshCore,
    nodes_path: Path,
    *,
    person: str | None,
    log: PollLog,
) -> tuple[int, int, bool]:
    """Apply channels.yaml grants. Returns (satisfied, want, all_ok)."""
    cpath = channels_path(nodes_path)
    if not cpath.is_file():
        return 0, 0, True
    try:
        catalog = load_channels(cpath)
    except ChannelsError as exc:
        raise TrustError(str(exc)) from exc
    want = resolve_person_channels(catalog, person)
    if not want:
        return 0, 0, True

    res = await client.commands.send_device_query()
    max_channels = DEFAULT_MAX_CHANNELS
    if res.type == EventType.DEVICE_INFO and isinstance(res.payload, dict):
        max_channels = int(res.payload.get("max_channels") or DEFAULT_MAX_CHANNELS)

    heard = await _fetch_channel_slots(client, max_channels)
    ops, planner_failures = plan_channel_ops(want, heard)
    set_fail = 0
    for op in ops:
        set_res = await client.commands.set_channel(op.idx, op.name, op.secret)
        if set_res.type == EventType.ERROR:
            log.step(f"{op.label}: set failed ({set_res.payload})")
            set_fail += 1
            continue
        log.step(f"{op.label} slot {op.idx}")
    for name in planner_failures:
        log.step(f"channel {name}: table full")

    fail_count = len(planner_failures) + set_fail
    satisfied = len(want) - fail_count
    return satisfied, len(want), fail_count == 0


def resolve_channel_person(
    nodes_path: Path,
    client: MeshCore,
    policy: TrustPolicy | None,
) -> str | None:
    if policy is not None:
        return policy.person
    pubkey = companion_identity(client)
    if not pubkey or not HEX_PUBKEY_RE.match(pubkey.strip().lower()):
        return None
    return find_person_for_pubkey(load_keys(keys_path(nodes_path)), pubkey.strip().lower())


async def run(args: argparse.Namespace) -> int:
    nodes_path: Path = args.nodes
    grant_include = {u.lower() for u in args.unit} if args.unit else None
    rows = build_trust_rows(nodes_path)
    if args.export:
        write_export(args.export, rows)
        print(f"wrote {len(rows)} contact(s) to {args.export}")
        if args.export_only:
            return 0
    if not rows:
        print("No pollable MeshCore units.", file=sys.stderr)
        return 1
    policy: TrustPolicy | None = None
    if args.policy:
        try:
            policy = parse_trust_policy(args.policy)
        except TrustError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if UNIT_NUM_RE.match(policy.person):
            print(
                f"person name {policy.person!r} looks like a unit key. "
                "Use --unit me0041 to scope the mesh grant.",
                file=sys.stderr,
            )
            return 1

    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    client = await connect(args)
    try:
        n = await push_contacts(client, rows, log=log)
        print(f"trusted {n}/{len(rows)} contact(s) on companion")
        contact_ok = n == len(rows)

        await ensure_companion_identity(client)
        try:
            channel_person = resolve_channel_person(nodes_path, client, policy)
            ch_ok, ch_want, channel_ok = await push_channels(
                client,
                nodes_path,
                person=channel_person,
                log=log,
            )
        except TrustError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if ch_want:
            print(f"channels {ch_ok}/{ch_want} grant(s)")

        if policy is None:
            return 0 if contact_ok and channel_ok else 2

        pubkey = companion_identity(client)
        if not pubkey or not HEX_PUBKEY_RE.match(pubkey.strip().lower()):
            print(
                "Companion SELF_INFO missing 64-hex public_key; "
                "wrote contacts only.",
                file=sys.stderr,
            )
            return 2
        pubkey = pubkey.strip().lower()
        keys_before = load_keys(keys_path(nodes_path))
        already = person_has_key(keys_before, policy.person, pubkey)

        grant_targets = load_targets(
            nodes_path, deployed_only=False, include=grant_include, skip=None
        )
        doc_before = load_nodes_doc(nodes_path)
        sites = load_sites_for_book(nodes_path)
        nodes_before = doc_before.get("nodes") or {}
        pre_hashes: dict[str, str] = {}
        for target in grant_targets:
            node = nodes_before.get(target.key) or {}
            pre_hashes[target.key] = profile_id(
                node, sites, doc=doc_before, keys=keys_before
            )

        try:
            persist_trust_policy(nodes_path, policy, pubkey)
        except TrustError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"keys.yaml {policy.person} {pubkey[:12]}…")

        do_login = policy.fleet_role == "admin" and (
            args.force or not already or bool(args.unit)
        )
        if not do_login:
            if policy.fleet_role != "admin":
                print("skip ACL login (guest grant; run fleet apply from an admin tag)")
            else:
                print("skip ACL login (key already in keys.yaml)")
            return 0 if contact_ok and channel_ok else 2

        if not grant_targets:
            print("No pollable MeshCore units for ACL login.", file=sys.stderr)
            return 2
        doc_after = load_nodes_doc(nodes_path)
        keys_after = load_keys(keys_path(nodes_path))
        conn = migrate_legacy(nodes_path.parent, doc_after.get("nodes") or {})
        acl_ok, acl_fail, ok_keys = await grant_companion_acl(
            client,
            grant_targets,
            login_timeout=args.login_timeout,
            attempts=args.attempts,
            log=log,
            conn=conn,
        )
        nodes_after = doc_after.get("nodes") or {}
        for key in ok_keys:
            node = nodes_after.get(key) or {}
            if stamp_profile_after_trust(
                conn,
                key,
                node,
                sites,
                pre_apply_hash=pre_hashes.get(key),
                doc=doc_after,
                keys=keys_after,
                doc_before=doc_before,
                keys_before=keys_before,
            ):
                log.step(f"{key}: profile reconciled")
        print(f"acl {acl_ok}/{acl_ok + acl_fail} login(s)")
        if acl_fail or not contact_ok or not channel_ok:
            return 2
        return 0
    finally:
        await client.stop_auto_message_fetching()
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    add_companion_args(parser)
    parser.add_argument(
        "policy",
        nargs="*",
        help="person[:role] and selector:role (omit for contacts only)",
    )
    parser.add_argument(
        "--unit",
        action="append",
        metavar="me0003",
        help="Scope ACL login only (contacts still import the full book)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Password-login even if this key is already in keys.yaml",
    )
    parser.add_argument("--export", type=Path, metavar="FILE", help="Write JSON contact list")
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Write --export and exit (no companion)",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.export_only and not args.export:
        parser.error("--export-only requires --export")
    if args.export_only:
        return asyncio.run(run(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
