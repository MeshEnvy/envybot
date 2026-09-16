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
    load_nodes_doc,
    write_nodes_doc,
)
from envybot.passwords import (
    guest_needs_assign,
    normalize_password,
    password_is_strong,
    password_token,
    resolve_guest_password,
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
    FLEET_AGC_RESET_INTERVAL,
    FLEET_DUTYCYCLE_PCT,
    FLEET_FEM_RXGAIN,
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
    set_agc_reset_interval_policy,
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
    "fem_rxgain",
    "agc_reset_interval",
    "rxgain",
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


def desired_fem_rxgain(node: dict[str, Any]) -> bool:
    """Book override or fleet default (FEM LNA on)."""
    if "fem_rxgain" in node:
        parsed = _parse_optional_bool(node.get("fem_rxgain"))
        if parsed is not None:
            return parsed
    if "radio.fem.rxgain" in node:
        parsed = _parse_optional_bool(node.get("radio.fem.rxgain"))
        if parsed is not None:
            return parsed
    return FLEET_FEM_RXGAIN


def desired_agc_reset_interval(node: dict[str, Any]) -> int:
    """Book override or fleet default. Rounded to firmware 4-second steps."""
    val = node.get("agc_reset_interval")
    if val is None or val == "":
        return FLEET_AGC_RESET_INTERVAL
    try:
        secs = max(0, int(val))
    except (TypeError, ValueError):
        return FLEET_AGC_RESET_INTERVAL
    return (secs + 2) // 4 * 4


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


FLEET_SITE_ADVERT_MIN = 0
FLEET_SITE_FLOOD_ADVERT_H = 12


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
        if advert is None:
            advert = FLEET_SITE_ADVERT_MIN
        if flood is None:
            flood = FLEET_SITE_FLOOD_ADVERT_H
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
    name = public_radio_name(key, node, sites, doc=doc)
    if bind:
        pos = resolve_public_apply_position(node, sites, key=key, doc=doc)
        lat = round(float(pos["lat"]), 6) if pos else None
        lon = round(float(pos["lon"]), 6) if pos else None
    else:
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
        "fem_rxgain": desired_fem_rxgain(node),
        "agc_reset_interval": desired_agc_reset_interval(node),
    }
    powersaving = desired_powersaving(node)
    if powersaving is not None:
        parts["powersaving"] = powersaving
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
    return resolve_guest_password(node, doc, key)


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




def persist_guest_password(nodes_path: Path, unit: str, node: dict[str, Any]) -> None:
    """Write guest password to disk when apply changed it in memory."""
    if "guest_password" not in node:
        return
    pw = node.get("guest_password")
    if pw is None:
        return
    disk_doc = load_nodes_doc(nodes_path)
    disk_nodes = disk_doc.get("nodes") or {}
    if not isinstance(disk_nodes, dict):
        return
    disk_node = disk_nodes.get(unit)
    if not isinstance(disk_node, dict):
        return
    if disk_node.get("guest_password") == pw:
        return
    disk_node["guest_password"] = pw
    disk_node.pop("last_guest_roll", None)
    write_nodes_doc(nodes_path, disk_doc)


def _heard_matches_desired(
    field: str,
    desired: str,
    heard: Any,
    *,
    node: dict[str, Any],
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    sites: dict[str, dict[str, Any]] | None,
    unit: str,
) -> bool:
    if field == "name":
        return str(heard or "") == desired
    if field in ("lat", "lon"):
        try:
            return abs(float(heard) - float(desired)) <= 1e-5
        except (TypeError, ValueError):
            return False
    if field in ("advert", "flood"):
        try:
            return int(float(heard)) == int(desired)
        except (TypeError, ValueError):
            return False
    if field == "acl":
        if not isinstance(heard, list):
            return False
        try:
            want = resolve_node_acl(doc or {}, node, keys or {})
        except UnknownPerson:
            want = []
        return not plan_acl_ops(want, heard)
    return field_desired_str(heard) == desired


def reconcile_heard(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    field: str,
    heard: Any,
    log: PollLog | None = None,
) -> bool:
    """Clear apply stamp when audit GET shows device drift from book."""
    if last_ok_apply(conn, unit, field) is None:
        return False
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    desired = applicable.get(field)
    if desired is None:
        return False
    if _heard_matches_desired(
        field,
        desired,
        heard,
        node=node,
        doc=doc,
        keys=keys,
        sites=sites,
        unit=unit,
    ):
        return False
    clear_apply_stamps(conn, unit, fields=[field])
    if log is not None:
        log.step(f"{field}: device != book, re-apply queued")
    return True

