"""Desired-state nodes.yaml. Observed telemetry does not live here."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from envybot.position import is_placeholder_gps, load_sites, site_loc

HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UNIT_NUM_RE = re.compile(r"^me(\d+)$", re.I)
PLACEHOLDER_PW = frozenset({"<mt>", "<bear changed to mt>"})
MASK_NAME = "Repeater"
GPS_MATCH_EPS = 0.0001

OBSERVED_KEYS = (
    "status",
    "telemetry",
    "acl",
    "neighbors",
    "firmware_version",
    "bootloader_version",
    "node_clock",
    "firmware_pulled_at",
    "bootloader_pulled_at",
    "name_pulled_at",
    "lat_pulled_at",
    "lon_pulled_at",
    "advert_pulled_at",
    "flood_advert_pulled_at",
    "path_hash_pulled_at",
    "dutycycle_pulled_at",
    "status_pulled_at",
    "telemetry_pulled_at",
    "acl_pulled_at",
    "neighbors_pulled_at",
    "live",
    "last_version_poll",
    "basic_pulled_at",
    "features",
    "position_pulled_at",
)

NODES_YAML_HEADER = (
    "# MeshEnvy fleet nodes — desired identity (private).\n"
    "# One entry per physical unit (ME####): identity, credentials, site bind,\n"
    "# optional public: true, optional lat/lon book override.\n"
    "# Observed last-seen / telemetry live in data/fleet/history.sqlite.\n"
    "# name: book radio name (later a codename). Pushed to the radio only when\n"
    "#   public: true. Default apply SETs Repeater + 0,0 + adverts off.\n"
    "# site: sites.yaml slug, or null while in the bag / decommissioned.\n"
    "# lat/lon: book GPS override only. Inherit site loc otherwise.\n"
    "# path_hash_mode / dutycycle: radio prefs (apply still enforces 1 / 100).\n"
    "# trust.companions: companion pubkeys for field ACLs.\n"
    "# admin1_pubkey: optional per-unit extra ACL key.\n"
    "# firmware_platform: meshcore | meshtastic.\n"
    "# decommissioned: unix epoch when pulled from service.\n"
    "# next_unit: next free ME number (never reuse).\n"
    "# Tool: envybot. Fleet: ./envybot fleet. Trust: ./envybot trust.\n"
    "# Never copy secrets (passwords, keypairs) into public trees.\n"
)


def load_nodes_doc(nodes_path: Path) -> dict[str, Any]:
    yaml = YAML()
    raw = nodes_path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            start = i
            break
    payload = "".join(lines[start:])
    return yaml.load(payload) or {}


def max_unit_num(nodes: dict[str, Any] | None) -> int:
    nums: list[int] = []
    for key, node in (nodes or {}).items():
        m = UNIT_NUM_RE.match(str(key))
        if m:
            nums.append(int(m.group(1)))
        if isinstance(node, dict):
            m2 = UNIT_NUM_RE.match(str(node.get("unit_id") or ""))
            if m2:
                nums.append(int(m2.group(1)))
    return max(nums) if nums else 0


def ensure_next_unit(doc: dict[str, Any]) -> int:
    floor = max_unit_num(doc.get("nodes") or {}) + 1
    try:
        claimed = int(doc.get("next_unit") or 0)
    except (TypeError, ValueError):
        claimed = 0
    nxt = max(floor, claimed, 1)
    doc["next_unit"] = nxt
    return nxt


def allocate_unit_id(doc: dict[str, Any]) -> str:
    n = ensure_next_unit(doc)
    doc["next_unit"] = n + 1
    return f"ME{n:04d}"


def remember_unit_id(doc: dict[str, Any], unit_id: str) -> None:
    m = UNIT_NUM_RE.match(unit_id.strip())
    if not m:
        return
    n = int(m.group(1))
    doc["next_unit"] = max(ensure_next_unit(doc), n + 1)


def write_nodes_doc(nodes_path: Path, doc: dict[str, Any]) -> None:
    ensure_next_unit(doc)
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    ordered: dict[str, Any] = {"next_unit": doc["next_unit"]}
    for key, val in doc.items():
        if key == "next_unit":
            continue
        ordered[key] = val
    tmp_path = nodes_path.with_name(nodes_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(NODES_YAML_HEADER)
        yaml.dump(ordered, fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(nodes_path)


def is_public(node: dict[str, Any] | None) -> bool:
    return bool(node and node.get("public") is True)


def normalize_fleet_node(node: dict[str, Any]) -> None:
    """Drop obsolete keys (greenfield — no field aliasing)."""
    strip_observed(node)


def strip_observed(node: dict[str, Any]) -> bool:
    changed = False
    for key in OBSERVED_KEYS:
        if key in node:
            node.pop(key, None)
            changed = True
    return changed


def _coords_match(a: float, b: float) -> bool:
    return abs(a - b) < GPS_MATCH_EPS


def drop_redundant_gps(node: dict[str, Any], sites: dict[str, dict[str, Any]]) -> bool:
    """Delete node lat/lon when they match the bound site loc."""
    slug = node.get("site")
    if not isinstance(slug, str) or not slug:
        return False
    loc = site_loc(sites.get(slug))
    if loc is None:
        return False
    lat, lon = node.get("lat"), node.get("lon")
    if lat is None or lon is None:
        return False
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if is_placeholder_gps(la, lo):
        node.pop("lat", None)
        node.pop("lon", None)
        return True
    if _coords_match(la, loc[0]) and _coords_match(lo, loc[1]):
        node.pop("lat", None)
        node.pop("lon", None)
        return True
    return False


def migrate_desired(doc: dict[str, Any], sites: dict[str, dict[str, Any]]) -> bool:
    """Strip observed YAML keys and redundant site-copy GPS. Do not stamp public."""
    changed = False
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        return False
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if strip_observed(node):
            changed = True
        if drop_redundant_gps(node, sites):
            changed = True
        if node.get("name") == MASK_NAME:
            node.pop("name", None)
            changed = True
    return changed


def book_companion_pubkeys(doc: dict[str, Any]) -> list[str]:
    """Companion pubkeys from the book-level trust allowlist."""
    trust = doc.get("trust")
    raw: list[Any] = []
    if isinstance(trust, dict):
        raw = trust.get("companions") or []
    elif isinstance(doc.get("authorized"), list):
        raw = doc["authorized"]
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            pk = item.strip().lower()
        elif isinstance(item, dict):
            pk = str(item.get("pubkey") or item.get("identity_pubkey") or "").strip().lower()
        else:
            continue
        if HEX_PUBKEY_RE.match(pk):
            out.append(pk)
    return out


def extra_admin_pubkeys(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("admin1_pubkey", "admin_pubkey"):
        val = node.get(key)
        if isinstance(val, str) and HEX_PUBKEY_RE.match(val.strip()):
            out.append(val.strip().lower())
    return out


def desired_acl_pubkeys(doc: dict[str, Any], node: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for pk in book_companion_pubkeys(doc) + extra_admin_pubkeys(node):
        if pk not in seen:
            seen.add(pk)
            ordered.append(pk)
    return ordered


def load_sites_for_book(nodes_path: Path) -> dict[str, dict[str, Any]]:
    return load_sites(nodes_path.parent / "sites.yaml")
