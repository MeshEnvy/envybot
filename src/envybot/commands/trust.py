#!/usr/bin/env python3
"""Import site name + resolved loc + pubkey onto a companion as contacts/favorites."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from envybot.nodes_doc import load_nodes_doc, load_sites_for_book
from envybot.position import resolve_book_position, site_binding
from envybot.web.snapshot import lookup_site_name
from envybot.radio import (
    CONTACT_FLAG_FAVORITE,
    CONTACT_TYPE_REPEATER,
    PollLog,
    RouterTarget,
    add_companion_args,
    connect,
    ensure_contact_favorited,
    load_targets,
)

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
) -> str:
    """Site name when bound; otherwise unit_id (bench / no site)."""
    bind = site_binding(key, node, sites)
    if bind:
        site_name = lookup_site_name(bind[0], sites or {})
        if site_name:
            return site_name[:32]
    return unit_id[:32]


def contact_payload(
    target: RouterTarget,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pos = resolve_book_position(node, sites, key=target.key)
    name = contact_adv_name(node, target.unit_id, sites, key=target.key)
    lat = float(pos["lat"]) if pos else 0.0
    lon = float(pos["lon"]) if pos else 0.0
    return {
        "public_key": target.pubkey_hex.lower(),
        "type": CONTACT_TYPE_REPEATER,
        "flags": CONTACT_FLAG_FAVORITE,
        "out_path_len": -1,
        "out_path_hash_mode": -1,
        "out_path": "",
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
    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    sites = load_sites_for_book(nodes_path)
    targets = load_targets(
        nodes_path, deployed_only=False, include=include, skip=None
    )
    return [contact_payload(t, nodes.get(t.key) or {}, sites) for t in targets]


def write_export(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


async def push_contacts(
    client: MeshCore,
    rows: list[dict[str, Any]],
    *,
    log: PollLog,
) -> int:
    ok = 0
    await client.ensure_contacts(follow=True)
    for row in rows:
        stub = {k: v for k, v in row.items() if k not in ("unit_id", "site")}
        existing = client.get_contact_by_key_prefix(row["public_key"][:12])
        if existing:
            updated = dict(existing)
            updated["adv_name"] = stub["adv_name"]
            updated["adv_lat"] = stub["adv_lat"]
            updated["adv_lon"] = stub["adv_lon"]
            updated["flags"] = existing.get("flags", 0) | CONTACT_FLAG_FAVORITE
            res = await client.commands.add_contact(updated)
            if res.type == EventType.ERROR:
                log.step(f"{row['unit_id']}: update failed ({res.payload})")
                continue
            await ensure_contact_favorited(client, updated, log=log)
        else:
            res = await client.commands.add_contact(stub)
            if res.type == EventType.ERROR:
                log.step(f"{row['unit_id']}: add failed ({res.payload})")
                continue
        log.step(
            f"{row['unit_id']} {row.get('adv_name')} "
            f"{row['adv_lat']:.5f},{row['adv_lon']:.5f}"
        )
        ok += 1
    return ok


async def run(args: argparse.Namespace) -> int:
    nodes_path: Path = args.nodes
    include = {u.lower() for u in args.unit} if args.unit else None
    rows = build_trust_rows(nodes_path, include=include)
    if args.export:
        write_export(args.export, rows)
        print(f"wrote {len(rows)} contact(s) to {args.export}")
        if args.export_only:
            return 0
    if not rows:
        print("No pollable MeshCore units.", file=sys.stderr)
        return 1
    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    client = await connect(args)
    try:
        n = await push_contacts(client, rows, log=log)
        print(f"trusted {n}/{len(rows)} contact(s) on companion")
        return 0 if n == len(rows) else 2
    finally:
        await client.stop_auto_message_fetching()
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    add_companion_args(parser)
    parser.add_argument("--unit", action="append", metavar="me0003")
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
