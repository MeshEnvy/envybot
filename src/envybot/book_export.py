"""Book YAML → CSV export rows (trusted admin; includes credentials)."""

from __future__ import annotations

import csv
from typing import Any, TextIO

from envybot.nodes_doc import is_meshcore_platform
from envybot.position import parse_loc, site_node_key
from envybot.unit_filter import parse_unit_specs

SITE_EXPORT_COLUMNS = (
    "site",
    "site_name",
    "site_lat",
    "site_lon",
    "admin_password",
    "identity_pubkey",
    "height_m",
    "advert_name",
    "unit_id",
    "board",
    "firmware_version",
    "bootloader_version",
    "advert_interval_min",
    "flood_advert_interval_h",
    "path_hash_mode",
    "dutycycle",
    "powersaving",
    "hop_retry",
    "hop_retry_ms",
    "fem_rxgain",
    "agc_reset_interval",
    "rxgain",
    "guest_password",
    "identity_secret",
)

_NODE_FIELDS = (
    "unit_id",
    "board",
    "firmware_version",
    "bootloader_version",
    "path_hash_mode",
    "dutycycle",
    "powersaving",
    "fem_rxgain",
    "rxgain",
    "admin_password",
    "guest_password",
    "identity_pubkey",
    "identity_secret",
)

_NODE_INT_FIELDS = (
    "advert_interval_min",
    "flood_advert_interval_h",
    "hop_retry",
    "hop_retry_ms",
    "agc_reset_interval",
)


def parse_tags_arg(values: list[str] | None) -> frozenset[str]:
    """Flatten --tags values (comma-separated) to lowercase tag set."""
    tokens = parse_unit_specs(values)
    return frozenset(t.lower() for t in tokens)


def _site_tag_set(site: dict[str, Any]) -> set[str]:
    raw = site.get("tags")
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().lower() for t in raw if str(t).strip()}


def site_matches_tags(site: dict[str, Any], required: frozenset[str]) -> bool:
    if not required:
        return True
    return required <= _site_tag_set(site)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _site_loc(site: dict[str, Any]) -> tuple[str, str]:
    loc = parse_loc(site.get("loc"))
    if not loc:
        return "", ""
    return _cell(loc[0]), _cell(loc[1])


def _node_decommissioned(node: dict[str, Any]) -> bool:
    val = node.get("decommissioned")
    if val is None:
        return False
    if val is False:
        return False
    if val == 0:
        return False
    return True


def _should_skip_bound_node(node: dict[str, Any]) -> bool:
    if _node_decommissioned(node):
        return True
    return not is_meshcore_platform(node)


def _node_cells(node: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in _NODE_FIELDS:
        if key in node:
            out[key] = _cell(node.get(key))
    for key in _NODE_INT_FIELDS:
        if key in node and node.get(key) is not None:
            out[key] = _cell(node.get(key))
    return out


def _site_cells(slug: str, site: dict[str, Any]) -> dict[str, str]:
    lat, lon = _site_loc(site)
    return {
        "site": slug,
        "site_name": _cell(site.get("name")),
        "site_lat": lat,
        "site_lon": lon,
        "height_m": _cell(site.get("height_m")),
        "advert_name": _cell(site.get("advert_name")),
    }


def iter_site_export_rows(
    doc: dict[str, Any],
    sites: dict[str, dict[str, Any]],
    *,
    required_tags: frozenset[str],
) -> list[dict[str, str]]:
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        nodes = {}

    rows: list[dict[str, str]] = []
    for slug in sorted(sites.keys()):
        site = sites[slug]
        if not isinstance(site, dict):
            continue
        if not site_matches_tags(site, required_tags):
            continue

        node_key = site_node_key(site)
        node = nodes.get(node_key) if node_key else None
        if isinstance(node, dict) and _should_skip_bound_node(node):
            continue

        row = {col: "" for col in SITE_EXPORT_COLUMNS}
        row.update(_site_cells(slug, site))
        if isinstance(node, dict):
            row.update(_node_cells(node))
        rows.append(row)
    return rows


def write_csv(rows: list[dict[str, str]], out: TextIO) -> None:
    writer = csv.DictWriter(
        out,
        fieldnames=list(SITE_EXPORT_COLUMNS),
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in SITE_EXPORT_COLUMNS})
