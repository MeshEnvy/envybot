"""Book-canonical GPS. Device coords are never the source of truth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ONBOARD_LAT = 14.009295
ONBOARD_LON = 120.996018


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


def load_sites(sites_path: Path) -> dict[str, dict[str, Any]]:
    if not sites_path.is_file():
        return {}
    yaml = YAML(typ="safe")
    doc = yaml.load(sites_path.read_text(encoding="utf-8")) or {}
    sites = doc.get("sites") or {}
    return {str(k): v for k, v in sites.items() if isinstance(v, dict)}


def site_loc(site: dict[str, Any] | None) -> tuple[float, float] | None:
    if not site:
        return None
    loc = site.get("loc")
    if not isinstance(loc, (list, tuple)) or len(loc) < 2:
        return None
    try:
        lat = float(loc[0])
        lon = float(loc[1])
    except (TypeError, ValueError):
        return None
    if is_placeholder_gps(lat, lon):
        return None
    return lat, lon


def resolve_book_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Desired radio position from the book. Node coords beat site loc."""
    lat = node.get("lat")
    lon = node.get("lon")
    if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
        try:
            return {"lat": float(lat), "lon": float(lon), "source": "node"}
        except (TypeError, ValueError):
            pass
    site_slug = node.get("site")
    if site_slug and isinstance(site_slug, str) and sites:
        loc = site_loc(sites.get(site_slug))
        if loc:
            site = sites.get(site_slug) or {}
            return {
                "lat": loc[0],
                "lon": loc[1],
                "source": "site",
                "site_name": site.get("name"),
            }
    return None


def book_coord(
    node: dict[str, Any],
    axis: str,
    sites: dict[str, dict[str, Any]] | None = None,
) -> float | None:
    pos = resolve_book_position(node, sites)
    if not pos:
        return None
    return float(pos[axis])
