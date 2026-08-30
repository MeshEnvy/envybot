"""Build sanitized fleet snapshots for the monitor web UI."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from envybot.commands.monitor import load_nodes_doc, normalize_fleet_node

SECRET_KEY_RE = re.compile(r"(password|secret)", re.I)
PULLED_AT_SUFFIX = "_pulled_at"

ONBOARD_LAT = 14.009295
ONBOARD_LON = 120.996018

DEFAULT_STALE_SECS = 86400.0

SessionState = str  # idle | queued | polling | ok | unreachable


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def is_placeholder_gps(lat: Any, lon: Any) -> bool:
    if lat is None or lon is None:
        return False
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return False
    # Device often reports 0,0 or a zero component when GPS is unset.
    if la == 0.0 or lo == 0.0:
        return True
    if abs(la - ONBOARD_LAT) < 0.001 and abs(lo - ONBOARD_LON) < 0.001:
        return True
    return False


def load_sites(sites_path: Path) -> dict[str, dict[str, Any]]:
    if not sites_path.is_file():
        return {}
    yaml = YAML(typ="safe")
    doc = yaml.load(sites_path.read_text(encoding="utf-8")) or {}
    sites = doc.get("sites") or {}
    return {str(k): v for k, v in sites.items() if isinstance(v, dict)}


def last_heard(node: dict[str, Any]) -> int | None:
    stamps: list[int] = []
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
    lat = node.get("lat")
    lon = node.get("lon")
    if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
        try:
            return {"lat": float(lat), "lon": float(lon), "source": "node"}
        except (TypeError, ValueError):
            pass
    site_slug = node.get("site")
    if site_slug and isinstance(site_slug, str):
        site = sites.get(site_slug)
        if site:
            loc = site.get("loc")
            if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                try:
                    return {
                        "lat": float(loc[0]),
                        "lon": float(loc[1]),
                        "source": "site",
                        "site_name": site.get("name"),
                    }
                except (TypeError, ValueError):
                    pass
    return None


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
        out.append(row)
    return out


def strip_secrets(node: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secret keys removed (for assertions)."""
    return {k: v for k, v in node.items() if not is_secret_key(k)}


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
) -> dict[str, Any]:
    normalize_fleet_node(node)
    heard = last_heard(node)
    position = resolve_position(node, sites)
    tele = extract_telemetry(node.get("telemetry"))
    status = node.get("status") if isinstance(node.get("status"), dict) else None
    acl = node.get("acl")
    acl_count = len(acl) if isinstance(acl, list) else None

    unit: dict[str, Any] = {
        "key": key,
        "unit_id": node.get("unit_id") or key.upper(),
        "name": node.get("name"),
        "owner": node.get("owner"),
        "site": node.get("site"),
        "hardware": node.get("hardware"),
        "notes": node.get("notes"),
        "decommissioned": node.get("decommissioned"),
        "firmware_version": node.get("firmware_version"),
        "firmware_platform": node.get("firmware_platform"),
        "bootloader_version": node.get("bootloader_version"),
        "identity_pubkey": str(node.get("identity_pubkey") or "").lower() or None,
        "position": position,
        "mapped": position is not None,
        "last_heard": heard,
        "freshness": freshness(heard, now=now, stale_secs=stale_secs),
        "telemetry": tele,
        "status": status,
        "neighbors": sanitize_neighbors(
            node.get("neighbors"),
            pubkey_index=pubkey_index,
            nodes=nodes,
        ),
        "neighbor_count": len(node.get("neighbors") or []) if isinstance(node.get("neighbors"), list) else 0,
        "acl_count": acl_count,
        "advert_interval_min": node.get("advert_interval_min"),
        "flood_advert_interval_h": node.get("flood_advert_interval_h"),
        "path_hash_mode": node.get("path_hash_mode"),
        "dutycycle": node.get("dutycycle"),
        "node_clock": node.get("node_clock"),
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
) -> dict[str, Any]:
    """Full public fleet snapshot from the book."""
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
