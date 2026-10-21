"""nodes.yaml writes during USB onboard."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from envybot.history import open_history, record_onboard_heard
from envybot.nodes_doc import (
    allocate_unit_id,
    is_decommissioned,
    load_nodes_doc,
    load_sites_for_book,
    remember_unit_id,
    write_nodes_doc,
)
from envybot.passwords import gen_unique_password, password_collides, password_is_strong
from envybot.position import site_binding
from envybot.repeater_serial import CliError

PUB_HEX_LEN = 64
PRV_HEX_LEN = 128
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def find_unit_by_pubkey(nodes: dict[str, Any], pubkey: str) -> str | None:
    want = pubkey.strip().lower()
    for key, node in nodes.items():
        have = str(node.get("identity_pubkey") or "").strip().lower()
        if have and have == want:
            return key
    return None


def stored_admin_password(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    val = node.get("admin_password")
    if not password_is_strong(val):
        return None
    return str(val).strip()


def stored_guest_password(node: dict[str, Any] | None) -> str | None:
    if not node or "guest_password" not in node:
        return None
    val = node.get("guest_password")
    if val is None:
        return None
    return str(val)


BOARD_TO_FLASH_TARGET: dict[str, str] = {
    "rak4631": "rak4631-repeater-slim",
    "heltec-t096": "sensecap-p1pro-repeater-slim",
    "t096": "sensecap-p1pro-repeater-slim",
}


def flash_target_from_board(board: str | None) -> str | None:
    if not board:
        return None
    return BOARD_TO_FLASH_TARGET.get(str(board).strip().lower())


def board_token_from_flash_target(slug: str | None) -> str | None:
    if not slug:
        return None
    key = slug.strip().lower()
    if key.startswith("rak4631"):
        return "rak4631"
    if "sensecap" in key or "p1pro" in key:
        return "heltec-t096"
    return None


def format_neighbors(neighbors: list[dict[str, Any]]) -> str:
    if not neighbors:
        return "none"
    parts = [f"{n['pubkey']} {n['snr']:g}dB {n['secs_ago']}s" for n in neighbors]
    return f"{len(neighbors)}: " + ", ".join(parts)


def merge_onboard_notes(
    node: dict[str, Any],
    result: dict[str, Any],
    *,
    ts: int,
) -> str | None:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bits = [f"Onboard {dt}"]
    fw = result.get("firmware_version")
    if fw:
        bits.append(f"fw {fw}")
    bl = result.get("bootloader_version")
    if bl:
        bits.append(f"bl {bl}")
    base = result.get("base_hash")
    if base:
        bits.append(f"base {base}")
    neighbors = result.get("neighbors")
    if neighbors is not None:
        bits.append(f"neighbors {format_neighbors(neighbors)}")
    line = "; ".join(bits)
    prev = str(node.get("notes") or "").strip()
    if not prev:
        return line
    if line in prev:
        return prev or None
    first, _, rest = prev.partition("\n")
    if first.startswith("Onboard ") and neighbors is not None and "neighbors " not in first:
        upgraded = f"{first}; neighbors {format_neighbors(neighbors)}"
        return upgraded if not rest else f"{upgraded}\n{rest}"
    if first.startswith("Onboard "):
        return prev
    return f"{prev}\n{line}"


def apply_onboard_book_fields(
    node: dict[str, Any],
    result: dict[str, Any],
    *,
    ts: int,
    flash_target: str | None,
) -> None:
    fw = result.get("firmware_version")
    if fw:
        node["firmware_version"] = fw
    bl = result.get("bootloader_version")
    if bl:
        node["bootloader_version"] = bl
    board = result.get("board") or board_token_from_flash_target(flash_target)
    if board and not str(node.get("board") or "").strip():
        node["board"] = board
    if node.get("onboarded_at") in (None, ""):
        node["onboarded_at"] = ts
    neighbors = result.get("neighbors")
    if neighbors is not None:
        node["onboard_neighbors"] = neighbors
    merged = merge_onboard_notes(node, result, ts=ts)
    node["notes"] = merged or None


def new_node(unit_id: str) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "firmware_platform": "meshcore",
        "firmware_version": None,
        "bootloader_version": None,
        "board": None,
        "onboarded_at": None,
        "onboard_neighbors": None,
        "last_admin_roll": None,
        "last_guest_roll": None,
        "last_key_roll": None,
        "notes": None,
        "admin_password": None,
        "guest_password": None,
        "identity_pubkey": None,
        "identity_secret": None,
        "admin1_pubkey": None,
        "admin1_secret": None,
        "decommissioned": None,
    }


def generate_identity_secret() -> str:
    return secrets.token_hex(PRV_HEX_LEN // 2)


def should_mint_fleet_identity(
    node: dict[str, Any] | None,
    usb_pubkey: str,
    *,
    created: bool,
) -> bool:
    """Mint a new mesh keypair on USB (``onboard`` only), not idempotent re-plug."""
    if created:
        return True
    if not node:
        return True
    book_pub = str(node.get("identity_pubkey") or "").strip().lower()
    if not book_pub:
        return True
    return book_pub != usb_pubkey.strip().lower()


def normalize_hex(text: str, expect_len: int, label: str) -> str:
    hexed = re.sub(r"\s+", "", text).lower()
    if not HEX_RE.match(hexed) or len(hexed) != expect_len:
        raise CliError(f"{label}: expected {expect_len} hex chars, got {text!r}")
    return hexed


def load_registry(nodes_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not nodes_path.is_file():
        raise SystemExit(f"nodes.yaml not found: {nodes_path}")
    doc = load_nodes_doc(nodes_path)
    nodes: dict[str, Any] = doc.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        doc["nodes"] = nodes
    from envybot.book_dal import raise_on_book_errors, validate_book

    sites = load_sites_for_book(nodes_path)
    raise_on_book_errors(validate_book(doc, nodes, sites))
    return doc, nodes


def resolve_unit(
    nodes: dict[str, Any],
    pubkey: str,
    *,
    unit: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    existing = find_unit_by_pubkey(nodes, pubkey)
    if existing and is_decommissioned(nodes.get(existing)):
        raise SystemExit(f"{existing}: decommissioned")
    if unit:
        key = unit.lower()
        if existing and existing != key:
            raise SystemExit(f"pubkey already registered as {existing}; not using --unit {key}")
        node = nodes.get(key)
        if is_decommissioned(node):
            raise SystemExit(f"{key}: decommissioned")
        have = str((node or {}).get("identity_pubkey") or "").strip().lower()
        want = pubkey.strip().lower()
        if have and have != want:
            raise SystemExit(
                f"--unit {key} pubkey does not match USB "
                f"(yaml {have[:8]}…, USB {want[:8]}…; use reonboard)"
            )
        return key, node
    if existing:
        return existing, nodes[existing]
    return None, None


def ensure_reonboard_unit(
    nodes: dict[str, Any],
    *,
    unit: str | None,
) -> tuple[str, dict[str, Any]]:
    if not unit:
        raise SystemExit("reonboard requires --unit")
    key = unit.lower()
    node = nodes.get(key)
    if not isinstance(node, dict):
        raise SystemExit(f"{key}: no such row in nodes.yaml")
    if is_decommissioned(node):
        raise SystemExit(f"{key}: decommissioned")
    platform = str(node.get("firmware_platform") or "meshcore").strip().lower()
    if platform == "meshtastic":
        raise SystemExit(f"{key}: meshtastic row (envybot skips USB reonboard)")
    return key, node


def ensure_onboard_unit(
    doc: dict[str, Any],
    nodes: dict[str, Any],
    pubkey: str,
    *,
    unit: str | None,
) -> tuple[str, dict[str, Any], bool]:
    key, node = resolve_unit(nodes, pubkey, unit=unit)
    if key is not None:
        if node is None:
            unit_id = key.upper()
            node = new_node(unit_id)
            return key, node, key not in nodes
        return key, node, False
    unit_id = allocate_unit_id(doc)
    key = unit_id.lower()
    return key, new_node(unit_id), True


def resolve_passwords(
    cli: Any,
    node: dict[str, Any] | None,
    *,
    admin_pw: str | None,
    guest_pw: str | None,
    doc: dict[str, Any] | None = None,
    key: str = "",
) -> tuple[str, str, str, str]:
    book = doc or {}
    stored_admin = stored_admin_password(node)
    if admin_pw is not None:
        admin, admin_src = admin_pw, "cli"
    elif stored_admin and not password_collides(
        stored_admin, book, key, field="admin_password"
    ):
        admin, admin_src = stored_admin, "yaml"
    else:
        admin, admin_src = gen_unique_password(book, key, field="admin_password"), "new"
    if not admin:
        raise CliError("admin password cannot be empty")

    stored_guest = stored_guest_password(node)
    if guest_pw is not None:
        guest, guest_src = guest_pw, "cli"
    elif stored_guest is not None:
        guest, guest_src = stored_guest, "yaml"
    else:
        guest, guest_src = "", "default"
    return admin, guest, admin_src, guest_src


def register(
    nodes_path: Path,
    result: dict[str, Any],
    *,
    unit: str | None,
    flash_target: str | None = None,
) -> tuple[str, bool]:
    doc, nodes = load_registry(nodes_path)
    sites = load_sites_for_book(nodes_path)
    pubkey = result["pubkey"]
    existing = find_unit_by_pubkey(nodes, pubkey)
    if existing and is_decommissioned(nodes.get(existing)):
        raise SystemExit(f"{existing}: decommissioned")
    if unit:
        key = unit.lower()
        if existing and existing != key:
            raise SystemExit(
                f"pubkey already registered as {existing}; not writing --unit {key}"
            )
        if is_decommissioned(nodes.get(key)):
            raise SystemExit(f"{key}: decommissioned")
        created = key not in nodes
        unit_id = key.upper()
        node = nodes.get(key) or new_node(unit_id)
        have = str(node.get("identity_pubkey") or "").strip().lower()
        want = pubkey.strip().lower()
        if have and have != want:
            raise SystemExit(
                f"{key}: book pubkey {have[:8]}… != USB {want[:8]}… after session"
            )
        node["unit_id"] = unit_id
        nodes[key] = node
        remember_unit_id(doc, unit_id)
    elif existing:
        key = existing
        created = False
        node = nodes[key]
        if not node.get("unit_id"):
            node["unit_id"] = key.upper()
    else:
        unit_id = allocate_unit_id(doc)
        key = unit_id.lower()
        created = True
        node = new_node(unit_id)
        nodes[key] = node

    now = int(time.time())
    node.pop("name", None)
    node["firmware_platform"] = result["firmware_platform"]
    node["admin_password"] = result["admin_password"]
    node["guest_password"] = result["guest_password"]
    node["identity_pubkey"] = result["pubkey"]
    node["identity_secret"] = result["secret"]
    node.pop("lat", None)
    node.pop("lon", None)
    if result.get("admin_changed"):
        node["last_admin_roll"] = now
    if result.get("guest_changed"):
        node["last_guest_roll"] = now
    if site_binding(key, node, sites) is None:
        node.pop("site", None)
    if "repeat" not in node:
        node["repeat"] = False
    apply_onboard_book_fields(node, result, ts=now, flash_target=flash_target)
    write_nodes_doc(nodes_path, doc)
    conn = open_history(nodes_path.parent)
    record_onboard_heard(
        conn,
        unit=key,
        firmware_version=result.get("firmware_version"),
        bootloader_version=result.get("bootloader_version"),
        base_hash=result.get("base_hash"),
        firmware_platform=result.get("firmware_platform"),
        node_clock=result.get("node_clock"),
        neighbors=result.get("neighbors"),
        ts=now,
    )
    conn.close()
    return key, created
