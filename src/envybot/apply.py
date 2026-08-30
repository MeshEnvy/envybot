"""SET desired radio profile. Private mask unless public: true."""

from __future__ import annotations

import secrets
import sqlite3
import string
import time
from typing import Any

from envybot.history import get_last_seen, insert_apply, last_ok_apply
from envybot.nodes_doc import (
    MASK_NAME,
    desired_acl_pubkeys,
    is_public,
    write_nodes_doc,
)
from envybot.position import is_placeholder_gps, resolve_book_position
from envybot.radio import (
    FleetSession,
    PollLog,
    RouterTarget,
    cli_error_reply,
    cli_set_ok,
    maybe_sync_repeater_clock,
    send_cmd_sync,
    set_book_coord,
    set_dutycycle_policy,
    set_path_hash_policy,
)

try:
    from meshcore import MeshCore
except ImportError:  # pragma: no cover
    MeshCore = Any  # type: ignore[misc,assignment]

PERM_ACL_ADMIN = 3
PW_LEN = 14
PW_ALPHABET = string.ascii_letters + string.digits + "%&@#*^$!"


def gen_password(length: int = PW_LEN) -> str:
    chars = [secrets.choice(string.ascii_letters)]
    chars.extend(secrets.choice(PW_ALPHABET) for _ in range(length - 1))
    return "".join(chars)


def profile_id(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
) -> str:
    if not is_public(node):
        return "private"
    name = str(node.get("name") or "").strip() or MASK_NAME
    pos = resolve_book_position(node, sites)
    if pos:
        return f"public:{name}:{pos['lat']:.5f}:{pos['lon']:.5f}"
    return f"public:{name}"


def _heard_leaks_private(seen: dict[str, Any] | None) -> bool:
    if not seen:
        return False
    name = seen.get("name_heard")
    if name and name != MASK_NAME:
        return True
    lat, lon = seen.get("lat_heard"), seen.get("lon_heard")
    try:
        if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
            return True
    except (TypeError, ValueError):
        pass
    if seen.get("advert_interval_min") not in (None, 0):
        return True
    if seen.get("flood_advert_interval_h") not in (None, 0):
        return True
    return False


def apply_is_due(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    force: bool = False,
) -> bool:
    if force:
        return True
    desired = profile_id(node, sites)
    if last_ok_apply(conn, unit, "profile") != desired:
        return True
    seen = get_last_seen(conn, unit)
    if is_public(node):
        if seen and seen.get("name_heard") and seen.get("name_heard") != node.get("name"):
            return True
        return False
    return _heard_leaks_private(seen)


async def _set_cli(
    client: MeshCore,
    target: RouterTarget,
    command: str,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None,
    field: str,
) -> bool:
    raw = await send_cmd_sync(
        client,
        target,
        command,
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        log.step(f"{field}: no response")
        return False
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"{field}: set failed ({raw.strip()[:40]})")
        return False
    log.step(f"{field} set OK")
    return True


def _ensure_guest_password(node: dict[str, Any]) -> str:
    existing = node.get("guest_password")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    pw = gen_password()
    node["guest_password"] = pw
    return pw


async def _apply_acl(
    client: MeshCore,
    target: RouterTarget,
    want: list[str],
    heard: list[dict[str, Any]] | None,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None,
) -> bool:
    ok = True
    want_l = [p.lower() for p in want]
    want_prefixes = {p[:12] for p in want_l if len(p) >= 12}
    for pk in want_l:
        if not await _set_cli(
            client,
            target,
            f"setperm {pk} {PERM_ACL_ADMIN}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field=f"acl {pk[:8]}",
        ):
            ok = False
    if not heard:
        return ok
    for entry in heard:
        key = str(entry.get("key") or "").strip().lower()
        if not key:
            continue
        prefix = key[:12] if len(key) >= 12 else key
        if prefix in want_prefixes or key in want_l:
            continue
        if not await _set_cli(
            client,
            target,
            f"setperm {key} 0",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field=f"acl drop {prefix}",
        ):
            ok = False
    return ok


async def apply_one(
    client: MeshCore,
    target: RouterTarget,
    *,
    node: dict[str, Any],
    doc: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    cmd_timeout: float,
    attempts: int,
    session: FleetSession | None,
    log: PollLog,
    conn: sqlite3.Connection,
    heard_acl: list[dict[str, Any]] | None = None,
    firmware_version: str | None = None,
    login_clock: int | None = None,
) -> bool:
    """SET mask or book identity plus shared radio policy. Returns True if profile OK."""
    ok = True
    public = is_public(node)
    desired = profile_id(node, sites)

    if public:
        name = str(node.get("name") or "").strip() or target.unit_id
        if not await _set_cli(
            client,
            target,
            f"set name {name}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field="name",
        ):
            ok = False
        pos = resolve_book_position(node, sites)
        if pos:
            if await set_book_coord(
                client, target, "lat", float(pos["lat"]),
                cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
            ) is None:
                ok = False
            if await set_book_coord(
                client, target, "lon", float(pos["lon"]),
                cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
            ) is None:
                ok = False
        if node.get("advert_interval_min") is not None:
            if not await _set_cli(
                client, target, f"set advert.interval {int(node['advert_interval_min'])}",
                cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="advert",
            ):
                ok = False
        if node.get("flood_advert_interval_h") is not None:
            if not await _set_cli(
                client, target, f"set flood.advert.interval {int(node['flood_advert_interval_h'])}",
                cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="flood_advert",
            ):
                ok = False
        guest = node.get("guest_password")
        if isinstance(guest, str) and guest:
            if not await _set_cli(
                client, target, f"set guest.password {guest}",
                cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="guest",
            ):
                ok = False
    else:
        if not await _set_cli(
            client, target, f"set name {MASK_NAME}",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="name",
        ):
            ok = False
        if await set_book_coord(
            client, target, "lat", 0.0,
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        ) is None:
            ok = False
        if await set_book_coord(
            client, target, "lon", 0.0,
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        ) is None:
            ok = False
        if not await _set_cli(
            client, target, "set advert.interval 0",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="advert",
        ):
            ok = False
        if not await _set_cli(
            client, target, "set flood.advert.interval 0",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="flood_advert",
        ):
            ok = False
        guest = _ensure_guest_password(node)
        if not await _set_cli(
            client, target, f"set guest.password {guest}",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="guest",
        ):
            ok = False

    if await set_path_hash_policy(
        client, target, cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
    ) is None:
        ok = False
    if await set_dutycycle_policy(
        client, target, cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        firmware_version=firmware_version or node.get("firmware_version"),
    ) is None:
        ok = False

    stored_clock = None
    seen = get_last_seen(conn, target.key)
    if seen and seen.get("node_clock") is not None:
        stored_clock = int(seen["node_clock"])
    await maybe_sync_repeater_clock(
        client,
        target,
        login_clock=login_clock,
        stored_clock=stored_clock,
        cmd_timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )

    want_acl = desired_acl_pubkeys(doc, node)
    if session is not None and session.companion_acl_prefix:
        prefix = session.companion_acl_prefix.lower()
        if not any(pk.startswith(prefix) for pk in want_acl):
            # Live companion may only be a 12-char prefix; still grant if we have full key later.
            pass
    if want_acl:
        if not await _apply_acl(
            client, target, want_acl, heard_acl,
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        ):
            ok = False

    insert_apply(conn, unit=target.key, field="profile", desired=desired, ok=ok)
    log.step(f"profile {'OK' if ok else 'partial'} ({desired})")
    return ok


def persist_guest_if_new(nodes_path: Any, doc: dict[str, Any]) -> None:
    write_nodes_doc(nodes_path, doc)
