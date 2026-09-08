"""SET desired radio profile from book (site name, GPS, adverts)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from envybot.history import (
    clear_apply_stamps,
    get_last_seen,
    insert_apply,
    last_ok_apply,
    stamp_apply,
)
from envybot.keys_doc import (
    UnknownPerson,
    grants_payload,
    plan_acl_ops,
    resolve_node_acl,
)
from envybot.nodes_doc import (
    MASK_NAME,
    sync_paused,
    write_nodes_doc,
)
from envybot.passwords import (
    assign_guest_password,
    guest_needs_assign,
    normalize_password,
    password_is_strong,
    password_token,
)
from envybot.position import public_radio_name, site_binding
from envybot.public_advert import (
    audit_apply_position,
    format_apply_name_log,
    format_apply_position_log,
    owner_info_cli_payload,
    owner_info_for_apply,
    resolve_public_apply_position,
)
from envybot.radio import (
    FLEET_DUTYCYCLE_PCT,
    FLEET_OTA_AUTOFETCH,
    FLEET_PATH_HASH_MODE,
    FleetSession,
    OtaAutofetchUnsupported,
    PollLog,
    RouterTarget,
    cli_error_reply,
    cli_set_ok,
    cli_admin_password_ok,
    maybe_sync_repeater_clock,
    mesh_wait_seconds,
    normalize_acl_payload,
    normalize_ota_autofetch,
    retry_binary_req,
    send_cmd_sync,
    set_book_coord,
    set_dutycycle_policy,
    set_fem_rxgain_policy,
    set_ota_autofetch_policy,
    set_path_hash_policy,
    set_powersaving_policy,
    set_rxgain_policy,
    FemRxgainUnsupported,
)

try:
    from meshcore import MeshCore
except ImportError:  # pragma: no cover
    MeshCore = Any  # type: ignore[misc,assignment]

PROFILE_ID_VERSION = 1

# ``radio.rxgain`` is stock MeshCore SX1262 boost. Off is a range footgun
# unless the book names a T096. Temporary off is firmware ``try``, not apply.
RXGAIN_BOARD_TOKENS = frozenset({"heltec-t096", "t096"})

APPLY_FIELDS = (
    "name",
    "lat",
    "lon",
    "advert",
    "flood",
    "guest",
    "admin",
    "owner",
    "repeat",
    "path_hash",
    "dutycycle",
    "ota_autofetch",
    "powersaving",
    "fem_rxgain",
    "rxgain",
    "acl",
    "identity",
)

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


def desired_ota_autofetch(node: dict[str, Any]) -> str:
    return normalize_ota_autofetch(node.get("ota_autofetch"))


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    return None


def desired_powersaving(node: dict[str, Any]) -> bool | None:
    """None unless the book sets ``powersaving``. No fleet default."""
    if "powersaving" not in node:
        return None
    return _parse_optional_bool(node.get("powersaving"))


def desired_fem_rxgain(node: dict[str, Any]) -> bool | None:
    """None unless the book sets ``fem_rxgain`` (alias ``radio.fem.rxgain``)."""
    if "fem_rxgain" in node:
        return _parse_optional_bool(node.get("fem_rxgain"))
    if "radio.fem.rxgain" in node:
        return _parse_optional_bool(node.get("radio.fem.rxgain"))
    return None


def desired_rxgain(node: dict[str, Any]) -> bool | None:
    """None unless the book sets ``rxgain`` (alias ``radio.rxgain``)."""
    if "rxgain" in node:
        return _parse_optional_bool(node.get("rxgain"))
    if "radio.rxgain" in node:
        return _parse_optional_bool(node.get("radio.rxgain"))
    return None


def node_board_token(node: dict[str, Any]) -> str:
    raw = node.get("board")
    if raw is None or raw == "":
        raw = node.get("hardware")
    if raw is None or raw == "":
        return ""
    return str(raw).strip().lower().replace("_", "-").replace(" ", "-")


def board_allows_rxgain(node: dict[str, Any]) -> bool:
    token = node_board_token(node)
    if token in RXGAIN_BOARD_TOKENS:
        return True
    return token.endswith("-t096") or "-t096-" in token


def rxgain_apply_enabled(node: dict[str, Any]) -> bool | None:
    """Value to SET, or None when apply must not touch ``radio.rxgain``."""
    want = desired_rxgain(node)
    if want is None or not board_allows_rxgain(node):
        return None
    return want


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def desired_repeat(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    key: str | None = None,
) -> bool:
    """Repeat/relay on when site-bound unless book overrides."""
    if "repeat" in node:
        parsed = _parse_optional_bool(node.get("repeat"))
        if parsed is not None:
            return parsed
    return site_binding(key, node, sites) is not None


def _profile_advert_intervals(
    node: dict[str, Any],
    *,
    site_bound: bool,
) -> tuple[int | None, int | None]:
    advert = _opt_int(node.get("advert_interval_min"))
    flood = _opt_int(node.get("flood_advert_interval_h"))
    if site_bound:
        return advert, flood
    if advert is None:
        advert = 0
    if flood is None:
        flood = 0
    return advert, flood


def profile_parts(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Canonical desired SET payload. Secrets are tokens, not plaintext."""
    bind = site_binding(key, node, sites)
    advert, flood = _profile_advert_intervals(node, site_bound=bind is not None)
    if bind:
        name = public_radio_name(key, node, sites, doc=doc) or MASK_NAME
        pos = resolve_public_apply_position(node, sites, key=key, doc=doc)
        lat = round(float(pos["lat"]), 6) if pos else None
        lon = round(float(pos["lon"]), 6) if pos else None
    else:
        name = MASK_NAME
        lat, lon = 0.0, 0.0
    pk = str(node.get("identity_pubkey") or "").strip().lower()
    try:
        grants = resolve_node_acl(doc or {}, node, keys or {})
    except UnknownPerson:
        grants = []
    parts: dict[str, Any] = {
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
        "ota_autofetch": desired_ota_autofetch(node),
        "owner": owner_info_for_apply(node, doc, key=key, sites=sites),
        "path_hash": desired_path_hash_mode(node),
        "repeat": desired_repeat(node, sites, key=key),
    }
    powersaving = desired_powersaving(node)
    if powersaving is not None:
        parts["powersaving"] = powersaving
    fem_rxgain = desired_fem_rxgain(node)
    if fem_rxgain is not None:
        parts["fem_rxgain"] = fem_rxgain
    rxgain = rxgain_apply_enabled(node)
    if rxgain is not None:
        parts["rxgain"] = rxgain
    return parts


def profile_id(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
) -> str:
    raw = json.dumps(
        profile_parts(node, sites, doc=doc, keys=keys, key=key),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"v{PROFILE_ID_VERSION}:{digest}"


def field_desired_str(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def applicable_field_desireds(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
) -> dict[str, str]:
    """Desired stamp value per SET field (identity is book metadata only)."""
    parts = profile_parts(node, sites, doc=doc, keys=keys, key=key)
    out: dict[str, str] = {}
    bind = site_binding(key, node, sites)
    for field in APPLY_FIELDS:
        if field not in parts:
            continue
        if field == "admin" and not password_is_strong(node.get("admin_password")):
            continue
        if field in ("lat", "lon") and bind and not resolve_public_apply_position(
            node, sites, key=key, doc=doc
        ):
            continue
        if field == "owner" and not parts["owner"]:
            continue
        out[field] = field_desired_str(parts[field])
    return out


def profile_legacy_synced(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> bool:
    desired = profile_id(node, sites, doc=doc, keys=keys, key=unit)
    stamped = last_ok_apply(conn, unit, "profile")
    if stamped == desired:
        return True
    # USB onboard used to stamp profile="private" before v1 hashes existed.
    if stamped != "private":
        return False
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    return all(last_ok_apply(conn, unit, field) == des for field, des in applicable.items())


def apply_due_fields(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    force: bool = False,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> list[str]:
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    if force:
        return list(applicable.keys())
    if profile_legacy_synced(conn, unit, node, sites, doc=doc, keys=keys):
        return []
    identity = applicable.get("identity")
    stamped_identity = last_ok_apply(conn, unit, "identity")
    if (
        identity is not None
        and stamped_identity is not None
        and stamped_identity != identity
    ):
        return list(applicable.keys())
    return [f for f, des in applicable.items() if last_ok_apply(conn, unit, f) != des]


def radio_apply_due_fields(due: list[str]) -> list[str]:
    """SET fields still due (identity is book metadata, not pushed over mesh)."""
    return [f for f in due if f != "identity"]


def format_apply_plan(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    force: bool = False,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> tuple[str, str]:
    """Apply summary: due SET fields (+ hash) vs already-stamped fields."""
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    due = apply_due_fields(conn, unit, node, sites, force=force, doc=doc, keys=keys)
    pid = profile_id(node, sites, doc=doc, keys=keys, key=unit)
    if not due:
        need = f"synced ({pid})"
    else:
        parts = radio_apply_due_fields(due)
        if "identity" in due:
            parts = [*parts, "identity"]
        need = f"{', '.join(parts)} ({pid})"
    have = [f for f in APPLY_FIELDS if f in applicable and f not in due]
    skip = f"{', '.join(have)} (synced)" if have else "none"
    return need, skip


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
    if apply_due_fields(conn, unit, node, sites, doc=doc, keys=keys):
        return True
    if guest_needs_assign(node, doc or {}, unit):
        return True
    return False


def stamp_profile_after_onboard(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
) -> str | None:
    """After USB onboard applied the bench mask, stamp every SET field."""
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    for field, des in applicable.items():
        stamp_apply(conn, unit=unit, field=field, desired=des, ok=True)
    return profile_id(node, sites, doc=doc, keys=keys, key=unit)


def stamp_profile_after_trust(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    pre_apply_hash: str | None,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    doc_before: dict[str, Any] | None = None,
    keys_before: dict[str, list[str]] | None = None,
) -> bool:
    """After trust updated ACL on a fully-synced unit, stamp acl only."""
    _ = pre_apply_hash
    before_doc = doc_before if doc_before is not None else doc
    before_keys = keys_before if keys_before is not None else keys
    if apply_due_fields(conn, unit, node, sites, doc=before_doc, keys=before_keys):
        return False
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    acl = applicable.get("acl")
    if acl is None:
        return False
    insert_apply(conn, unit=unit, field="acl", desired=acl, ok=True)
    return True


SetSend = Literal["ok", "timeout", "error"]


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
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> SetSend:
    raw = await send_cmd_sync(
        client,
        target,
        command,
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step(f"{field}: no response")
        return "timeout"
    if field == "admin":
        ok_reply = cli_admin_password_ok(raw, expected)
    else:
        ok_reply = cli_set_ok(raw)
    if cli_error_reply(raw) or not ok_reply:
        log.step(f"{field}: set failed ({raw.strip()[:40]})")
        return "error"
    log.step(f"{field} set OK")
    return "ok"


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
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> SetSend:
    for op in plan_acl_ops(want, heard):
        result = await _set_cli(
            client,
            target,
            f"setperm {op.key} {op.perm}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field=op.label,
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
        )
        if result != "ok":
            return result
    return "ok"


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
    force: bool = False,
) -> bool:
    """SET mask or book identity plus shared radio policy. Returns True when fully synced."""
    if force:
        clear_apply_stamps(conn, target.key)

    due = frozenset(
        apply_due_fields(
            conn, target.key, node, sites, force=force, doc=doc, keys=keys
        )
    )
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=target.key)

    def stamp(field: str) -> None:
        des = applicable.get(field)
        if des is not None:
            stamp_apply(conn, unit=target.key, field=field, desired=des, ok=True)

    if not due:
        log.step(f"profile OK ({profile_id(node, sites, doc=doc, keys=keys, key=target.key)})")
        return True

    def abort(field: str) -> bool:
        log.step(f"apply aborted: {field} unreachable")
        remaining = apply_due_fields(
            conn, target.key, node, sites, doc=doc, keys=keys
        )
        if remaining:
            log.step(f"profile partial ({len(remaining)} due: {', '.join(remaining)})")
        return False

    bind = site_binding(target.key, node, sites)

    if "name" in due:
        name = public_radio_name(target.key, node, sites, doc=doc) if bind else MASK_NAME
        if bind:
            name_log = format_apply_name_log(target.key, node, sites, doc=doc)
            if name_log:
                log.step(name_log)
        if await _set_cli(
            client,
            target,
            f"set name {name}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field="name",
        ) == "ok":
            stamp("name")
        else:
            return abort("name")
    else:
        log.step("name: skip (synced)")

    if bind:
        pos = resolve_public_apply_position(node, sites, key=target.key, doc=doc)
        if pos and ("lat" in due or "lon" in due):
            audit = audit_apply_position(node, sites, key=target.key, doc=doc)
            if audit:
                log.step(format_apply_position_log(audit))
        if pos:
            if "lat" in due:
                if await set_book_coord(
                    client, target, "lat", float(pos["lat"]),
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log, session=session,
                ) is not None:
                    stamp("lat")
                else:
                    return abort("lat")
            else:
                log.step("lat: skip (synced)")
            if "lon" in due:
                if await set_book_coord(
                    client, target, "lon", float(pos["lon"]),
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log, session=session,
                ) is not None:
                    stamp("lon")
                else:
                    return abort("lon")
            else:
                log.step("lon: skip (synced)")
        if node.get("advert_interval_min") is not None:
            if "advert" in due:
                if await _set_cli(
                    client, target, f"set advert.interval {int(node['advert_interval_min'])}",
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log, session=session, field="advert",
                ) == "ok":
                    stamp("advert")
                else:
                    return abort("advert")
            else:
                log.step("advert: skip (synced)")
        if node.get("flood_advert_interval_h") is not None:
            if "flood" in due:
                if await _set_cli(
                    client, target, f"set flood.advert.interval {int(node['flood_advert_interval_h'])}",
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log, session=session, field="flood_advert",
                ) == "ok":
                    stamp("flood")
                else:
                    return abort("flood")
            else:
                log.step("flood_advert: skip (synced)")
    else:
        if "lat" in due:
            if await set_book_coord(
                client, target, "lat", 0.0,
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session,
            ) is not None:
                stamp("lat")
            else:
                return abort("lat")
        else:
            log.step("lat: skip (synced)")
        if "lon" in due:
            if await set_book_coord(
                client, target, "lon", 0.0,
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session,
            ) is not None:
                stamp("lon")
            else:
                return abort("lon")
        else:
            log.step("lon: skip (synced)")
        if "advert" in due:
            advert = _opt_int(node.get("advert_interval_min"))
            interval = 0 if advert is None else advert
            if await _set_cli(
                client, target, f"set advert.interval {interval}",
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session, field="advert",
            ) == "ok":
                stamp("advert")
            else:
                return abort("advert")
        else:
            log.step("advert: skip (synced)")
        if "flood" in due:
            flood = _opt_int(node.get("flood_advert_interval_h"))
            interval = 0 if flood is None else flood
            if await _set_cli(
                client, target, f"set flood.advert.interval {interval}",
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session, field="flood_advert",
            ) == "ok":
                stamp("flood")
            else:
                return abort("flood")
        else:
            log.step("flood_advert: skip (synced)")

    if "guest" in due:
        guest = _ensure_guest_password(node, doc, target.key)
        if await _set_cli(
            client, target, f"set guest.password {guest}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log, session=session, field="guest",
        ) == "ok":
            stamp("guest")
        else:
            return abort("guest")
    else:
        log.step("guest: skip (synced)")

    if "owner" in due:
        owner_text = owner_info_for_apply(node, doc, key=target.key, sites=sites)
        owner_cmd = owner_info_cli_payload(owner_text)
        if await _set_cli(
            client,
            target,
            f"set owner.info {owner_cmd}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field="owner",
        ) == "ok":
            stamp("owner")
        else:
            return abort("owner")
    else:
        log.step("owner: skip (synced)")

    if "repeat" in due:
        repeat_on = desired_repeat(node, sites, key=target.key)
        if await _set_cli(
            client,
            target,
            f"set repeat {'on' if repeat_on else 'off'}",
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
            field="repeat",
        ) == "ok":
            stamp("repeat")
        else:
            return abort("repeat")
    else:
        log.step("repeat: skip (synced)")

    admin = node.get("admin_password")
    if password_is_strong(admin):
        if "admin" in due:
            admin_pw = normalize_password(admin)
            if await _set_cli(
                client, target, f"password {admin_pw}",
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session, field="admin",
                expected=admin_pw,
            ) == "ok":
                stamp("admin")
            else:
                return abort("admin")
        else:
            log.step("admin: skip (synced)")

    if "path_hash" in due:
        if await set_path_hash_policy(
            client, target, cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log, session=session,
            mode=desired_path_hash_mode(node),
        ) is not None:
            stamp("path_hash")
        else:
            return abort("path_hash")
    else:
        log.step("path.hash: skip (synced)")

    if "dutycycle" in due:
        if await set_dutycycle_policy(
            client, target, cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log, session=session,
            firmware_version=firmware_version or node.get("firmware_version"),
            pct=float(desired_dutycycle(node)),
        ) is not None:
            stamp("dutycycle")
        else:
            return abort("dutycycle")
    else:
        log.step("dutycycle: skip (synced)")

    if "ota_autofetch" in due:
        unsupported = False
        try:
            applied = await set_ota_autofetch_policy(
                client, target, cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session,
                mode=desired_ota_autofetch(node),
            )
        except OtaAutofetchUnsupported:
            unsupported = True
            applied = None
        if applied is not None or unsupported:
            stamp("ota_autofetch")
            if unsupported:
                log.step("ota autofetch: skip (unsupported)")
        else:
            return abort("ota_autofetch")
    else:
        log.step("ota autofetch: skip (synced)")

    if "powersaving" in due:
        if await set_powersaving_policy(
            client, target, cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log, session=session,
            enabled=desired_powersaving(node),
        ) is not None:
            stamp("powersaving")
        else:
            return abort("powersaving")
    elif "powersaving" in applicable:
        log.step("powersaving: skip (synced)")

    if "fem_rxgain" in due:
        unsupported = False
        try:
            applied = await set_fem_rxgain_policy(
                client, target, cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session,
                enabled=desired_fem_rxgain(node),
            )
        except FemRxgainUnsupported:
            unsupported = True
            applied = None
        if applied is not None or unsupported:
            stamp("fem_rxgain")
            if unsupported:
                log.step("fem.rxgain: skip (unsupported)")
        else:
            return abort("fem_rxgain")
    elif "fem_rxgain" in applicable:
        log.step("fem.rxgain: skip (synced)")

    if "rxgain" in due:
        unsupported = False
        try:
            applied = await set_rxgain_policy(
                client, target, cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log, session=session,
                enabled=rxgain_apply_enabled(node),
            )
        except FemRxgainUnsupported:
            unsupported = True
            applied = None
        if applied is not None or unsupported:
            stamp("rxgain")
            if unsupported:
                log.step("radio.rxgain: skip (unsupported)")
        else:
            return abort("rxgain")
    elif "rxgain" in applicable:
        log.step("radio.rxgain: skip (synced)")

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
    if want_acl and "acl" in due:
        if heard_acl is None:
            wait_cap = mesh_wait_seconds(6000, cap=cmd_timeout)

            acl_raw = await retry_binary_req(
                "GET_ACL",
                lambda dest_wait: client.commands.req_acl_sync(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                client=client,
                attempts=attempts,
                log=log,
                session=session,
                target=target,
                wait_s=wait_cap,
                cap=cmd_timeout,
            )
            heard_acl = normalize_acl_payload(acl_raw)
        if await _apply_acl(
            client, target, want_acl, heard_acl,
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log, session=session,
        ) == "ok":
            stamp("acl")
        else:
            return abort("acl")
    elif want_acl:
        log.step("acl: skip (synced)")

    remaining = apply_due_fields(conn, target.key, node, sites, doc=doc, keys=keys)
    if not radio_apply_due_fields(remaining):
        if "identity" in applicable and "identity" in remaining:
            stamp("identity")
        remaining = apply_due_fields(
            conn, target.key, node, sites, doc=doc, keys=keys
        )
    if not remaining:
        pid = profile_id(node, sites, doc=doc, keys=keys, key=target.key)
        log.step(f"profile OK ({pid})")
        return True
    log.step(f"profile partial ({len(remaining)} due: {', '.join(remaining)})")
    return False


def persist_guest_if_new(nodes_path: Any, doc: dict[str, Any]) -> None:
    nodes = doc.get("nodes")
    if isinstance(nodes, dict):
        sync_paused(Path(nodes_path), nodes)
    write_nodes_doc(nodes_path, doc)
