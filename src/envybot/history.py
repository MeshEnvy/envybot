"""Book-local SQLite history. Observed telemetry and apply audit live here."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

HISTORY_REL = Path("data/fleet/history.sqlite")
LEGACY_JSONL = Path("data/fleet/polls.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS last_seen (
  unit TEXT PRIMARY KEY,
  updated_at INTEGER NOT NULL,
  firmware_at INTEGER,
  bootloader_at INTEGER,
  name_at INTEGER,
  gps_at INTEGER,
  advert_at INTEGER,
  flood_advert_at INTEGER,
  status_at INTEGER,
  telemetry_at INTEGER,
  acl_at INTEGER,
  neighbors_at INTEGER,
  firmware_version TEXT,
  bootloader_version TEXT,
  firmware_platform TEXT,
  name_heard TEXT,
  lat_heard REAL,
  lon_heard REAL,
  advert_interval_min INTEGER,
  flood_advert_interval_h INTEGER,
  node_clock INTEGER,
  battery_mv INTEGER,
  voltage REAL,
  packets_recv INTEGER,
  packets_sent INTEGER,
  err_events INTEGER,
  recv_errors INTEGER
);

CREATE TABLE IF NOT EXISTS status (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS status_unit_ts ON status (unit, ts);

CREATE TABLE IF NOT EXISTS telemetry (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  channel INTEGER,
  type TEXT,
  value REAL
);
CREATE INDEX IF NOT EXISTS telemetry_unit_ts ON telemetry (unit, ts);

CREATE TABLE IF NOT EXISTS acl_snapshots (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS acl_unit_ts ON acl_snapshots (unit, ts);

CREATE TABLE IF NOT EXISTS neighbors (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS neighbors_unit_ts ON neighbors (unit, ts);

CREATE TABLE IF NOT EXISTS applies (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  field TEXT NOT NULL,
  desired TEXT,
  ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS applies_unit_ts ON applies (unit, ts);

CREATE TABLE IF NOT EXISTS commands (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  argv TEXT,
  reply TEXT,
  ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_unit_ts ON commands (unit, ts);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _voltage_from_items(items: Any) -> float | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "voltage":
            continue
        val = item.get("value")
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def history_path(book: Path) -> Path:
    return book / HISTORY_REL


def legacy_jsonl_path(book: Path) -> Path:
    return book / LEGACY_JSONL


def open_history(book: Path) -> sqlite3.Connection:
    path = history_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _ensure_last_seen_columns(conn)
    return conn


def _ensure_last_seen_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(last_seen)")}
    if "voltage" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN voltage REAL")


def is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM last_seen").fetchone()
    return int(row["n"]) == 0


def get_last_seen(conn: sqlite3.Connection, unit: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM last_seen WHERE unit = ?", (unit,)).fetchone()
    return dict(row) if row else None


def latest_neighbors(conn: sqlite3.Connection, unit: str) -> list[Any] | None:
    row = conn.execute(
        "SELECT payload FROM neighbors WHERE unit = ? ORDER BY ts DESC LIMIT 1",
        (unit,),
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def all_last_seen(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM last_seen"):
        out[str(row["unit"])] = dict(row)
    return out


def last_ok_apply(conn: sqlite3.Connection, unit: str, field: str) -> str | None:
    row = conn.execute(
        "SELECT desired FROM applies WHERE unit = ? AND field = ? AND ok = 1 "
        "ORDER BY ts DESC LIMIT 1",
        (unit, field),
    ).fetchone()
    return None if row is None else row["desired"]


def insert_apply(
    conn: sqlite3.Connection,
    *,
    unit: str,
    field: str,
    desired: str | None,
    ok: bool,
    ts: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO applies (ts, unit, field, desired, ok) VALUES (?, ?, ?, ?, ?)",
        (ts or int(time.time()), unit, field, desired, 1 if ok else 0),
    )
    conn.commit()


def insert_command(
    conn: sqlite3.Connection,
    *,
    unit: str,
    argv: str,
    reply: str | None,
    ok: bool,
    ts: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO commands (ts, unit, argv, reply, ok) VALUES (?, ?, ?, ?, ?)",
        (ts or int(time.time()), unit, argv, reply, 1 if ok else 0),
    )
    conn.commit()


def history_series(
    conn: sqlite3.Connection,
    unit: str,
    metric: str,
    *,
    since: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Time series for the fleet UI. metric: battery_mv|packets|errors|voltage."""
    since = since or 0
    if metric == "voltage":
        rows = conn.execute(
            "SELECT ts, value FROM telemetry WHERE unit = ? AND type = 'voltage' "
            "AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (unit, since, limit),
        ).fetchall()
        return [{"ts": r["ts"], "value": r["value"]} for r in reversed(rows)]
    if metric == "battery_mv":
        key = "battery_mv"
    elif metric == "packets":
        key = "packets_recv"
    elif metric == "errors":
        key = "err_events"
    else:
        return []
    rows = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts >= ? "
        "ORDER BY ts DESC LIMIT ?",
        (unit, since, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        val = payload.get(key)
        if metric == "packets":
            sent = payload.get("packets_sent")
            out.append({"ts": row["ts"], "recv": val, "sent": sent})
        elif val is not None:
            out.append({"ts": row["ts"], "value": val})
    return out


def _upsert_last_seen(conn: sqlite3.Connection, unit: str, fields: dict[str, Any]) -> None:
    existing = get_last_seen(conn, unit)
    now = int(fields.get("updated_at") or time.time())
    row = dict(existing or {"unit": unit, "updated_at": now})
    row.update({k: v for k, v in fields.items() if v is not None or k in ("unit",)})
    row["unit"] = unit
    row["updated_at"] = now
    cols = [
        "unit",
        "updated_at",
        "firmware_at",
        "bootloader_at",
        "name_at",
        "gps_at",
        "advert_at",
        "flood_advert_at",
        "status_at",
        "telemetry_at",
        "acl_at",
        "neighbors_at",
        "firmware_version",
        "bootloader_version",
        "firmware_platform",
        "name_heard",
        "lat_heard",
        "lon_heard",
        "advert_interval_min",
        "flood_advert_interval_h",
        "node_clock",
        "battery_mv",
        "voltage",
        "packets_recv",
        "packets_sent",
        "err_events",
        "recv_errors",
    ]
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    update_sql = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "unit")
    conn.execute(
        f"INSERT INTO last_seen ({col_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(unit) DO UPDATE SET {update_sql}",
        [row.get(c) for c in cols],
    )


def record_poll(
    conn: sqlite3.Connection,
    *,
    unit: str,
    res: Any,
    ts: int | None = None,
) -> None:
    """Write a PollResult into last_seen + history tables."""
    now = ts or int(time.time())
    groups = getattr(res, "polled_groups", frozenset()) or frozenset()
    fields: dict[str, Any] = {"updated_at": now}
    if getattr(res, "node_clock", None) is not None:
        fields["node_clock"] = res.node_clock
    if "firmware" in groups and res.firmware_version:
        fields["firmware_version"] = res.firmware_version
        fields["firmware_at"] = now
        if res.firmware_platform:
            fields["firmware_platform"] = res.firmware_platform
    if "bootloader" in groups and res.bootloader_version is not None:
        fields["bootloader_version"] = res.bootloader_version
        fields["bootloader_at"] = now
    if "name" in groups and res.name is not None:
        fields["name_heard"] = res.name
        fields["name_at"] = now
    if ("lat" in groups or "lon" in groups or "gps" in groups) and (
        res.lat is not None or res.lon is not None
    ):
        if res.lat is not None:
            fields["lat_heard"] = res.lat
        if res.lon is not None:
            fields["lon_heard"] = res.lon
        fields["gps_at"] = now
    if "advert" in groups and res.advert_interval_min is not None:
        fields["advert_interval_min"] = res.advert_interval_min
        fields["advert_at"] = now
    if "flood_advert" in groups and res.flood_advert_interval_h is not None:
        fields["flood_advert_interval_h"] = res.flood_advert_interval_h
        fields["flood_advert_at"] = now
    if "status" in groups and res.status:
        fields["status_at"] = now
        fields["battery_mv"] = res.status.get("battery_mv")
        fields["packets_recv"] = res.status.get("packets_recv")
        fields["packets_sent"] = res.status.get("packets_sent")
        fields["err_events"] = res.status.get("err_events")
        fields["recv_errors"] = res.status.get("recv_errors")
        conn.execute(
            "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
            (now, unit, json.dumps(res.status, default=str)),
        )
    if "telemetry" in groups and res.telemetry is not None:
        fields["telemetry_at"] = now
        volt = _voltage_from_items(res.telemetry)
        if volt is not None:
            fields["voltage"] = volt
        for item in res.telemetry:
            if not isinstance(item, dict):
                continue
            val = item.get("value")
            try:
                num = float(val) if val is not None else None
            except (TypeError, ValueError):
                num = None
            conn.execute(
                "INSERT INTO telemetry (ts, unit, channel, type, value) VALUES (?, ?, ?, ?, ?)",
                (now, unit, item.get("channel"), item.get("type"), num),
            )
    if "acl" in groups and res.acl is not None:
        fields["acl_at"] = now
        conn.execute(
            "INSERT INTO acl_snapshots (ts, unit, payload) VALUES (?, ?, ?)",
            (now, unit, json.dumps(res.acl, default=str)),
        )
    if "neighbors" in groups and res.neighbors is not None:
        fields["neighbors_at"] = now
        conn.execute(
            "INSERT INTO neighbors (ts, unit, payload) VALUES (?, ?, ?)",
            (now, unit, json.dumps(res.neighbors, default=str)),
        )
    _upsert_last_seen(conn, unit, fields)
    conn.commit()


def record_onboard_heard(
    conn: sqlite3.Connection,
    *,
    unit: str,
    firmware_version: str | None = None,
    bootloader_version: str | None = None,
    firmware_platform: str | None = None,
    node_clock: int | None = None,
    neighbors: Any = None,
    ts: int | None = None,
) -> None:
    now = ts or int(time.time())
    fields: dict[str, Any] = {
        "updated_at": now,
        "name_heard": "Repeater",
        "name_at": now,
        "lat_heard": 0.0,
        "lon_heard": 0.0,
        "gps_at": now,
    }
    if firmware_version:
        fields["firmware_version"] = firmware_version
        fields["firmware_at"] = now
    if firmware_platform:
        fields["firmware_platform"] = firmware_platform
    if bootloader_version is not None:
        fields["bootloader_version"] = bootloader_version
        fields["bootloader_at"] = now
    if node_clock is not None:
        fields["node_clock"] = node_clock
    if neighbors is not None:
        fields["neighbors_at"] = now
        conn.execute(
            "INSERT INTO neighbors (ts, unit, payload) VALUES (?, ?, ?)",
            (now, unit, json.dumps(neighbors, default=str)),
        )
    _upsert_last_seen(conn, unit, fields)
    conn.commit()


def _stamp_from_yaml(node: dict[str, Any], *keys: str) -> int | None:
    stamps: list[int] = []
    for key in keys:
        val = node.get(key)
        if val is None:
            continue
        try:
            stamps.append(int(val))
        except (TypeError, ValueError):
            continue
    return max(stamps) if stamps else None


def import_yaml_last_seen(conn: sqlite3.Connection, nodes: dict[str, Any]) -> int:
    """Seed last_seen from current YAML observed blobs. Returns rows written."""
    written = 0
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        status = node.get("status") if isinstance(node.get("status"), dict) else None
        now = _stamp_from_yaml(
            node,
            "status_pulled_at",
            "firmware_pulled_at",
            "name_pulled_at",
            "telemetry_pulled_at",
        ) or int(time.time())
        fields: dict[str, Any] = {"updated_at": now}
        if node.get("firmware_version"):
            fields["firmware_version"] = node.get("firmware_version")
            fields["firmware_at"] = node.get("firmware_pulled_at") or now
        if node.get("bootloader_version") is not None:
            fields["bootloader_version"] = node.get("bootloader_version")
            fields["bootloader_at"] = node.get("bootloader_pulled_at") or now
        if node.get("firmware_platform"):
            fields["firmware_platform"] = node.get("firmware_platform")
        if node.get("name"):
            fields["name_heard"] = node.get("name")
            fields["name_at"] = node.get("name_pulled_at") or now
        if node.get("advert_interval_min") is not None:
            fields["advert_interval_min"] = node.get("advert_interval_min")
            fields["advert_at"] = node.get("advert_pulled_at") or now
        if node.get("flood_advert_interval_h") is not None:
            fields["flood_advert_interval_h"] = node.get("flood_advert_interval_h")
            fields["flood_advert_at"] = node.get("flood_advert_pulled_at") or now
        if node.get("node_clock") is not None:
            fields["node_clock"] = node.get("node_clock")
        if status:
            fields["status_at"] = node.get("status_pulled_at") or now
            fields["battery_mv"] = status.get("battery_mv")
            fields["packets_recv"] = status.get("packets_recv")
            fields["packets_sent"] = status.get("packets_sent")
            fields["err_events"] = status.get("err_events")
            fields["recv_errors"] = status.get("recv_errors")
            conn.execute(
                "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
                (now, key, json.dumps(status, default=str)),
            )
        tele = node.get("telemetry")
        if isinstance(tele, list):
            fields["telemetry_at"] = node.get("telemetry_pulled_at") or now
            volt = _voltage_from_items(tele)
            if volt is not None:
                fields["voltage"] = volt
            for item in tele:
                if not isinstance(item, dict):
                    continue
                val = item.get("value")
                try:
                    num = float(val) if val is not None else None
                except (TypeError, ValueError):
                    num = None
                conn.execute(
                    "INSERT INTO telemetry (ts, unit, channel, type, value) VALUES (?, ?, ?, ?, ?)",
                    (now, key, item.get("channel"), item.get("type"), num),
                )
        if isinstance(node.get("acl"), list):
            fields["acl_at"] = node.get("acl_pulled_at") or now
            conn.execute(
                "INSERT INTO acl_snapshots (ts, unit, payload) VALUES (?, ?, ?)",
                (now, key, json.dumps(node["acl"], default=str)),
            )
        if isinstance(node.get("neighbors"), list):
            fields["neighbors_at"] = node.get("neighbors_pulled_at") or now
            conn.execute(
                "INSERT INTO neighbors (ts, unit, payload) VALUES (?, ?, ?)",
                (now, key, json.dumps(node["neighbors"], default=str)),
            )
        if len(fields) > 1:
            _upsert_last_seen(conn, key, fields)
            written += 1
    conn.commit()
    return written


def import_jsonl(conn: sqlite3.Connection, path: Path) -> int:
    """One-shot ingest of polls.jsonl monitor/cmd events. Returns lines imported."""
    if not path.is_file():
        return 0
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event")
            unit = rec.get("unit")
            ts = int(rec.get("ts") or time.time())
            if not unit:
                continue
            if event == "cmd":
                insert_command(
                    conn,
                    unit=str(unit),
                    argv=str(rec.get("command") or ""),
                    reply=rec.get("reply"),
                    ok=bool(rec.get("ok")),
                    ts=ts,
                )
                n += 1
                continue
            if event != "monitor":
                continue

            class _Tmp:
                pass

            tmp = _Tmp()
            tmp.firmware_version = rec.get("firmware_version")
            tmp.bootloader_version = rec.get("bootloader_version")
            tmp.firmware_platform = rec.get("firmware_platform")
            tmp.name = rec.get("adv_name")
            tmp.lat = rec.get("lat")
            tmp.lon = rec.get("lon")
            tmp.node_clock = rec.get("node_clock")
            tmp.status = rec.get("status")
            tmp.telemetry = rec.get("telemetry")
            tmp.advert_interval_min = rec.get("advert_interval_min")
            tmp.flood_advert_interval_h = rec.get("flood_advert_interval_h")
            tmp.acl = rec.get("acl")
            tmp.neighbors = rec.get("neighbors")
            groups = rec.get("polled_groups") or []
            tmp.polled_groups = frozenset(groups)
            record_poll(conn, unit=str(unit), res=tmp, ts=ts)
            n += 1
    return n


def migrate_legacy(book: Path, nodes: dict[str, Any]) -> sqlite3.Connection:
    """Open history. If empty, import jsonl + YAML last-seen, then delete jsonl."""
    conn = open_history(book)
    if not is_empty(conn):
        return conn
    jsonl = legacy_jsonl_path(book)
    if jsonl.is_file():
        import_jsonl(conn, jsonl)
        jsonl.unlink()
    import_yaml_last_seen(conn, nodes)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated_at', ?)",
        (str(int(time.time())),),
    )
    conn.commit()
    return conn
