"""SET desired radio profile. Private mask unless public: true."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from envybot.history import get_last_seen, insert_apply, last_ok_apply
from envybot.keys_doc import (
    UnknownPerson,
    grants_payload,
    plan_acl_ops,
    resolve_node_acl,
)
from envybot.nodes_doc import (
    MASK_NAME,
    is_public,
    write_nodes_doc,
)
from envybot.passwords import (
    assign_guest_password,
    guest_needs_assign,
    normalize_password,
    password_is_strong,
    password_token,
)
from envybot.position import resolve_book_position
from envybot.radio import (
    FLEET_DUTYCYCLE_PCT,
    FLEET_PATH_HASH_MODE,
    FleetSession,
    PollLog,
    RouterTarget,
    cli_error_reply,
    cli_set_ok,
    cli_admin_password_ok,
    maybe_sync_repeater_clock,
    mesh_wait_seconds,
    normalize_acl_payload,
    reset_to_flood,
    retry_binary_req,
    send_cmd_sync,
    set_book_coord,
    set_dutycycle_policy,
    set_path_hash_policy,
)

try:
    from meshcore import MeshCore
except ImportError:  # pragma: no cover
    MeshCore = Any  # type: ignore[misc,assignment]

PROFILE_ID_VERSION = 1


def desired_path_hash_mode(node: dict[str, Any]) -> int:
    val = node.get("path_hash_mode")
    if val is None or val == "":
        return FLEET_PATH_HASH_MODE
    try:
        return int(val)
    except (TypeError, ValueError):
        return FLEET_PATH_HASH_MODE


def desired_dutycycle(node: dict[str, Any]) -> int:
    val = node.get("dutycycle")
    if val is None or val == "":
        return int(FLEET_DUTYCYCLE_PCT)
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return int(FLEET_DUTYCYCLE_PCT)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def profile_parts(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Canonical desired SET payload. Secrets are tokens, not plaintext."""
    public = is_public(node)
    if public:
        name = str(node.get("name") or "").strip() or MASK_NAME
        pos = resolve_book_position(node, sites)
        lat = round(float(pos["lat"]), 6) if pos else None
        lon = round(float(pos["lon"]), 6) if pos else None
        advert = _opt_int(node.get("advert_interval_min"))
        flood = _opt_int(node.get("flood_advert_interval_h"))
    else:
        name = MASK_NAME
        lat, lon = 0.0, 0.0
        advert, flood = 0, 0
    pk = str(node.get("identity_pubkey") or "").strip().lower()
    try:
        grants = resolve_node_acl(doc or {}, node, keys or {})
    except UnknownPerson:
        grants = []
    return {
        "acl": grants_payload(grants),
        "admin": password_token(node.get("admin_password")),
        "advert": advert,
        "dutycycle": desired_dutycycle(node),
        "flood": flood,
        "guest": password_token(node.get("guest_password")),
        "identity": pk or None,
        "lat": lat,
        "lon": lon,
        "name": name,
        "path_hash": desired_path_hash_mode(node),
        "public": public,
    }


def profile_id(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> str:
    raw = json.dumps(
        profile_parts(node, sites, doc=doc, keys=keys),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"v{PROFILE_ID_VERSION}:{digest}"


def apply_is_due(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    force: bool = False,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> bool:
    if force:
        return True
    desired = profile_id(node, sites, doc=doc, keys=keys)
    if last_ok_apply(conn, unit, "profile") != desired:
        return True
    if not is_public(node) and guest_needs_assign(node, doc or {}, unit):
        return True
    return False


def stamp_profile_after_trust(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    pre_apply_hash: str | None,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> bool:
    """After trust updated ACL on a fully-synced unit, stamp the new profile hash."""
    if not pre_apply_hash:
        return False
    if last_ok_apply(conn, unit, "profile") != pre_apply_hash:
        return False
    desired = profile_id(node, sites, doc=doc, keys=keys)
    insert_apply(conn, unit=unit, field="profile", desired=desired, ok=True)
    return True


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
    expected: str | None = None,
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
    if field == "admin":
        ok_reply = cli_admin_password_ok(raw, expected)
    else:
        ok_reply = cli_set_ok(raw)
    if cli_error_reply(raw) or not ok_reply:
        log.step(f"{field}: set failed ({raw.strip()[:40]})")
        return False
    log.step(f"{field} set OK")
    return True


def _ensure_guest_password(node: dict[str, Any], doc: dict[str, Any], key: str) -> str:
    return assign_guest_password(node, doc, key)


async def _apply_acl(
    client: MeshCore,
    target: RouterTarget,
    want: list[Any],
    heard: list[dict[str, Any]] | None,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None,
) -> bool:
    ok = True
    for op in plan_acl_ops(want, heard):
        if not await _set_cli(
            client,
            target,
            f"setperm {op.key} {op.perm}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field=op.label,
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
    keys: dict[str, list[str]] | None = None,
) -> bool:
    """SET mask or book identity plus shared radio policy. Returns True if profile OK."""
    ok = True
    public = is_public(node)

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
        if isinstance(guest, str) and guest.strip():
            guest = _ensure_guest_password(node, doc, target.key)
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
        guest = _ensure_guest_password(node, doc, target.key)
        if not await _set_cli(
            client, target, f"set guest.password {guest}",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="guest",
        ):
            ok = False

    admin = node.get("admin_password")
    if password_is_strong(admin):
        admin_pw = normalize_password(admin)
        if not await _set_cli(
            client, target, f"password {admin_pw}",
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session, field="admin",
            expected=admin_pw,
        ):
            ok = False

    if await set_path_hash_policy(
        client, target, cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        mode=desired_path_hash_mode(node),
    ) is None:
        ok = False
    if await set_dutycycle_policy(
        client, target, cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        firmware_version=firmware_version or node.get("firmware_version"),
        pct=float(desired_dutycycle(node)),
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

    try:
        want_acl = resolve_node_acl(doc, node, keys or {})
    except UnknownPerson as exc:
        log.step(f"acl: unknown person {exc}")
        want_acl = []
        ok = False
    if want_acl:
        if heard_acl is None:
            wait_cap = mesh_wait_seconds(6000, cap=cmd_timeout)

            async def flood_on_retry(_attempt: int) -> None:
                await reset_to_flood(client, target, log=log)

            acl_raw = await retry_binary_req(
                "GET_ACL",
                lambda dest_wait: client.commands.req_acl_sync(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                attempts=attempts,
                log=log,
                on_retry=flood_on_retry,
                session=session,
                target=target,
                wait_s=wait_cap,
                cap=cmd_timeout,
            )
            heard_acl = normalize_acl_payload(acl_raw)
        if not await _apply_acl(
            client, target, want_acl, heard_acl,
            cmd_timeout=cmd_timeout, attempts=attempts, log=log, session=session,
        ):
            ok = False

    desired = profile_id(node, sites, doc=doc, keys=keys)
    insert_apply(conn, unit=target.key, field="profile", desired=desired, ok=ok)
    log.step(f"profile {'OK' if ok else 'partial'} ({desired})")
    return ok


def persist_guest_if_new(nodes_path: Any, doc: dict[str, Any]) -> None:
    write_nodes_doc(nodes_path, doc)
