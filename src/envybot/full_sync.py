"""Per-node full sync cadence (book interval + sqlite completion stamp)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

FULL_SYNC_INTERVAL_CHOICES = ("24h", "7d", "30d", "off")

_DURATION_RE = re.compile(r"^(\d+)\s*([hdw])$", re.I)

DEFAULT_FULL_SYNC_INTERVAL = "7d"


def parse_full_sync_interval_seconds(raw: Any) -> int | None:
    """Return interval seconds, or None when manual-only (off). Default 7d."""
    if raw is None or raw == "":
        return 7 * 86400
    if isinstance(raw, (int, float)):
        sec = int(raw)
        return sec if sec > 0 else None
    text = str(raw).strip().lower()
    if text in ("off", "manual", "never", "0"):
        return None
    if text in ("24h", "1d"):
        return 86400
    if text == "7d":
        return 7 * 86400
    if text == "30d":
        return 30 * 86400
    m = _DURATION_RE.match(text)
    if not m:
        return 7 * 86400
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    if unit == "w":
        return n * 7 * 86400
    return 7 * 86400


def node_full_sync_interval_label(node: dict[str, Any] | None) -> str:
    raw = (node or {}).get("full_sync_interval")
    if raw is None or raw == "":
        return DEFAULT_FULL_SYNC_INTERVAL
    text = str(raw).strip().lower()
    if text in FULL_SYNC_INTERVAL_CHOICES:
        return text
    return text or DEFAULT_FULL_SYNC_INTERVAL


def full_sync_is_due(
    conn: sqlite3.Connection,
    unit: str,
    node: dict[str, Any] | None,
    *,
    now: int,
) -> bool:
    interval = parse_full_sync_interval_seconds((node or {}).get("full_sync_interval"))
    if interval is None:
        return False
    last = last_full_sync_at(conn, unit)
    if last is None:
        return True
    return now - last >= interval


def last_full_sync_at(conn: sqlite3.Connection, unit: str) -> int | None:
    row = conn.execute(
        "SELECT full_sync_at FROM last_seen WHERE unit = ?",
        (unit.lower(),),
    ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def stamp_full_sync(conn: sqlite3.Connection, unit: str, *, ts: int | None = None) -> None:
    now = ts or int(__import__("time").time())
    conn.execute(
        "INSERT INTO last_seen (unit, updated_at, full_sync_at) VALUES (?, ?, ?) "
        "ON CONFLICT(unit) DO UPDATE SET full_sync_at = excluded.full_sync_at, "
        "updated_at = excluded.updated_at",
        (unit.lower(), now, now),
    )
