"""Build sanitized fleet snapshots for the monitor web UI."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from envybot.history import all_last_seen, latest_neighbors, open_history
from envybot.nodes_doc import is_public, load_nodes_doc, normalize_fleet_node
from envybot.position import is_placeholder_gps, load_sites, resolve_book_position, site_binding

SECRET_KEY_RE = re.compile(r"(password|secret)", re.I)
PULLED_AT_SUFFIX = "_pulled_at"

DEFAULT_STALE_SECS = 86400.0

SessionState = str  # idle | queued | polling | ok | unreachable


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def lookup_site_name(
    site_slug: Any,
    sites: dict[str, dict[str, Any]],
) -> str | None:
    """Pretty site name when the node is bound. Slug if the site has no name."""
    if not isinstance(site_slug, str) or not site_slug.strip():
        return None
    site = sites.get(site_slug)
    if isinstance(site, dict):
        name = site.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return site_slug


def unit_label(
    *,
    key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]],
) -> str:
    book_name = str(node.get("name") or "").strip()
    bind = site_binding(key, node, sites)
    site_name = lookup_site_name(bind[0], sites) if bind else None
    if book_name and site_name:
        return f"{book_name} @ {site_name}"
    if site_name:
        return site_name
    if book_name:
        return book_name
    return str(node.get("unit_id") or key.upper())


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
) -> dict[str, Any] | None:
    return resolve_book_position(node, sites)


def build_pubkey_index(nodes: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        pk = str(node.get("identity_pubkey") or "").lower()
        if not pk:
            continue
        index[pk] = key
        if len(pk) >= 8:
            index[pk[:8]] = key
    return index


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
            row["name"] = peer.get("name")
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


def drift_state(node: dict[str, Any], seen: dict[str, Any] | None) -> str | None:
    """None = ok. leak (private) or mismatch (public)."""
    if not seen:
        return None
    name_heard = seen.get("name_heard")
    lat, lon = seen.get("lat_heard"), seen.get("lon_heard")
    if is_public(node):
        book = str(node.get("name") or "")
        if name_heard and book and name_heard != book:
            return "mismatch"
        return None
    if name_heard and name_heard not in ("Repeater", ""):
        return "leak"
    if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
        return "leak"
    if seen.get("advert_interval_min") not in (None, 0):
        return "leak"
    return None


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
) -> dict[str, Any]:
    normalize_fleet_node(node)
    heard = last_heard(node, seen)
    position = resolve_position(node, sites)
    tele_src: list[dict[str, Any]] = []
    if seen:
        if seen.get("voltage") is not None:
            tele_src.append({"type": "voltage", "value": seen.get("voltage")})
        if seen.get("temperature") is not None:
            tele_src.append({"type": "temperature", "value": seen.get("temperature")})
    tele = extract_telemetry(tele_src or None)
    if seen and seen.get("battery_mv") is not None:
        status = {
            "battery_mv": seen.get("battery_mv"),
            "packets_recv": seen.get("packets_recv"),
            "packets_sent": seen.get("packets_sent"),
            "err_events": seen.get("err_events"),
            "recv_errors": seen.get("recv_errors"),
            "uptime_secs": seen.get("uptime_secs"),
        }
    else:
        status = None
    nbs = neighbors_raw if neighbors_raw is not None else node.get("neighbors")
    bind = site_binding(key, node, sites)
    site_slug = bind[0] if bind else None
    site_name = lookup_site_name(site_slug, sites) if site_slug else None

    unit: dict[str, Any] = {
        "key": key,
        "unit_id": node.get("unit_id") or key.upper(),
        "name": node.get("name"),
        "label": unit_label(key=key, node=node, sites=sites),
        "owner": node.get("owner"),
        "site": site_slug,
        "site_name": site_name,
        "public": is_public(node),
        "hardware": node.get("hardware"),
        "notes": node.get("notes"),
        "decommissioned": node.get("decommissioned"),
        "firmware_version": (seen or {}).get("firmware_version"),
        "firmware_platform": node.get("firmware_platform"),
        "bootloader_version": (seen or {}).get("bootloader_version"),
        "name_heard": (seen or {}).get("name_heard"),
        "identity_pubkey": str(node.get("identity_pubkey") or "").lower() or None,
        "position": position,
        "mapped": position is not None,
        "last_heard": heard,
        "freshness": freshness(heard, now=now, stale_secs=stale_secs),
        "telemetry": tele,
        "status": status,
        "neighbors": sanitize_neighbors(
            nbs,
            pubkey_index=pubkey_index,
            nodes=nodes,
            sites=sites,
        ),
        "neighbor_count": len(nbs or []) if isinstance(nbs, list) else 0,
        "acl_count": None,
        "advert_interval_min": (seen or {}).get("advert_interval_min"),
        "flood_advert_interval_h": (seen or {}).get("flood_advert_interval_h"),
        "path_hash_mode": node.get("path_hash_mode"),
        "dutycycle": node.get("dutycycle"),
        "node_clock": (seen or {}).get("node_clock"),
        "drift": drift_state(node, seen),
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
    pubkey_index = build_pubkey_index(nodes)
    now = int(time.time())
    states = session_states or {}
    seen_map = last_seen
    neighbors_map: dict[str, Any] = {}
    if seen_map is None:
        try:
            conn = open_history(book_dir)
            seen_map = all_last_seen(conn)
            for key in nodes:
                nbs = latest_neighbors(conn, key)
                if nbs is not None:
                    neighbors_map[key] = nbs
            conn.close()
        except OSError:
            seen_map = {}

    units: dict[str, dict[str, Any]] = {}
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
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
        )

    mapped = sum(1 for u in units.values() if u.get("mapped"))
    fresh = sum(1 for u in units.values() if u.get("freshness") == "fresh")
    stale = sum(1 for u in units.values() if u.get("freshness") == "stale")
    never = sum(1 for u in units.values() if u.get("freshness") == "never")

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
