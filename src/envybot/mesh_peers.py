"""SQLite mesh_peers registry: off-book identity and mesh liveness."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

MESH_PEER_COLS = (
    "pubkey",
    "adv_name",
    "adv_lat",
    "adv_lon",
    "last_mesh_seen_at",
    "last_mesh_seen_by",
    "identity_at",
    "identity_source",
    "book_unit",
    "updated_at",
)

_MESH_PEER_UPDATE_PARTS = (
    "adv_name=CASE WHEN excluded.adv_name IS NOT NULL AND excluded.adv_name != '' "
    "THEN excluded.adv_name ELSE mesh_peers.adv_name END",
    "adv_lat=COALESCE(excluded.adv_lat, mesh_peers.adv_lat)",
    "adv_lon=COALESCE(excluded.adv_lon, mesh_peers.adv_lon)",
    "last_mesh_seen_at=CASE WHEN excluded.last_mesh_seen_at IS NOT NULL "
    "AND (mesh_peers.last_mesh_seen_at IS NULL "
    "OR excluded.last_mesh_seen_at >= mesh_peers.last_mesh_seen_at) "
    "THEN excluded.last_mesh_seen_at ELSE mesh_peers.last_mesh_seen_at END",
    "last_mesh_seen_by=CASE WHEN excluded.last_mesh_seen_at IS NOT NULL "
    "AND (mesh_peers.last_mesh_seen_at IS NULL "
    "OR excluded.last_mesh_seen_at >= mesh_peers.last_mesh_seen_at) "
    "THEN excluded.last_mesh_seen_by ELSE mesh_peers.last_mesh_seen_by END",
    "book_unit=excluded.book_unit",
    "identity_at=COALESCE(excluded.identity_at, mesh_peers.identity_at)",
    "identity_source=CASE WHEN excluded.identity_source IS NOT NULL "
    "THEN excluded.identity_source ELSE mesh_peers.identity_source END",
    "updated_at=excluded.updated_at",
)

_MESH_PEER_UPSERT_SQL = (
    f"INSERT INTO mesh_peers ({', '.join(MESH_PEER_COLS)}) "
    f"VALUES ({', '.join('?' for _ in MESH_PEER_COLS)}) "
    f"ON CONFLICT(pubkey) DO UPDATE SET {', '.join(_MESH_PEER_UPDATE_PARTS)}"
)


def normalize_pubkey_hex(raw: Any) -> str:
    return str(raw or "").strip().lower()


def resolve_peer_storage_key(conn: sqlite3.Connection, pubkey: str) -> str:
    """Prefer an existing full-key row when a neighbor prefix matches one."""
    pk = normalize_pubkey_hex(pubkey)
    if not pk:
        return pk
    if conn.execute("SELECT 1 FROM mesh_peers WHERE pubkey = ?", (pk,)).fetchone():
        return pk
    if len(pk) <= 8:
        rows = conn.execute(
            "SELECT pubkey FROM mesh_peers WHERE pubkey LIKE ? AND length(pubkey) > ?",
            (pk + "%", len(pk)),
        ).fetchall()
        if len(rows) == 1:
            return str(rows[0][0])
        if len(rows) > 1:
            return max(str(r[0]) for r in rows)
    return pk


def _mesh_peer_row(conn: sqlite3.Connection, pubkey: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM mesh_peers WHERE pubkey = ?", (pubkey,)).fetchone()
    return dict(row) if row else None


def _write_mesh_peer_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(_MESH_PEER_UPSERT_SQL, [row.get(c) for c in MESH_PEER_COLS])


def _promote_prefix_peer(conn: sqlite3.Connection, full_pubkey: str) -> None:
    """Merge a prefix-only row into a full companion pubkey and drop the prefix row."""
    pk = normalize_pubkey_hex(full_pubkey)
    if len(pk) <= 8:
        return
    prefix = pk[:8]
    if prefix == pk:
        return
    old = _mesh_peer_row(conn, prefix)
    if old is None or old.get("pubkey") != prefix:
        return
    now = int(time.time())
    row: dict[str, Any] = {"pubkey": pk, "updated_at": now}
    for col in MESH_PEER_COLS:
        if col in ("pubkey", "updated_at"):
            continue
        val = old.get(col)
        if val is not None or col == "book_unit":
            row[col] = val
    _write_mesh_peer_row(conn, row)
    conn.execute("DELETE FROM mesh_peers WHERE pubkey = ?", (prefix,))


def _upsert_mesh_peer(conn: sqlite3.Connection, pubkey: str, fields: dict[str, Any]) -> None:
    pk = normalize_pubkey_hex(pubkey)
    if not pk:
        return
    if len(pk) > 8:
        _promote_prefix_peer(conn, pk)
    storage = resolve_peer_storage_key(conn, pk)
    now = int(fields.get("updated_at") or time.time())
    existing = _mesh_peer_row(conn, storage) or {"pubkey": storage}
    row: dict[str, Any] = dict(existing)
    row["pubkey"] = storage
    row["updated_at"] = now
    for key, val in fields.items():
        if key in ("pubkey", "updated_at"):
            continue
        if val is not None or key == "book_unit":
            row[key] = val
    _write_mesh_peer_row(conn, row)


def upsert_mesh_peers_from_contacts(
    conn: sqlite3.Connection, contacts: Any, *, commit: bool = True
) -> None:
    """Refresh heard identity from companion contacts."""
    if not isinstance(contacts, dict):
        return
    from envybot.position import is_placeholder_gps

    now = int(time.time())
    for key, raw in contacts.items():
        if not isinstance(raw, dict):
            continue
        pk = normalize_pubkey_hex(raw.get("public_key") or key)
        if not pk:
            continue
        fields: dict[str, Any] = {
            "identity_at": now,
            "identity_source": "companion",
            "updated_at": now,
        }
        name = str(raw.get("adv_name") or "").strip()
        if name:
            fields["adv_name"] = name
        try:
            lat = float(raw.get("adv_lat"))
            lon = float(raw.get("adv_lon"))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
            fields["adv_lat"] = lat
            fields["adv_lon"] = lon
        _upsert_mesh_peer(conn, pk, fields)
    if commit:
        conn.commit()


def touch_mesh_peers_from_neighbor_poll(
    conn: sqlite3.Connection,
    *,
    observer_unit: str,
    ts: int,
    neighbors: Any,
    commit: bool = False,
) -> None:
    """Record mesh liveness from a neighbor GET (no name or GPS in payload)."""
    if not isinstance(neighbors, list):
        return
    unit = str(observer_unit).lower()
    for item in neighbors:
        if not isinstance(item, dict):
            continue
        pk = normalize_pubkey_hex(item.get("pubkey"))
        if not pk:
            continue
        storage = resolve_peer_storage_key(conn, pk)
        _upsert_mesh_peer(
            conn,
            storage,
            {
                "last_mesh_seen_at": ts,
                "last_mesh_seen_by": unit,
                "updated_at": ts,
            },
        )
    if commit:
        conn.commit()


def sync_mesh_peers_book_units(conn: sqlite3.Connection, nodes: dict[str, Any]) -> None:
    """Link mesh_peers rows to managed book units by identity_pubkey."""
    from envybot.nodes_doc import is_decommissioned, is_meshcore_platform

    now = int(time.time())
    active: dict[str, str] = {}
    if isinstance(nodes, dict):
        for key, node in nodes.items():
            if not isinstance(node, dict) or is_decommissioned(node):
                continue
            if not is_meshcore_platform(node):
                continue
            pk = normalize_pubkey_hex(node.get("identity_pubkey"))
            if pk:
                active[pk] = str(key).lower()
    conn.execute("UPDATE mesh_peers SET book_unit = NULL WHERE book_unit IS NOT NULL")
    for pk, unit_key in active.items():
        existing = _mesh_peer_row(conn, resolve_peer_storage_key(conn, pk))
        fields: dict[str, Any] = {
            "book_unit": unit_key,
            "updated_at": now,
        }
        if existing is None:
            fields["identity_source"] = "book"
            fields["identity_at"] = now
        _upsert_mesh_peer(conn, pk, fields)
    conn.commit()


def load_mesh_peer_lookup(
    conn: sqlite3.Connection,
    heard_pubkeys: set[str] | None = None,
) -> tuple[
    dict[str, str],
    dict[str, tuple[float, float]],
    dict[str, dict[str, Any]],
]:
    """Load names, GPS, and mesh liveness keyed by stored pubkey (prefix at read time)."""
    names: dict[str, str] = {}
    locs: dict[str, tuple[float, float]] = {}
    liveness: dict[str, dict[str, Any]] = {}
    if heard_pubkeys is not None and not heard_pubkeys:
        return names, locs, liveness
    if heard_pubkeys is None:
        rows = conn.execute("SELECT * FROM mesh_peers")
    else:
        exact: set[str] = set()
        prefixes: set[str] = set()
        for raw in heard_pubkeys:
            pk = normalize_pubkey_hex(raw)
            if not pk:
                continue
            exact.add(pk)
            if len(pk) >= 8:
                prefixes.add(pk[:8])
            if len(pk) <= 8:
                prefixes.add(pk)
        clauses: list[str] = []
        params: list[Any] = []
        if exact:
            clauses.append(f"pubkey IN ({','.join('?' * len(exact))})")
            params.extend(sorted(exact))
        for pref in sorted(prefixes):
            clauses.append("pubkey LIKE ?")
            params.append(pref + "%")
        if not clauses:
            return names, locs, liveness
        rows = conn.execute(
            f"SELECT * FROM mesh_peers WHERE {' OR '.join(clauses)}",
            params,
        )
    for row in rows:
        peer = dict(row)
        pk = normalize_pubkey_hex(peer.get("pubkey"))
        if not pk:
            continue
        name = str(peer.get("adv_name") or "").strip()
        if name:
            names[pk] = name
        lat, lon = peer.get("adv_lat"), peer.get("adv_lon")
        if lat is not None and lon is not None:
            try:
                locs[pk] = (float(lat), float(lon))
            except (TypeError, ValueError):
                pass
        seen_at = peer.get("last_mesh_seen_at")
        if seen_at is not None:
            try:
                ts = int(seen_at)
            except (TypeError, ValueError):
                ts = None
            if ts is not None:
                seen_by = str(peer.get("last_mesh_seen_by") or "").strip().lower()
                liveness[pk] = {
                    "seen_at": ts,
                    "seen_by": seen_by or None,
                }
    return names, locs, liveness
