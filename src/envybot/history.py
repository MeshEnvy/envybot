"""Book-local SQLite history. Observed telemetry and apply audit live here."""

from __future__ import annotations

import json
import math
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
  ota_at INTEGER,
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
  base_hash TEXT,
  firmware_platform TEXT,
  name_heard TEXT,
  lat_heard REAL,
  lon_heard REAL,
  advert_interval_min INTEGER,
  flood_advert_interval_h INTEGER,
  node_clock INTEGER,
  battery_mv INTEGER,
  voltage REAL,
  uptime_secs INTEGER,
  temperature REAL,
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
  value REAL,
  lat REAL,
  lon REAL
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

CREATE TABLE IF NOT EXISTS ota_snapshots (
  ts INTEGER NOT NULL,
  unit TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ota_unit_ts ON ota_snapshots (unit, ts);

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
  ok INTEGER NOT NULL,
  source TEXT
);
CREATE INDEX IF NOT EXISTS commands_unit_ts ON commands (unit, ts);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS mesh_audit (
  id INTEGER PRIMARY KEY,
  ts_sent REAL NOT NULL,
  ts_reply REAL,
  unit TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT,
  attempt INTEGER,
  path TEXT NOT NULL,
  wait_s REAL,
  ok INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  reply TEXT,
  error TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS mesh_audit_unit_ts ON mesh_audit (unit, ts_sent);
"""


def _telemetry_value_from_items(items: Any, kind: str) -> float | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != kind:
            continue
        val = item.get("value")
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _voltage_from_items(items: Any) -> float | None:
    return _telemetry_value_from_items(items, "voltage")


def _temperature_from_items(items: Any) -> float | None:
    return _telemetry_value_from_items(items, "temperature")


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
    _ensure_audit_source_columns(conn)
    _ensure_sample_loc_columns(conn)
    _backfill_sample_loc_from_sites(conn, book)
    return conn


def _ensure_audit_source_columns(conn: sqlite3.Connection) -> None:
    cmd_cols = {row[1] for row in conn.execute("PRAGMA table_info(commands)")}
    if "source" not in cmd_cols:
        conn.execute("ALTER TABLE commands ADD COLUMN source TEXT")
    audit_cols = {row[1] for row in conn.execute("PRAGMA table_info(mesh_audit)")}
    if "source" not in audit_cols:
        conn.execute("ALTER TABLE mesh_audit ADD COLUMN source TEXT")


def _ensure_sample_loc_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
    if "lat" not in cols:
        conn.execute("ALTER TABLE telemetry ADD COLUMN lat REAL")
    if "lon" not in cols:
        conn.execute("ALTER TABLE telemetry ADD COLUMN lon REAL")


SAMPLE_LOC_BACKFILL_META = "sample_loc_backfill"


def _backfill_sample_loc_from_sites(conn: sqlite3.Connection, book: Path) -> int:
    """One-shot: stamp current site GPS onto status/telemetry rows that lack loc.

    Bound units only. Bench/unmapped rows stay NULL. Does not overwrite a
    loc already on the sample. Re-run by deleting meta ``sample_loc_backfill``.
    """
    done = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (SAMPLE_LOC_BACKFILL_META,)
    ).fetchone()
    if done:
        return 0
    nodes_path = book / "nodes.yaml"
    sites_path = book / "sites.yaml"
    if not nodes_path.is_file() or not sites_path.is_file():
        return 0
    from envybot.nodes_doc import load_nodes_doc
    from envybot.position import load_sites, site_loc_for_unit

    nodes = load_nodes_doc(nodes_path).get("nodes") or {}
    sites = load_sites(sites_path)
    stamped = 0
    if isinstance(nodes, dict):
        for unit, node in nodes.items():
            loc = site_loc_for_unit(
                str(unit), node if isinstance(node, dict) else None, sites
            )
            if not loc:
                continue
            stamped += _stamp_missing_sample_loc(conn, str(unit).lower(), loc)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (SAMPLE_LOC_BACKFILL_META, str(int(time.time()))),
    )
    conn.commit()
    return stamped


def _stamp_missing_sample_loc(
    conn: sqlite3.Connection,
    unit: str,
    loc: tuple[float, float],
) -> int:
    lat, lon = loc
    n = 0
    n += conn.execute(
        "UPDATE telemetry SET lat = ?, lon = ? "
        "WHERE unit = ? AND (lat IS NULL OR lon IS NULL)",
        (lat, lon, unit),
    ).rowcount
    rows = conn.execute(
        "SELECT rowid, payload FROM status WHERE unit = ?", (unit,)
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("site_lat") is not None and payload.get("site_lon") is not None:
            continue
        payload["site_lat"] = lat
        payload["site_lon"] = lon
        conn.execute(
            "UPDATE status SET payload = ? WHERE rowid = ?",
            (json.dumps(payload, default=str), row["rowid"]),
        )
        n += 1
    return n


def _ensure_last_seen_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(last_seen)")}
    if "voltage" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN voltage REAL")
    if "uptime_secs" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN uptime_secs INTEGER")
    if "temperature" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN temperature REAL")
    if "ota_at" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN ota_at INTEGER")
    if "base_hash" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN base_hash TEXT")
    if "ota_status_at" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN ota_status_at INTEGER")
    if "ota_ls_at" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN ota_ls_at INTEGER")
    if "ota_state" not in cols:
        conn.execute("ALTER TABLE last_seen ADD COLUMN ota_state TEXT")
    _backfill_last_seen_promoted_fields(conn)


def _backfill_last_seen_promoted_fields(conn: sqlite3.Connection) -> None:
    """Promote uptime/temp from history rows when last_seen cache is missing them."""
    rows = conn.execute(
        "SELECT unit, uptime_secs, temperature FROM last_seen "
        "WHERE uptime_secs IS NULL OR temperature IS NULL"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        unit = str(row["unit"])
        fields: dict[str, Any] = {}
        if row["uptime_secs"] is None:
            status_row = conn.execute(
                "SELECT payload FROM status WHERE unit = ? ORDER BY ts DESC LIMIT 1",
                (unit,),
            ).fetchone()
            if status_row:
                try:
                    payload = json.loads(status_row["payload"])
                except json.JSONDecodeError:
                    payload = {}
                uptime = payload.get("uptime_secs") if isinstance(payload, dict) else None
                if uptime is not None:
                    try:
                        fields["uptime_secs"] = int(uptime)
                    except (TypeError, ValueError):
                        pass
        if row["temperature"] is None:
            tele_row = conn.execute(
                "SELECT value FROM telemetry WHERE unit = ? AND type = 'temperature' "
                "ORDER BY ts DESC LIMIT 1",
                (unit,),
            ).fetchone()
            if tele_row and tele_row["value"] is not None:
                fields["temperature"] = tele_row["value"]
        if fields:
            _upsert_last_seen(conn, unit, fields)
    conn.commit()


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


def latest_ota(conn: sqlite3.Connection, unit: str) -> dict[str, Any] | None:
    """Latest merged OTA snapshot for a unit (status + heard catalog)."""
    row = conn.execute("SELECT ota_state FROM last_seen WHERE unit = ?", (unit,)).fetchone()
    if row and row["ota_state"]:
        try:
            data = json.loads(row["ota_state"])
        except json.JSONDecodeError:
            data = None
        else:
            if isinstance(data, dict):
                return data
    snap = conn.execute(
        "SELECT payload FROM ota_snapshots WHERE unit = ? ORDER BY ts DESC LIMIT 1",
        (unit,),
    ).fetchone()
    if not snap:
        return None
    try:
        data = json.loads(snap["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def record_ota_snapshot(
    conn: sqlite3.Connection,
    *,
    unit: str,
    snapshot: dict[str, Any],
    ts: int | None = None,
    status_at: bool = False,
    ls_at: bool = False,
) -> None:
    now = ts or int(time.time())
    payload = json.dumps(snapshot, default=str)
    conn.execute(
        "INSERT INTO ota_snapshots (ts, unit, payload) VALUES (?, ?, ?)",
        (now, unit, payload),
    )
    fields: dict[str, Any] = {"updated_at": now, "ota_state": payload}
    if status_at:
        fields["ota_status_at"] = now
    if ls_at:
        fields["ota_ls_at"] = now
    _upsert_last_seen(conn, unit, fields)


def latest_status(conn: sqlite3.Connection, unit: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM status WHERE unit = ? ORDER BY ts DESC LIMIT 1",
        (unit,),
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _status_int(payload: dict[str, Any], key: str) -> int | None:
    val = payload.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


INTERVAL_DELTA_KEYS = (
    "packets_recv",
    "packets_sent",
    "recv_errors",
    "recv_flood",
    "recv_direct",
    "sent_flood",
    "sent_direct",
    "flood_dups",
    "direct_dups",
    "tx_airtime_secs",
    "rx_airtime_secs",
)

TRAFFIC_WINDOW_SECS = 6 * 3600


def _load_status_payload(row: sqlite3.Row) -> tuple[int, dict[str, Any]] | None:
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return int(row["ts"]), payload


def _status_pair_delta(
    prev: dict[str, Any],
    curr: dict[str, Any],
    *,
    prev_ts: int,
    curr_ts: int,
) -> dict[str, Any]:
    curr_uptime = _status_int(curr, "uptime_secs")
    prev_uptime = _status_int(prev, "uptime_secs")
    reboot = (
        curr_uptime is not None
        and prev_uptime is not None
        and curr_uptime < prev_uptime
    )
    duration_secs = max(0, curr_ts - prev_ts)
    base: dict[str, Any] = {
        "from_ts": prev_ts,
        "to_ts": curr_ts,
        "duration_secs": duration_secs,
        "reboot_reset": reboot,
    }
    if reboot:
        return base

    def delta(key: str) -> int | None:
        c = _status_int(curr, key)
        p = _status_int(prev, key)
        if c is None or p is None:
            return None
        d = c - p
        return d if d >= 0 else None

    any_delta = False
    for key in INTERVAL_DELTA_KEYS:
        val = delta(key)
        if val is not None:
            base[key] = val
            any_delta = True
    if not any_delta:
        base["_empty"] = True
    rx_air = base.get("rx_airtime_secs")
    if rx_air is not None and duration_secs > 0:
        base["rx_airtime_pct"] = round((rx_air / duration_secs) * 100, 1)
    return base


def _parse_status_row(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {"ts": int(row["ts"])}
    for key in (
        *INTERVAL_DELTA_KEYS,
        "battery_mv",
        "uptime_secs",
        "noise_floor",
        "last_snr",
        "last_rssi",
        "tx_queue_len",
    ):
        val = payload.get(key)
        if val is not None:
            out[key] = val
    return out


def status_series(
    conn: sqlite3.Connection,
    unit: str,
    *,
    days: int = 7,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Chronological status samples for health checks and derived series."""
    since = int(time.time()) - max(1, days) * 86400
    rows = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts >= ? "
        "ORDER BY ts ASC LIMIT ?",
        (unit, since, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_status_row(row)
        if parsed is not None:
            out.append(parsed)
    return out


def count_reboots(status_rows: list[dict[str, Any]]) -> int:
    """Reboot count from uptime drops across chronological status rows."""
    reboots = 0
    prev_uptime: int | None = None
    for row in status_rows:
        uptime = _status_int(row, "uptime_secs") if isinstance(row, dict) else None
        if uptime is None:
            continue
        if prev_uptime is not None and uptime < prev_uptime:
            reboots += 1
        prev_uptime = uptime
    return reboots


def interval_traffic(conn: sqlite3.Connection, unit: str) -> dict[str, Any] | None:
    """Delta traffic between the last two status polls (since last fleet GET)."""
    rows = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? ORDER BY ts DESC LIMIT 2",
        (unit,),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev_row = _load_status_payload(rows[1])
    curr_row = _load_status_payload(rows[0])
    if prev_row is None or curr_row is None:
        return None
    prev_ts, prev = prev_row
    curr_ts, curr = curr_row
    base = _status_pair_delta(prev, curr, prev_ts=prev_ts, curr_ts=curr_ts)
    if base.get("reboot_reset"):
        return base
    if base.pop("_empty", False):
        return None
    return base


def rolling_traffic(
    conn: sqlite3.Connection,
    unit: str,
    *,
    window_secs: int = TRAFFIC_WINDOW_SECS,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Sum traffic deltas across status polls in a rolling wall-clock window."""
    now_ts = now or int(time.time())
    since_ts = now_ts - window_secs
    anchor = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (unit, since_ts),
    ).fetchone()
    recent = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts > ? ORDER BY ts ASC",
        (unit, since_ts),
    ).fetchall()
    chron: list[sqlite3.Row] = []
    if anchor is not None:
        chron.append(anchor)
    chron.extend(recent)
    if len(chron) < 2:
        return None

    totals = {key: 0.0 for key in INTERVAL_DELTA_KEYS}
    covered_secs = 0
    any_delta = False
    reboot_in_window = False

    for idx in range(1, len(chron)):
        prev_row = _load_status_payload(chron[idx - 1])
        curr_row = _load_status_payload(chron[idx])
        if prev_row is None or curr_row is None:
            continue
        prev_ts, prev = prev_row
        curr_ts, curr = curr_row
        pair = _status_pair_delta(prev, curr, prev_ts=prev_ts, curr_ts=curr_ts)
        if pair.get("reboot_reset"):
            reboot_in_window = True
            continue
        duration = curr_ts - prev_ts
        if duration <= 0:
            continue
        segment_start = max(prev_ts, since_ts)
        segment_end = min(curr_ts, now_ts)
        if segment_end <= segment_start:
            continue
        overlap = segment_end - segment_start
        covered_secs += overlap
        if pair.pop("_empty", False):
            continue
        any_delta = True
        fraction = overlap / duration
        for key in INTERVAL_DELTA_KEYS:
            val = pair.get(key)
            if val is not None:
                totals[key] += val * fraction

    base: dict[str, Any] = {
        "window_secs": window_secs,
        "from_ts": since_ts,
        "to_ts": now_ts,
        "duration_secs": covered_secs,
        "reboot_reset": reboot_in_window,
    }
    if reboot_in_window and not any_delta:
        return base
    if not any_delta:
        return None
    for key, val in totals.items():
        rounded = int(round(val))
        if rounded:
            base[key] = rounded
    rx_air = totals.get("rx_airtime_secs", 0.0)
    if rx_air > 0 and covered_secs > 0:
        base["rx_airtime_pct"] = round((rx_air / covered_secs) * 100, 1)
    return base


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


def last_ok_apply_times(
    conn: sqlite3.Connection, unit: str | None = None
) -> dict[str, dict[str, int]]:
    """Latest ok apply ts per unit, per field."""
    sql = "SELECT unit, field, MAX(ts) AS ts FROM applies WHERE ok = 1"
    args: tuple[Any, ...] = ()
    if unit is not None:
        sql += " AND unit = ?"
        args = (unit,)
    sql += " GROUP BY unit, field"
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(sql, args):
        out.setdefault(str(row["unit"]), {})[str(row["field"])] = int(row["ts"])
    return out


_IDENTITY_HEARD: dict[str, tuple[str, str, type]] = {
    "name": ("name_heard", "name_at", str),
    "lat": ("lat_heard", "gps_at", float),
    "lon": ("lon_heard", "gps_at", float),
    "advert": ("advert_interval_min", "advert_at", int),
    "flood": ("flood_advert_interval_h", "flood_advert_at", int),
}


def stamp_apply(
    conn: sqlite3.Connection,
    *,
    unit: str,
    field: str,
    desired: str | None,
    ok: bool = True,
    ts: int | None = None,
) -> None:
    """Record an apply stamp and, for identity fields, refresh last-seen."""
    insert_apply(conn, unit=unit, field=field, desired=desired, ok=ok, ts=ts)
    if not ok or desired is None:
        return
    spec = _IDENTITY_HEARD.get(field)
    if spec is None:
        return
    col, at, caster = spec
    try:
        val = caster(desired)
    except (TypeError, ValueError):
        return
    now = ts or int(time.time())
    _upsert_last_seen(conn, unit, {col: val, at: now, "updated_at": now})
    conn.commit()


def clear_apply_stamps(
    conn: sqlite3.Connection,
    unit: str,
    *,
    fields: list[str] | None = None,
) -> None:
    """Drop apply stamps so the next run re-SETs (--force)."""
    if fields:
        placeholders = ", ".join("?" for _ in fields)
        conn.execute(
            f"DELETE FROM applies WHERE unit = ? AND field IN ({placeholders})",
            (unit, *fields),
        )
    else:
        conn.execute("DELETE FROM applies WHERE unit = ?", (unit,))
    conn.commit()


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
    source: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO commands (ts, unit, argv, reply, ok, source) VALUES (?, ?, ?, ?, ?, ?)",
        (ts or int(time.time()), unit, argv, reply, 1 if ok else 0, source),
    )
    conn.commit()


def _unreadable_pct(recv_errors: int | None, packets_recv: int | None) -> float | None:
    if recv_errors is None:
        return None
    recv = packets_recv or 0
    total = recv_errors + recv
    if total <= 0:
        return 0.0
    return round((recv_errors / total) * 100, 2)


def _derived_status_series(
    rows: list[dict[str, Any]],
    metric: str,
) -> list[dict[str, Any]]:
    """Build per-interval derived points from chronological status rows."""
    out: list[dict[str, Any]] = []
    if len(rows) < 2:
        return out
    prev = rows[0]
    for row in rows[1:]:
        ts = row.get("ts")
        prev_ts = prev.get("ts")
        if ts is None or prev_ts is None:
            prev = row
            continue
        duration = max(0, int(ts) - int(prev_ts))
        if duration <= 0:
            prev = row
            continue
        prev_uptime = prev.get("uptime_secs")
        curr_uptime = row.get("uptime_secs")
        if (
            prev_uptime is not None
            and curr_uptime is not None
            and int(curr_uptime) < int(prev_uptime)
        ):
            prev = row
            continue
        if metric == "unreadable_pct":
            prev_err = prev.get("recv_errors")
            curr_err = row.get("recv_errors")
            prev_recv = prev.get("packets_recv")
            curr_recv = row.get("packets_recv")
            if prev_err is None or curr_err is None or prev_recv is None or curr_recv is None:
                prev = row
                continue
            d_err = int(curr_err) - int(prev_err)
            d_recv = int(curr_recv) - int(prev_recv)
            if d_err < 0 or d_recv < 0:
                prev = row
                continue
            if d_recv == 0 and d_err == 0:
                if out:
                    out.append({"ts": ts, "value": out[-1]["value"]})
                prev = row
                continue
            pct = _unreadable_pct(d_err, d_recv)
            if pct is not None:
                out.append({"ts": ts, "value": pct})
        elif metric == "recv_rate":
            prev_recv = prev.get("packets_recv")
            curr_recv = row.get("packets_recv")
            if prev_recv is None or curr_recv is None:
                prev = row
                continue
            d_recv = int(curr_recv) - int(prev_recv)
            if d_recv < 0:
                prev = row
                continue
            if d_recv == 0:
                if out:
                    out.append({"ts": ts, "value": out[-1]["value"]})
                prev = row
                continue
            hours = duration / 3600.0
            if hours <= 0:
                prev = row
                continue
            out.append({"ts": ts, "value": round(d_recv / hours, 2)})
        elif metric == "airtime_pct":
            prev_rx = prev.get("rx_airtime_secs")
            curr_rx = row.get("rx_airtime_secs")
            if prev_rx is None or curr_rx is None:
                prev = row
                continue
            d_rx = int(curr_rx) - int(prev_rx)
            if d_rx < 0:
                prev = row
                continue
            out.append({"ts": ts, "value": round((d_rx / duration) * 100, 2)})
        prev = row
    return out


SPARK_HOURS = 72


def compact_sparks(
    status_rows: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
    unit: str | None = None,
    hours: int = SPARK_HOURS,
    now: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compact spark series for dashboard cards."""
    now_ts = now or int(time.time())
    since = now_ts - max(1, hours) * 3600
    filtered = [r for r in status_rows if int(r.get("ts") or 0) >= since]
    battery: list[dict[str, Any]] = []
    for row in filtered:
        ts = row.get("ts")
        mv = row.get("battery_mv")
        if ts is None or mv is None:
            continue
        try:
            battery.append({"ts": int(ts), "value": round(float(mv) / 1000.0, 3)})
        except (TypeError, ValueError):
            continue
    temperature: list[dict[str, Any]] = []
    if conn is not None and unit:
        tele_rows = conn.execute(
            "SELECT ts, value FROM telemetry WHERE unit = ? AND type = 'temperature' "
            "AND ts >= ? ORDER BY ts ASC",
            (unit, since),
        ).fetchall()
        for row in tele_rows:
            num = _finite_number(row["value"])
            if num is not None:
                temperature.append({"ts": int(row["ts"]), "value": num})
    recv_rate = _derived_status_series(filtered, "recv_rate")
    unreadable_pct = _derived_status_series(filtered, "unreadable_pct")
    return {
        "battery_mv": battery,
        "temperature": temperature,
        "recv_rate": recv_rate,
        "unreadable_pct": unreadable_pct,
    }


def _finite_number(val: Any) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _sample_series(
    rows: list[dict[str, Any]],
    key: str,
    extra: list[tuple[int, float]] | None = None,
) -> list[tuple[int, float]]:
    samples: list[tuple[int, float]] = []
    for ts, val in extra or []:
        num = _finite_number(val)
        if num is None:
            continue
        samples.append((int(ts), num))
    for row in rows:
        num = _finite_number(row.get(key))
        ts = row.get("ts")
        if num is None or ts is None:
            continue
        samples.append((int(ts), num))
    samples.sort(key=lambda item: item[0])
    return samples


TELEMETRY_MERGE_WINDOW = 120


def _attach_nearest(
    rows: list[dict[str, Any]],
    key: str,
    extra: list[tuple[int, float]],
    *,
    window: int = TELEMETRY_MERGE_WINDOW,
) -> None:
    """Bind off-timestamp telemetry to the nearest status poll within ``window``."""
    if not extra:
        return
    samples: list[tuple[int, float]] = []
    for ts, val in extra:
        num = _finite_number(val)
        if num is None:
            continue
        samples.append((int(ts), num))
    samples.sort(key=lambda item: item[0])
    if not samples:
        return
    for row in rows:
        if _finite_number(row.get(key)) is not None:
            continue
        ts = row.get("ts")
        if ts is None:
            continue
        ts = int(ts)
        best: float | None = None
        best_dt = window + 1
        for kts, kval in samples:
            dt = abs(kts - ts)
            if dt < best_dt:
                best_dt = dt
                best = kval
            if kts > ts + window:
                break
        if best is not None and best_dt <= window:
            row[key] = best


def _interpolate_missing(
    rows: list[dict[str, Any]],
    key: str,
    *,
    digits: int,
    extra: list[tuple[int, float]] | None = None,
) -> None:
    """Straight-line fill for gaps between known samples. No extrapolation."""
    known = _sample_series(rows, key, extra)
    if len(known) < 2:
        return
    for row in rows:
        if _finite_number(row.get(key)) is not None:
            continue
        ts = row.get("ts")
        if ts is None:
            continue
        ts = int(ts)
        prev: tuple[int, float] | None = None
        nxt: tuple[int, float] | None = None
        for kts, kval in known:
            if kts < ts:
                prev = (kts, kval)
            elif kts > ts:
                nxt = (kts, kval)
                break
        if prev is None or nxt is None:
            continue
        t0, v0 = prev
        t1, v1 = nxt
        filled = v0 if t1 == t0 else v0 + (v1 - v0) * ((ts - t0) / (t1 - t0))
        row[key] = round(filled, digits)
        syn = row.setdefault("synthetic", [])
        if key not in syn:
            syn.append(key)


GAUGE_DELTA_FIELDS = (
    ("voltage", 3),
    ("temperature", 1),
    ("noise_floor", 0),
    ("battery_mv", 0),
)
COUNTER_DELTA_FIELDS = ("packets_recv", "packets_sent", "recv_errors")


def _apply_poll_deltas(rows: list[dict[str, Any]]) -> None:
    """Add per-field deltas vs the previous chronological row."""
    prev: dict[str, Any] | None = None
    for row in rows:
        reboot = False
        if prev is not None:
            prev_up = prev.get("uptime_secs")
            curr_up = row.get("uptime_secs")
            reboot = (
                prev_up is not None
                and curr_up is not None
                and int(curr_up) < int(prev_up)
            )
            row["since_prev_secs"] = max(0, int(row["ts"]) - int(prev["ts"]))
            if not reboot:
                for key in COUNTER_DELTA_FIELDS:
                    curr_val = row.get(key)
                    prev_val = prev.get(key)
                    if curr_val is None or prev_val is None:
                        continue
                    delta = int(curr_val) - int(prev_val)
                    if delta >= 0:
                        row[f"delta_{key}"] = delta
            for key, digits in GAUGE_DELTA_FIELDS:
                curr_val = _finite_number(row.get(key))
                prev_val = _finite_number(prev.get(key))
                if curr_val is None or prev_val is None:
                    continue
                row[f"delta_{key}"] = round(curr_val - prev_val, digits)
        row["reboot"] = reboot
        prev = row


def _reverse_limit(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows.reverse()
    return rows[:limit]


def status_history(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> list[dict[str, Any]]:
    since = int(time.time()) - max(1, hours) * 3600
    status_rows = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts >= ? ORDER BY ts ASC",
        (unit, since),
    ).fetchall()
    chronological: list[dict[str, Any]] = []
    for row in status_rows:
        loaded = _load_status_payload(row)
        if loaded is None:
            continue
        ts, payload = loaded
        battery_mv = _status_int(payload, "battery_mv")
        chronological.append(
            {
                "ts": ts,
                "battery_mv": battery_mv,
                "voltage": round(battery_mv / 1000.0, 3) if battery_mv is not None else None,
                "packets_recv": _status_int(payload, "packets_recv"),
                "packets_sent": _status_int(payload, "packets_sent"),
                "recv_errors": _status_int(payload, "recv_errors"),
                "noise_floor": _status_int(payload, "noise_floor"),
                "uptime_secs": _status_int(payload, "uptime_secs"),
                "lat": _finite_number(payload.get("site_lat")),
                "lon": _finite_number(payload.get("site_lon")),
            }
        )
    _apply_poll_deltas(chronological)
    return _reverse_limit(chronological, limit)


def telemetry_history(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> list[dict[str, Any]]:
    since = int(time.time()) - max(1, hours) * 3600
    tele_rows = conn.execute(
        "SELECT ts, type, value, lat, lon FROM telemetry WHERE unit = ? AND ts >= ? "
        "AND type IN ('voltage', 'temperature') ORDER BY ts ASC",
        (unit, since),
    ).fetchall()
    by_ts: dict[int, dict[str, Any]] = {}
    for row in tele_rows:
        ts = int(row["ts"])
        kind = str(row["type"])
        num = _finite_number(row["value"])
        if num is None:
            continue
        entry = by_ts.setdefault(
            ts, {"ts": ts, "voltage": None, "temperature": None, "lat": None, "lon": None}
        )
        if kind == "voltage":
            entry["voltage"] = round(num, 3)
        elif kind == "temperature":
            entry["temperature"] = round(num, 1)
        if entry.get("lat") is None:
            entry["lat"] = _finite_number(row["lat"])
        if entry.get("lon") is None:
            entry["lon"] = _finite_number(row["lon"])
    chronological = sorted(by_ts.values(), key=lambda r: r["ts"])
    prev: dict[str, Any] | None = None
    for row in chronological:
        if prev is not None:
            row["since_prev_secs"] = max(0, int(row["ts"]) - int(prev["ts"]))
            for key, digits in (("voltage", 3), ("temperature", 1)):
                curr = _finite_number(row.get(key))
                prev_val = _finite_number(prev.get(key))
                if curr is not None and prev_val is not None:
                    row[f"delta_{key}"] = round(curr - prev_val, digits)
        prev = row
    return _reverse_limit(chronological, limit)


def neighbors_history(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> list[dict[str, Any]]:
    since = int(time.time()) - max(1, hours) * 3600
    rows = conn.execute(
        "SELECT ts, payload FROM neighbors WHERE unit = ? AND ts >= ? ORDER BY ts ASC",
        (unit, since),
    ).fetchall()
    chronological: list[dict[str, Any]] = []
    prev_count: int | None = None
    prev_ts: int | None = None
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        count = len(payload) if isinstance(payload, list) else 0
        ts = int(row["ts"])
        entry: dict[str, Any] = {"ts": ts, "count": count}
        if prev_ts is not None:
            entry["since_prev_secs"] = max(0, ts - prev_ts)
        if prev_count is not None:
            entry["delta_count"] = count - prev_count
        chronological.append(entry)
        prev_count = count
        prev_ts = ts
    return _reverse_limit(chronological, limit)


def acl_history(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> list[dict[str, Any]]:
    since = int(time.time()) - max(1, hours) * 3600
    rows = conn.execute(
        "SELECT ts, payload FROM acl_snapshots WHERE unit = ? AND ts >= ? ORDER BY ts ASC",
        (unit, since),
    ).fetchall()
    chronological: list[dict[str, Any]] = []
    prev_count: int | None = None
    prev_ts: int | None = None
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        count = len(payload) if isinstance(payload, list) else 0
        ts = int(row["ts"])
        entry: dict[str, Any] = {"ts": ts, "count": count}
        if prev_ts is not None:
            entry["since_prev_secs"] = max(0, ts - prev_ts)
        if prev_count is not None:
            entry["delta_count"] = count - prev_count
        chronological.append(entry)
        prev_count = count
        prev_ts = ts
    return _reverse_limit(chronological, limit)


def source_histories(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> dict[str, list[dict[str, Any]]]:
    """Orthogonal poll histories per GET source, newest-first within each list."""
    return {
        "status": status_history(conn, unit, limit=limit, hours=hours),
        "telemetry": telemetry_history(conn, unit, limit=limit, hours=hours),
        "neighbors": neighbors_history(conn, unit, limit=limit, hours=hours),
        "acl": acl_history(conn, unit, limit=limit, hours=hours),
    }


def poll_snapshots(
    conn: sqlite3.Connection,
    unit: str,
    *,
    limit: int = 48,
    hours: int = 72,
) -> list[dict[str, Any]]:
    """Merged status+telemetry polls, newest-first.

    Each row has absolute values and ``delta_*`` vs the prior poll. Missing
    gauges (temperature, voltage, noise) are straight-line interpolated from
    neighboring samples, including off-timestamp telemetry. No extrapolation.
    """
    since = int(time.time()) - max(1, hours) * 3600
    status_rows = conn.execute(
        "SELECT ts, payload FROM status WHERE unit = ? AND ts >= ? ORDER BY ts ASC",
        (unit, since),
    ).fetchall()
    if not status_rows:
        return []

    tele_since = since - 7 * 86400
    tele_rows = conn.execute(
        "SELECT ts, type, value FROM telemetry WHERE unit = ? AND ts >= ? "
        "AND type IN ('voltage', 'temperature')",
        (unit, tele_since),
    ).fetchall()
    tele_by_ts: dict[int, dict[str, float]] = {}
    tele_extra: dict[str, list[tuple[int, float]]] = {"voltage": [], "temperature": []}
    for row in tele_rows:
        num = _finite_number(row["value"])
        if num is None:
            continue
        ts = int(row["ts"])
        kind = str(row["type"])
        tele_by_ts.setdefault(ts, {})[kind] = num
        if kind in tele_extra:
            tele_extra[kind].append((ts, num))

    chronological: list[dict[str, Any]] = []
    for row in status_rows:
        loaded = _load_status_payload(row)
        if loaded is None:
            continue
        ts, payload = loaded
        tele = tele_by_ts.get(ts, {})
        battery_mv = _status_int(payload, "battery_mv")
        voltage = tele.get("voltage")
        if voltage is None and battery_mv is not None:
            voltage = round(battery_mv / 1000.0, 3)
        chronological.append(
            {
                "ts": ts,
                "battery_mv": battery_mv,
                "voltage": voltage,
                "temperature": tele.get("temperature"),
                "packets_recv": _status_int(payload, "packets_recv"),
                "packets_sent": _status_int(payload, "packets_sent"),
                "recv_errors": _status_int(payload, "recv_errors"),
                "noise_floor": _status_int(payload, "noise_floor"),
                "uptime_secs": _status_int(payload, "uptime_secs"),
            }
        )

    _attach_nearest(chronological, "temperature", tele_extra["temperature"])
    _attach_nearest(chronological, "voltage", tele_extra["voltage"])
    _interpolate_missing(
        chronological, "temperature", digits=1, extra=tele_extra["temperature"]
    )
    _interpolate_missing(chronological, "voltage", digits=3, extra=tele_extra["voltage"])
    _interpolate_missing(chronological, "noise_floor", digits=0)
    _apply_poll_deltas(chronological)
    chronological.reverse()
    return chronological[:limit]


def history_series(
    conn: sqlite3.Connection,
    unit: str,
    metric: str,
    *,
    since: int | None = None,
    hours: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Time series for the fleet UI sparklines."""
    if hours is not None:
        since = int(time.time()) - max(1, hours) * 3600
    elif since is None:
        since = int(time.time()) - 72 * 3600
    if metric == "voltage":
        rows = conn.execute(
            "SELECT ts, value FROM telemetry WHERE unit = ? AND type = 'voltage' "
            "AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (unit, since, limit),
        ).fetchall()
        return [{"ts": r["ts"], "value": r["value"]} for r in reversed(rows)]
    if metric == "temperature":
        rows = conn.execute(
            "SELECT ts, value FROM telemetry WHERE unit = ? AND type = 'temperature' "
            "AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (unit, since, limit),
        ).fetchall()
        return [{"ts": r["ts"], "value": r["value"]} for r in reversed(rows)]
    if metric in ("battery_mv", "noise_floor"):
        key = metric
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
            if val is not None:
                point: dict[str, Any] = {"ts": row["ts"], "value": val}
                if key == "battery_mv":
                    point["value"] = round(float(val) / 1000.0, 3)
                out.append(point)
        return out
    if metric == "packets":
        rows = conn.execute(
            "SELECT ts, payload FROM status WHERE unit = ? AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?",
            (unit, since, limit),
        ).fetchall()
        out = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            val = payload.get("packets_recv")
            sent = payload.get("packets_sent")
            out.append({"ts": row["ts"], "recv": val, "sent": sent})
        return out
    if metric in ("unreadable_pct", "recv_rate", "airtime_pct"):
        rows = status_series(conn, unit, days=max(1, (int(time.time()) - since) // 86400 + 1), limit=limit)
        filtered = [r for r in rows if r.get("ts", 0) >= since]
        return _derived_status_series(filtered, metric)
    return []


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
        "ota_at",
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
        "base_hash",
        "firmware_platform",
        "name_heard",
        "lat_heard",
        "lon_heard",
        "advert_interval_min",
        "flood_advert_interval_h",
        "node_clock",
        "battery_mv",
        "voltage",
        "uptime_secs",
        "temperature",
        "packets_recv",
        "packets_sent",
        "err_events",
        "recv_errors",
        "ota_status_at",
        "ota_ls_at",
        "ota_state",
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
    site_loc: tuple[float, float] | None = None,
) -> None:
    """Write a PollResult into last_seen + history tables.

    ``site_loc`` is book GPS at sample time (not device GET). Logged on
    status/telemetry rows so sun can be computed later.
    """
    now = ts or int(time.time())
    groups = getattr(res, "polled_groups", frozenset()) or frozenset()
    loc_lat = loc_lon = None
    if site_loc is not None:
        loc_lat, loc_lon = site_loc
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
    if "ota" in groups and res.base_hash is not None:
        fields["base_hash"] = res.base_hash
        fields["ota_at"] = now
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
        uptime = res.status.get("uptime_secs")
        if uptime is not None:
            try:
                fields["uptime_secs"] = int(uptime)
            except (TypeError, ValueError):
                pass
        status_payload = dict(res.status)
        if loc_lat is not None and loc_lon is not None:
            status_payload["site_lat"] = loc_lat
            status_payload["site_lon"] = loc_lon
        conn.execute(
            "INSERT INTO status (ts, unit, payload) VALUES (?, ?, ?)",
            (now, unit, json.dumps(status_payload, default=str)),
        )
    if "telemetry" in groups and res.telemetry is not None:
        fields["telemetry_at"] = now
        volt = _voltage_from_items(res.telemetry)
        if volt is not None:
            fields["voltage"] = volt
        temp = _temperature_from_items(res.telemetry)
        if temp is not None:
            fields["temperature"] = temp
        for item in res.telemetry:
            if not isinstance(item, dict):
                continue
            val = item.get("value")
            try:
                num = float(val) if val is not None else None
            except (TypeError, ValueError):
                num = None
            conn.execute(
                "INSERT INTO telemetry (ts, unit, channel, type, value, lat, lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, unit, item.get("channel"), item.get("type"), num, loc_lat, loc_lon),
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
    ota_groups = {"ota_status", "ota_ls"} & set(groups)
    if ota_groups and getattr(res, "ota", None) is not None:
        from envybot.ota_parse import merge_ota_snapshot

        prev = latest_ota(conn, unit)
        ota = res.ota
        status_part = None
        if "ota_status" in ota_groups:
            status_part = {
                "running": ota.get("running") or {},
                "local": ota.get("local") or {"state": "none"},
            }
        heard = ota.get("heard") if "ota_ls" in ota_groups else None
        snapshot = merge_ota_snapshot(
            prev,
            status=status_part,
            heard=heard,
            raw_status=ota.get("raw_status") if "ota_status" in ota_groups else None,
            raw_ls=ota.get("raw_ls") if "ota_ls" in ota_groups else None,
        )
        record_ota_snapshot(
            conn,
            unit=unit,
            snapshot=snapshot,
            ts=now,
            status_at="ota_status" in ota_groups,
            ls_at="ota_ls" in ota_groups,
        )
    _upsert_last_seen(conn, unit, fields)
    conn.commit()


def record_onboard_heard(
    conn: sqlite3.Connection,
    *,
    unit: str,
    firmware_version: str | None = None,
    bootloader_version: str | None = None,
    base_hash: str | None = None,
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
    if base_hash is not None:
        fields["base_hash"] = base_hash
        fields["ota_at"] = now
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
            uptime = status.get("uptime_secs")
            if uptime is not None:
                try:
                    fields["uptime_secs"] = int(uptime)
                except (TypeError, ValueError):
                    pass
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
            temp = _temperature_from_items(tele)
            if temp is not None:
                fields["temperature"] = temp
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
                    source="cmd",
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


def begin_mesh_audit(
    conn: sqlite3.Connection | None,
    *,
    unit: str,
    kind: str,
    label: str | None,
    attempt: int | None,
    path: str,
    wait_s: float | None,
    source: str | None = None,
) -> int | None:
    """Insert a mesh send row at send time. Returns row id or None if conn is omitted."""
    if conn is None:
        return None
    cur = conn.execute(
        "INSERT INTO mesh_audit "
        "(ts_sent, unit, kind, label, attempt, path, wait_s, ok, outcome, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?)",
        (time.time(), unit.lower(), kind, label, attempt, path, wait_s, source),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_mesh_audit(
    conn: sqlite3.Connection | None,
    audit_id: int | None,
    *,
    ok: bool,
    outcome: str,
    reply: str | None = None,
    error: str | None = None,
) -> None:
    """Close an open mesh_audit row. Timeout keeps ts_reply NULL."""
    if conn is None or audit_id is None:
        return
    ts_reply = None if outcome == "timeout" else time.time()
    conn.execute(
        "UPDATE mesh_audit SET ts_reply = ?, ok = ?, outcome = ?, reply = ?, error = ? "
        "WHERE id = ? AND outcome = 'pending'",
        (ts_reply, 1 if ok else 0, outcome, reply, error, audit_id),
    )
    conn.commit()


def mark_mesh_audit_late(
    conn: sqlite3.Connection | None,
    audit_id: int | None,
    *,
    reply: str | None = None,
) -> None:
    """Mark a timed-out row that got a late reply."""
    if conn is None or audit_id is None:
        return
    conn.execute(
        "UPDATE mesh_audit SET ts_reply = ?, ok = 1, outcome = 'late', "
        "reply = COALESCE(?, reply) WHERE id = ? AND outcome = 'timeout'",
        (time.time(), reply, audit_id),
    )
    conn.commit()


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
