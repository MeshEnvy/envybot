"""Book GPS. Radio apply uses sites.yaml. Sun/map can use bench_loc."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

EARTH_MI = 3958.8

ONBOARD_LAT = 14.009295
ONBOARD_LON = 120.996018


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_MI * math.asin(min(1.0, math.sqrt(a)))


def approx_miles(miles: float) -> float:
    """UI rounding: 0.1 mi under 10, whole miles at 10+."""
    if miles < 10:
        return round(miles, 1)
    return float(round(miles))


def is_placeholder_gps(lat: Any, lon: Any) -> bool:
    if lat is None or lon is None:
        return True
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return True
    if la == 0.0 or lo == 0.0:
        return True
    if abs(la - ONBOARD_LAT) < 0.001 and abs(lo - ONBOARD_LON) < 0.001:
        return True
    return False


def load_sites_doc(sites_path: Path) -> dict[str, Any]:
    if not sites_path.is_file():
        return {"sites": {}}
    yaml = YAML()
    doc = yaml.load(sites_path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        return {"sites": {}}
    if not isinstance(doc.get("sites"), dict):
        doc["sites"] = {}
    return doc


def write_sites_doc(sites_path: Path, doc: dict[str, Any]) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    yaml.indent(mapping=2, sequence=4, offset=2)
    tmp = sites_path.with_name(sites_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    tmp.replace(sites_path)


def load_sites(sites_path: Path) -> dict[str, dict[str, Any]]:
    sites = load_sites_doc(sites_path).get("sites") or {}
    return {str(k): v for k, v in sites.items() if isinstance(v, dict)}


def site_node_key(site: dict[str, Any] | None) -> str | None:
    if not site:
        return None
    raw = site.get("node")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower()


def index_sites_by_node(sites: dict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    """node key (me####) → (slug, site)."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for slug, site in sites.items():
        key = site_node_key(site)
        if key:
            out[key] = (slug, site)
    return out


def site_binding(
    node_key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
) -> tuple[str, dict[str, Any]] | None:
    """The site that points at this unit, if any."""
    if not sites:
        return None
    idx = index_sites_by_node(sites)
    candidates: list[str] = []
    if node_key:
        candidates.append(str(node_key).strip().lower())
    if node:
        uid = node.get("unit_id")
        if isinstance(uid, str) and uid.strip():
            candidates.append(uid.strip().lower())
    for cand in candidates:
        if cand in idx:
            return idx[cand]
    return None


def bind_node_to_site(
    sites: dict[str, dict[str, Any]],
    node_key: str,
    slug: str | None,
) -> None:
    """1:1 bind. Clears any previous site pointing at this unit."""
    want = node_key.strip().lower()
    for site in sites.values():
        if site_node_key(site) == want:
            site.pop("node", None)
    if slug:
        target = sites[slug]
        target["node"] = want


def parse_loc(raw: Any) -> tuple[float, float] | None:
    """``[lat, lon]`` or None when missing / placeholder."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        lat = float(raw[0])
        lon = float(raw[1])
    except (TypeError, ValueError):
        return None
    if is_placeholder_gps(lat, lon):
        return None
    return lat, lon


def site_loc(site: dict[str, Any] | None) -> tuple[float, float] | None:
    if not site:
        return None
    return parse_loc(site.get("loc"))


def bench_loc_from_doc(doc: dict[str, Any] | None) -> tuple[float, float] | None:
    """Book-level HQ loc for unbound units (sun/map). Not applied."""
    if not doc:
        return None
    return parse_loc(doc.get("bench_loc"))


def site_loc_for_unit(
    key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    bench_loc: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """Site GPS, else node loc, else book bench_loc. For sun/map samples."""
    pos = resolve_display_position(
        node or {},
        sites,
        key=key,
        doc=doc,
        bench_loc=bench_loc,
    )
    if not pos:
        return None
    return float(pos["lat"]), float(pos["lon"])


def resolve_book_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None = None,
    *,
    key: str | None = None,
) -> dict[str, Any] | None:
    """Desired radio position from the bound site loc. Apply/trust only."""
    bind = site_binding(key, node, sites)
    if not bind:
        return None
    slug, site = bind
    loc = site_loc(site)
    if not loc:
        return None
    name = site.get("name")
    return {
        "lat": loc[0],
        "lon": loc[1],
        "source": "site",
        "site": slug,
        "site_name": name.strip() if isinstance(name, str) and name.strip() else None,
    }


def resolve_display_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None = None,
    *,
    key: str | None = None,
    doc: dict[str, Any] | None = None,
    bench_loc: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """Sun/map loc: bound site, else node ``loc``, else book ``bench_loc``."""
    site_pos = resolve_book_position(node, sites, key=key)
    if site_pos:
        return site_pos
    node_loc = parse_loc((node or {}).get("loc"))
    if node_loc:
        return {
            "lat": node_loc[0],
            "lon": node_loc[1],
            "source": "node",
            "site": None,
            "site_name": None,
        }
    loc = bench_loc if bench_loc is not None else bench_loc_from_doc(doc)
    if not loc:
        return None
    return {
        "lat": loc[0],
        "lon": loc[1],
        "source": "bench",
        "site": None,
        "site_name": None,
    }


def book_coord(
    node: dict[str, Any],
    axis: str,
    sites: dict[str, dict[str, Any]] | None = None,
    *,
    key: str | None = None,
) -> float | None:
    pos = resolve_book_position(node, sites, key=key)
    if not pos:
        return None
    return float(pos[axis])


def lookup_site_name(
    site_slug: Any,
    sites: dict[str, dict[str, Any]] | None,
) -> str | None:
    """Pretty site name when the node is bound. Slug if the site has no name."""
    if not isinstance(site_slug, str) or not site_slug.strip():
        return None
    if sites:
        site = sites.get(site_slug)
        if isinstance(site, dict):
            name = site.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return site_slug


def node_alias(node: dict[str, Any] | None) -> str | None:
    """Book nickname for UI / selectors. Not pushed to the radio."""
    raw = (node or {}).get("alias")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def display_name(
    key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
) -> str:
    """Site name when bound; else book alias; else unit id."""
    unit_id = str((node or {}).get("unit_id") or (key or "").upper())
    bind = site_binding(key, node, sites)
    if bind:
        slug, _site = bind
        return lookup_site_name(slug, sites) or slug
    alias = node_alias(node)
    if alias:
        return alias
    return unit_id


def public_radio_name(
    key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    max_len: int = 32,
) -> str:
    """On-air name for public apply / trust. Uses book public_advert when doc is set."""
    from envybot.public_advert import public_radio_name as _book_public_name

    if doc is not None:
        return _book_public_name(key, node, sites, doc=doc, max_len=max_len)
    unit_id = str((node or {}).get("unit_id") or (key or "").upper())
    bind = site_binding(key, node, sites)
    if bind:
        slug, _site = bind
        name = lookup_site_name(slug, sites) or slug
        return name[:max_len]
    return unit_id[:max_len]
