"""Build sanitized fleet snapshots for the monitor web UI."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from envybot.apply import (
    apply_is_due,
    applicable_field_desireds,
    profile_parts,
)
from envybot.health import compute_health
from envybot.history import (
    all_last_seen,
    compact_sparks,
    interval_traffic,
    last_ok_apply_desireds,
    last_ok_apply_times,
    latest_mesh_audit_path,
    latest_neighbors,
    latest_ota,
    latest_status,
    load_community_locs,
    open_history,
    rolling_traffic,
    save_community_locs,
    status_series,
)
from envybot.ota_parse import ota_badge
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import (
    is_decommissioned,
    is_meshcore_platform,
    is_paused,
    is_public,
    load_nodes_doc,
    normalize_fleet_node,
)
from envybot.routing import (
    abbrev_live_route_label,
    live_route_from_audit_path,
    resolve_routing,
    routing_explicit,
)
from envybot.position import (
    approx_miles,
    display_name,
    haversine_miles,
    is_placeholder_gps,
    load_sites,
    lookup_site_name,
    node_alias,
    resolve_display_position,
    site_binding,
)

SECRET_KEY_RE = re.compile(r"(password|secret)", re.I)
PULLED_AT_SUFFIX = "_pulled_at"

DEFAULT_STALE_SECS = 86400.0
NEIGHBOR_FRESH_SECS = 7 * 24 * 3600

SessionState = str  # idle | queued | refreshing | pulling | pushing | polling | ok | unreachable | paused

RADIO_PREF_FIELDS = (
    ("powersaving", "Power saving"),
    ("fem_rxgain", "FEM LNA"),
    ("fem_vfem", "FEM VFEM"),
    ("rxgain", "SX1262 boost"),
    ("dutycycle", "Duty cycle"),
    ("path_hash", "Path hash"),
    ("ota_autofetch", "OTA autofetch"),
)

STATUS_PUBLIC_KEYS = (
    "battery_mv",
    "packets_recv",
    "packets_sent",
    "err_events",
    "recv_errors",
    "uptime_secs",
    "recv_flood",
    "recv_direct",
    "sent_flood",
    "sent_direct",
    "last_snr",
    "last_rssi",
    "noise_floor",
    "tx_queue_len",
    "tx_airtime_secs",
    "rx_airtime_secs",
    "direct_dups",
    "flood_dups",
)


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def _stability_ack_ts(node: dict[str, Any]) -> int | None:
    raw = node.get("stability_ack_ts")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def unit_label(
    *,
    key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]],
) -> str:
    return display_name(key, node, sites)


def last_heard(node: dict[str, Any], seen: dict[str, Any] | None = None) -> int | None:
    stamps: list[int] = []
    if seen:
        for key in (
            "updated_at",
            "status_at",
            "firmware_at",
            "name_at",
            "telemetry_at",
            "neighbors_at",
        ):
            val = seen.get(key)
            if val is None:
                continue
            try:
                stamps.append(int(val))
            except (TypeError, ValueError):
                continue
    for key, val in node.items():
        if not key.endswith(PULLED_AT_SUFFIX) or val is None:
            continue
        try:
            stamps.append(int(val))
        except (TypeError, ValueError):
            continue
    return max(stamps) if stamps else None


def freshness(last: int | None, *, now: int, stale_secs: float) -> str:
    if last is None:
        return "never"
    age = max(0, now - last)
    if age <= stale_secs:
        return "fresh"
    return "stale"


def extract_telemetry(telemetry: Any) -> dict[str, float | None]:
    voltage: float | None = None
    temperature: float | None = None
    if not isinstance(telemetry, list):
        return {"voltage": voltage, "temperature": temperature}
    for item in telemetry:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        val = item.get("value")
        if kind == "voltage" and val is not None:
            try:
                voltage = float(val)
            except (TypeError, ValueError):
                pass
        elif kind == "temperature" and val is not None:
            try:
                temperature = float(val)
            except (TypeError, ValueError):
                pass
    return {"voltage": voltage, "temperature": temperature}


def resolve_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]],
    *,
    key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return resolve_display_position(node, sites, key=key, doc=doc)


def build_pubkey_index(nodes: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, node in nodes.items():
        if not isinstance(node, dict) or is_decommissioned(node):
            continue
        if not is_meshcore_platform(node):
            continue
        pk = str(node.get("identity_pubkey") or "").lower()
        if not pk:
            continue
        index[pk] = key
        if len(pk) >= 8:
            index[pk[:8]] = key
    return index


def neighbor_is_fresh(secs_ago: Any, *, max_age: float = NEIGHBOR_FRESH_SECS) -> bool:
    """True when the node's last-heard age is known and within the UI window."""
    if secs_ago is None:
        return False
    try:
        age = float(secs_ago)
    except (TypeError, ValueError):
        return False
    if age < 0:
        return True
    return age <= max_age


def reported_locs_from_contacts(contacts: Any) -> dict[str, tuple[float, float]]:
    """Advert GPS from companion contacts, indexed by full key and prefixes."""
    if not isinstance(contacts, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for key, raw in contacts.items():
        if not isinstance(raw, dict):
            continue
        pk = str(raw.get("public_key") or key or "").strip().lower()
        if not pk:
            continue
        try:
            lat = float(raw.get("adv_lat"))
            lon = float(raw.get("adv_lon"))
        except (TypeError, ValueError):
            continue
        if is_placeholder_gps(lat, lon):
            continue
        loc = (lat, lon)
        out[pk] = loc
        if len(pk) >= 16:
            out[pk[:16]] = loc
        if len(pk) >= 8:
            out[pk[:8]] = loc
    return out


def lookup_reported_loc(
    pubkey: Any, reported_locs: dict[str, tuple[float, float]]
) -> tuple[float, float] | None:
    if not isinstance(pubkey, str) or not pubkey:
        return None
    pk = pubkey.strip().lower()
    if not pk:
        return None
    if pk in reported_locs:
        return reported_locs[pk]
    if len(pk) >= 16 and pk[:16] in reported_locs:
        return reported_locs[pk[:16]]
    if len(pk) >= 8 and pk[:8] in reported_locs:
        return reported_locs[pk[:8]]
    return None


def _coord_pair(pos: Any) -> tuple[float, float] | None:
    if not isinstance(pos, dict):
        return None
    try:
        lat = float(pos["lat"])
        lon = float(pos["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if is_placeholder_gps(lat, lon):
        return None
    return lat, lon


def neighbor_loc(
    nb: dict[str, Any],
    units: dict[str, dict[str, Any]],
    reported_locs: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Book display loc for fleet peers; companion advert GPS for community."""
    key = nb.get("unit_key")
    if isinstance(key, str) and key in units:
        book = _coord_pair(units[key].get("position"))
        if book:
            return book
    return lookup_reported_loc(nb.get("pubkey_prefix"), reported_locs)


def attach_neighbor_miles(
    units: dict[str, dict[str, Any]],
    reported_locs: dict[str, tuple[float, float]] | None = None,
) -> None:
    locs = reported_locs or {}
    for unit in units.values():
        origin = _coord_pair(unit.get("position"))
        if not origin:
            continue
        for nb in unit.get("neighbors") or []:
            if not isinstance(nb, dict):
                continue
            dest = neighbor_loc(nb, units, locs)
            if not dest:
                continue
            miles = haversine_miles(origin[0], origin[1], dest[0], dest[1])
            nb["miles"] = approx_miles(miles)


def sanitize_neighbors(
    raw: Any,
    *,
    pubkey_index: dict[str, str],
    nodes: dict[str, Any],
    sites: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pubkey") or "").lower()
        if not pk:
            continue
        if not neighbor_is_fresh(item.get("secs_ago")):
            continue
        prefix = pk[:8] if len(pk) >= 8 else pk
        row: dict[str, Any] = {
            "pubkey_prefix": prefix,
            "secs_ago": item.get("secs_ago"),
            "snr": item.get("snr"),
        }
        resolved = pubkey_index.get(pk) or pubkey_index.get(prefix)
        if resolved and isinstance(nodes.get(resolved), dict):
            peer = nodes[resolved]
            row["unit_key"] = resolved
            row["unit_id"] = peer.get("unit_id")
            peer_bind = site_binding(resolved, peer, sites)
            row["site"] = peer_bind[0] if peer_bind else None
            row["site_name"] = lookup_site_name(peer_bind[0], sites) if peer_bind else None
            row["label"] = unit_label(key=resolved, node=peer, sites=sites)
        out.append(row)
    return out


def strip_secrets(node: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secret keys removed (for assertions)."""
    return {k: v for k, v in node.items() if not is_secret_key(k)}


def _neighbors_from_seen(seen: dict[str, Any] | None) -> list[Any]:
    if not seen:
        return []
    raw = seen.get("neighbors_payload")
    if isinstance(raw, list):
        return raw
    return []


def build_unit_status(
    seen: dict[str, Any] | None,
    status_raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge latest status JSON with last_seen counters for the fleet UI."""
    merged: dict[str, Any] = {}
    if isinstance(status_raw, dict):
        for key in STATUS_PUBLIC_KEYS:
            val = status_raw.get(key)
            if val is not None:
                merged[key] = val
    if seen:
        for key in STATUS_PUBLIC_KEYS:
            if merged.get(key) is None and seen.get(key) is not None:
                merged[key] = seen[key]
    return merged or None


def _heard_nonzero(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return abs(float(value)) > 1e-9
    except (TypeError, ValueError):
        return False


def _heard_after_apply(
    seen: dict[str, Any],
    *,
    value_key: str,
    at_key: str,
    apply_field: str,
    apply_at: dict[str, int] | None,
) -> bool:
    if not _heard_nonzero(seen.get(value_key)):
        return False
    heard_at = seen.get(at_key)
    if heard_at is None or heard_at == "":
        return False
    stamped = (apply_at or {}).get(apply_field)
    if stamped is None:
        return True
    try:
        return int(heard_at) >= int(stamped)
    except (TypeError, ValueError):
        return False


def identity_leak_parts(
    node: dict[str, Any],
    seen: dict[str, Any] | None,
    apply_at: dict[str, int] | None = None,
) -> list[str]:
    """Last-pull advert intervals that are still current vs apply stamps."""
    if is_public(node) or not seen:
        return []
    parts: list[str] = []
    if _heard_after_apply(
        seen,
        value_key="advert_interval_min",
        at_key="advert_at",
        apply_field="advert",
        apply_at=apply_at,
    ):
        parts.append(f"advert {int(float(seen['advert_interval_min']))} min")
    if _heard_after_apply(
        seen,
        value_key="flood_advert_interval_h",
        at_key="flood_advert_at",
        apply_field="flood",
        apply_at=apply_at,
    ):
        parts.append(f"flood advert {int(float(seen['flood_advert_interval_h']))} h")
    return parts


def identity_leak_reason(
    node: dict[str, Any],
    seen: dict[str, Any] | None,
    apply_at: dict[str, int] | None = None,
) -> str | None:
    parts = identity_leak_parts(node, seen, apply_at)
    if not parts:
        return None
    return "Last pull: " + ", ".join(parts)


def heard_identity_leak(
    node: dict[str, Any],
    seen: dict[str, Any] | None,
    apply_at: dict[str, int] | None = None,
) -> bool:
    """True when a later pull still shows a nonzero advert interval."""
    return bool(identity_leak_parts(node, seen, apply_at))


def drift_state(
    node: dict[str, Any],
    *,
    profile_ok: bool,
    seen: dict[str, Any] | None = None,
    apply_at: dict[str, int] | None = None,
) -> str | None:
    """``leak`` = last pull still has advert on. ``due`` = stamp stale."""
    if heard_identity_leak(node, seen, apply_at):
        return "leak"
    if not profile_ok:
        return "due"
    return None


def pref_display(field: str, value: Any) -> str:
    if field in ("powersaving", "fem_rxgain", "fem_vfem", "rxgain"):
        if isinstance(value, bool):
            return "on" if value else "off"
        text = str(value).strip().lower()
        if text in ("1", "true", "on", "yes"):
            return "on"
        if text in ("0", "false", "off", "no"):
            return "off"
        return text
    if field == "dutycycle":
        try:
            return f"{int(round(float(value)))}%"
        except (TypeError, ValueError):
            return str(value)
    if field == "path_hash":
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return str(value)
        return "2-byte" if mode == 1 else str(mode)
    return str(value)


def build_radio_prefs(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
    apply_desireds: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Book apply prefs plus stamp state. Not a live radio GET."""
    parts = profile_parts(node, sites, doc=doc, keys=keys, key=key)
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=key)
    stamped = apply_desireds or {}
    prefs: list[dict[str, str]] = []
    for field, label in RADIO_PREF_FIELDS:
        if field not in applicable or field not in parts:
            continue
        desired = applicable[field]
        prefs.append(
            {
                "id": field,
                "label": label,
                "value": pref_display(field, parts[field]),
                "state": "synced" if stamped.get(field) == desired else "due",
            }
        )
    return prefs


def sanitize_unit(
    key: str,
    node: dict[str, Any],
    *,
    sites: dict[str, dict[str, Any]],
    pubkey_index: dict[str, str],
    nodes: dict[str, Any],
    now: int,
    stale_secs: float,
    session: dict[str, Any] | None = None,
    seen: dict[str, Any] | None = None,
    neighbors_raw: Any = None,
    status_raw: dict[str, Any] | None = None,
    traffic_interval: dict[str, Any] | None = None,
    traffic_window_6h: dict[str, Any] | None = None,
    profile_ok: bool = False,
    ota_raw: dict[str, Any] | None = None,
    apply_at: dict[str, int] | None = None,
    prefs: list[dict[str, str]] | None = None,
    doc: dict[str, Any] | None = None,
    audit_path: str | None = None,
) -> dict[str, Any]:
    normalize_fleet_node(node)
    heard = last_heard(node, seen)
    position = resolve_position(node, sites, key=key, doc=doc)
    tele_src: list[dict[str, Any]] = []
    if seen:
        if seen.get("voltage") is not None:
            tele_src.append({"type": "voltage", "value": seen.get("voltage")})
        if seen.get("temperature") is not None:
            tele_src.append({"type": "temperature", "value": seen.get("temperature")})
    tele = extract_telemetry(tele_src or None)
    status = build_unit_status(seen, status_raw)
    nbs = neighbors_raw if neighbors_raw is not None else node.get("neighbors")
    bind = site_binding(key, node, sites)
    site_slug = bind[0] if bind else None
    site_name = lookup_site_name(site_slug, sites) if site_slug else None

    policy = resolve_routing(node)
    explicit = routing_explicit(node)
    live_route = None
    if session and session.get("live_route"):
        live_route = session.get("live_route")
    elif audit_path:
        live_route = live_route_from_audit_path(audit_path, policy=policy)

    unit: dict[str, Any] = {
        "key": key,
        "unit_id": node.get("unit_id") or key.upper(),
        "label": unit_label(key=key, node=node, sites=sites),
        "owner": node.get("owner"),
        "site": site_slug,
        "site_name": site_name,
        "alias": node_alias(node),
        "public": is_public(node),
        "paused": is_paused(node),
        "routing": policy.value,
        "routing_explicit": explicit,
        "live_route": live_route,
        "live_route_label": abbrev_live_route_label(live_route["label"])
        if live_route and live_route.get("label")
        else None,
        "hardware": node.get("hardware"),
        "notes": node.get("notes"),
        "firmware_version": (seen or {}).get("firmware_version"),
        "firmware_platform": node.get("firmware_platform"),
        "bootloader_version": (seen or {}).get("bootloader_version"),
        "base_hash": (seen or {}).get("base_hash") or None,
        "ota": ota_raw,
        "ota_badge": ota_badge(ota_raw),
        "identity_pubkey": str(node.get("identity_pubkey") or "").lower() or None,
        "position": position,
        "mapped": position is not None,
        "last_heard": heard,
        "freshness": freshness(heard, now=now, stale_secs=stale_secs),
        "telemetry": tele,
        "status": status,
        "traffic_interval": traffic_interval,
        "traffic_window_6h": traffic_window_6h,
        "neighbors": sanitize_neighbors(
            nbs,
            pubkey_index=pubkey_index,
            nodes=nodes,
            sites=sites,
        ),
        "neighbor_count": len(nbs or []) if isinstance(nbs, list) else 0,
        "acl_count": None,
        "path_hash_mode": node.get("path_hash_mode"),
        "dutycycle": node.get("dutycycle"),
        "powersaving": node.get("powersaving"),
        "fem_rxgain": node.get("fem_rxgain"),
        "fem_vfem": node.get("fem_vfem"),
        "rxgain": node.get("rxgain"),
        "prefs": prefs or [],
        "node_clock": (seen or {}).get("node_clock"),
        "drift": drift_state(node, profile_ok=profile_ok, seen=seen, apply_at=apply_at),
        "drift_detail": identity_leak_reason(node, seen, apply_at),
    }
    if session:
        unit["session"] = session
    return unit


def build_fleet_snapshot(
    *,
    nodes_path: Path,
    sites_path: Path | None = None,
    stale_secs: float = DEFAULT_STALE_SECS,
    session_states: dict[str, dict[str, Any]] | None = None,
    companion: str | None = None,
    poll: dict[str, Any] | None = None,
    last_seen: dict[str, dict[str, Any]] | None = None,
    reported_locs: dict[str, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Full public fleet snapshot: YAML desired + sqlite last_seen."""
    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        nodes = {}
    book_dir = nodes_path.parent
    if sites_path is None:
        sites_path = book_dir / "sites.yaml"
    sites = load_sites(sites_path)
    keys = load_keys(keys_path(nodes_path))
    pubkey_index = build_pubkey_index(nodes)
    now = int(time.time())
    states = session_states or {}
    seen_map = last_seen
    neighbors_map: dict[str, Any] = {}
    ota_map: dict[str, Any] = {}
    status_map: dict[str, dict[str, Any]] = {}
    interval_map: dict[str, dict[str, Any]] = {}
    window_map: dict[str, dict[str, Any]] = {}
    status_rows_map: dict[str, list[dict[str, Any]]] = {}
    apply_at_map: dict[str, dict[str, int]] = {}
    apply_desired_map: dict[str, dict[str, str]] = {}
    audit_path_map: dict[str, str] = {}
    conn = None
    try:
        conn = open_history(book_dir)
        if seen_map is None:
            seen_map = all_last_seen(conn)
        apply_at_map = last_ok_apply_times(conn)
        apply_desired_map = last_ok_apply_desireds(conn)
        for key in nodes:
            nbs = latest_neighbors(conn, key)
            if nbs is not None:
                neighbors_map[key] = nbs
            ota = latest_ota(conn, key)
            if ota is not None:
                ota_map[key] = ota
            status = latest_status(conn, key)
            if status is not None:
                status_map[key] = status
            interval = interval_traffic(conn, key)
            if interval is not None:
                interval_map[key] = interval
            window = rolling_traffic(conn, key, now=now)
            if window is not None:
                window_map[key] = window
            status_rows_map[key] = status_series(conn, key)
            audit_path = latest_mesh_audit_path(conn, key)
            if audit_path:
                audit_path_map[key] = audit_path
        if reported_locs:
            save_community_locs(conn, reported_locs)
        else:
            reported_locs = load_community_locs(conn)
    except OSError:
        seen_map = seen_map or {}
        conn = None

    try:
        units: dict[str, dict[str, Any]] = {}
        for key, node in nodes.items():
            if not isinstance(node, dict) or is_decommissioned(node):
                continue
            if not is_meshcore_platform(node):
                continue
            profile_ok = False
            if conn is not None:
                profile_ok = not apply_is_due(
                    conn, key, node, sites, doc=doc, keys=keys
                )
            units[key] = sanitize_unit(
                key,
                node,
                sites=sites,
                pubkey_index=pubkey_index,
                nodes=nodes,
                now=now,
                stale_secs=stale_secs,
                session=states.get(key),
                seen=seen_map.get(key),
                neighbors_raw=neighbors_map.get(key),
                status_raw=status_map.get(key),
                traffic_interval=interval_map.get(key),
                traffic_window_6h=window_map.get(key),
                profile_ok=profile_ok,
                ota_raw=ota_map.get(key),
                apply_at=apply_at_map.get(key),
                prefs=build_radio_prefs(
                    node,
                    sites,
                    doc=doc,
                    keys=keys,
                    key=key,
                    apply_desireds=apply_desired_map.get(key),
                ),
                doc=doc,
                audit_path=audit_path_map.get(key),
            )
            status_rows = status_rows_map.get(key, [])
            units[key]["health"] = compute_health(
                freshness=units[key]["freshness"],
                session=states.get(key),
                drift=units[key].get("drift"),
                drift_detail=units[key].get("drift_detail"),
                status=units[key].get("status"),
                telemetry=units[key].get("telemetry"),
                traffic_interval=interval_map.get(key),
                traffic_window=window_map.get(key),
                status_rows=status_rows,
                paused=units[key]["paused"],
                stability_ack_ts=_stability_ack_ts(node),
                last_heard=units[key].get("last_heard"),
                now=now,
            )
            units[key]["sparks"] = compact_sparks(status_rows, conn=conn, unit=key, now=now)
        attach_neighbor_miles(units, reported_locs)
    finally:
        if conn is not None:
            conn.close()

    mapped = sum(1 for u in units.values() if u.get("mapped"))
    fresh = sum(1 for u in units.values() if u.get("freshness") == "fresh")
    stale = sum(1 for u in units.values() if u.get("freshness") == "stale")
    never = sum(1 for u in units.values() if u.get("freshness") == "never")
    paused = sum(1 for u in units.values() if u.get("paused"))

    return {
        "ts": now,
        "book": str(book_dir),
        "stale_secs": stale_secs,
        "companion": companion,
        "poll": poll or {"phase": "idle"},
        "counts": {
            "total": len(units),
            "mapped": mapped,
            "unmapped": len(units) - mapped,
            "fresh": fresh,
            "stale": stale,
            "never": never,
            "paused": paused,
        },
        "units": units,
    }


def build_neighbor_edges(units: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """GeoJSON-ready edges between mapped units with resolved neighbors."""
    by_key = units
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key, unit in by_key.items():
        pos = unit.get("position")
        if not pos:
            continue
        for nb in unit.get("neighbors") or []:
            peer_key = nb.get("unit_key")
            if not peer_key or peer_key not in by_key:
                continue
            peer = by_key[peer_key]
            peer_pos = peer.get("position")
            if not peer_pos:
                continue
            pair = tuple(sorted((key, peer_key)))
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(
                {
                    "from": key,
                    "to": peer_key,
                    "coordinates": [
                        [pos["lon"], pos["lat"]],
                        [peer_pos["lon"], peer_pos["lat"]],
                    ],
                }
            )
    return edges


def assert_no_secrets(payload: Any, *, path: str = "") -> None:
    """Recursively assert no secret keys appear in a public payload."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if is_secret_key(str(k)):
                raise AssertionError(f"secret key leaked at {path}.{k}")
            assert_no_secrets(v, path=f"{path}.{k}" if path else k)
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            assert_no_secrets(item, path=f"{path}[{i}]")
