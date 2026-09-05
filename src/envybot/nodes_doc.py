"""Desired-state nodes.yaml. Observed telemetry does not live here."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from envybot.position import load_sites

HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UNIT_NUM_RE = re.compile(r"^me(\d+)$", re.I)
PLACEHOLDER_PW = frozenset({"<mt>", "<bear changed to mt>"})
MASK_NAME = "Repeater"


def is_meshcore_platform(node: dict[str, Any] | None) -> bool:
    """Fleet/trust/cmd only talk MeshCore. Blank platform counts as meshcore."""
    platform = str((node or {}).get("firmware_platform") or "").strip().lower()
    return platform in ("", "meshcore")

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
    "# One entry per physical unit (ME####): identity, credentials,\n"
    "# optional public: true. Apply GPS from sites.yaml when bound + public.\n"
    "# bench_loc: book HQ for unbound units (map/sun/history). Fleet UI may update it.\n"
    "# Per-node loc: [lat, lon] overrides bench_loc when unbound. Not pushed to radio.\n"
    "# Site loc wins when the unit is linked in sites.yaml.\n"
    "# Observed last-seen / telemetry live in data/fleet/history.sqlite.\n"
    "# Display name: sites.yaml name when bound, else alias, else unit_id.\n"
    "# alias is UI/selector only (not pushed to the radio). Default apply SETs\n"
    "# Repeater + 0,0 + adverts off unless public: true (then site name + GPS).\n"
    "# path_hash_mode / dutycycle: radio prefs (apply/onboard default 1 / 50).\n"
    "# ota_autofetch: off|any|signed (apply/onboard default off).\n"
    "# powersaving / fem_rxgain / fem_vfem / rxgain: optional on|off. Apply SETs only when present.\n"
    "#   fem_rxgain is T096 LNA; fem_vfem is VFEM bias during RX; rxgain is SX1262 boosted gain.\n"
    "#   Missing CLI (unsupported / unknown config) stamps done.\n"
    "# trust.admin / trust.guest: people from keys.yaml (MeshCore ACL).\n"
    "# admin1_pubkey / admin1_secret: Meshtastic remote-admin. Not MC ACL.\n"
    "# firmware_platform: meshcore | meshtastic. Meshtastic rows stay in the\n"
    "#   book; envybot ignores them (no UI, poll, apply, trust, cmd).\n"
    "# paused: true skips auto fleet poll/apply. Still in the UI. Refresh/Pull/Push override.\n"
    "# stability_ack_ts: unix epoch when reboot warning was dismissed in the UI.\n"
    "#   Ignores earlier uptime drops until the next reboot.\n"
    "# routing: direct | path | flood — mesh send policy (default path when omitted).\n"
    "#   path: use cached route; flood-login to discover; discard stale cache after 3 fails.\n"
    "#   direct: always zero-hop. flood: always flood (danger — high airtime).\n"
    "# decommissioned: unix epoch when pulled from service. Envybot ignores the row.\n"
    "# next_unit: next free ME number (never reuse).\n"
    "# admin_password / guest_password: unique + strong per unit. Privacy apply\n"
    "#   rolls blank, weak, or colliding guest passwords (never reuse m35h3nvy).\n"
    "# Apply due = per-field sqlite applies (legacy profile row still honored).\n"
    "#   Poll: status/telemetry hourly, neighbors daily; audit GET on --force.\n"
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


def is_paused(node: dict[str, Any] | None) -> bool:
    """True when the book stamps paused: true (auto fleet poll/apply off)."""
    return bool(node and node.get("paused") is True)


def sync_paused(nodes_path: Path, nodes: dict[str, Any]) -> None:
    """Copy UI book fields from disk so in-memory persist does not clobber them."""
    try:
        fresh = load_nodes_doc(nodes_path)
    except OSError:
        return
    disk_nodes = fresh.get("nodes") or {}
    if not isinstance(disk_nodes, dict):
        return
    for key, mem in nodes.items():
        if not isinstance(mem, dict):
            continue
        disk = disk_nodes.get(key)
        if not isinstance(disk, dict):
            continue
        if disk.get("paused") is True:
            mem["paused"] = True
        else:
            mem.pop("paused", None)
        for field in ("routing", "alias", "notes"):
            val = disk.get(field)
            if field == "routing":
                if isinstance(val, str) and val.strip():
                    mem["routing"] = val.strip().lower()
                else:
                    mem.pop("routing", None)
                continue
            if isinstance(val, str) and val.strip():
                mem[field] = val.strip() if field == "alias" else val
            else:
                mem.pop(field, None)
        for field in ("powersaving", "fem_rxgain", "fem_vfem", "rxgain"):
            if field in disk:
                mem[field] = disk[field]
            else:
                mem.pop(field, None)


def is_decommissioned(node: dict[str, Any] | None) -> bool:
    """True when the book stamps a pull-from-service epoch (blank key is live)."""
    if not node:
        return False
    return node.get("decommissioned") not in (None, "", False)


def migrate_routing(node: dict[str, Any]) -> bool:
    """One-shot: ``flood: true`` → ``routing: flood``; drop obsolete key.

    Not a runtime dual-path. Call from migrate_desired / normalize so the
    book is rewritten once, then only ``routing`` remains.
    """
    changed = False
    if node.get("flood") is True:
        if not isinstance(node.get("routing"), str) or not str(node.get("routing")).strip():
            node["routing"] = "flood"
        node.pop("flood", None)
        changed = True
    elif "flood" in node:
        node.pop("flood", None)
        changed = True
    return changed


def normalize_fleet_node(node: dict[str, Any]) -> None:
    """Drop obsolete keys (greenfield — no field aliasing)."""
    strip_observed(node)
    migrate_routing(node)


def strip_observed(node: dict[str, Any]) -> bool:
    changed = False
    for key in OBSERVED_KEYS:
        if key in node:
            node.pop(key, None)
            changed = True
    return changed


def drop_node_location(node: dict[str, Any]) -> bool:
    """Strip legacy site bind on the node row. Per-node loc is kept."""
    changed = False
    if "site" in node:
        node.pop("site", None)
        changed = True
    return changed


def migrate_desired(doc: dict[str, Any], sites: dict[str, dict[str, Any]]) -> bool:
    """Strip observed YAML keys and leftover node GPS/site. Do not stamp public."""
    del sites
    changed = False
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        return False
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        if strip_observed(node):
            changed = True
        if migrate_routing(node):
            changed = True
        if drop_node_location(node):
            changed = True
        if "name" in node:
            node.pop("name", None)
            changed = True
    return changed


def load_sites_for_book(nodes_path: Path) -> dict[str, dict[str, Any]]:
    return load_sites(nodes_path.parent / "sites.yaml")
