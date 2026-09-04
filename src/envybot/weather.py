"""Open-Meteo weather cache and attach for fleet poll history."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from envybot.position import is_placeholder_gps

WEATHER_BACKFILL_META = "weather_backfill_at"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = (
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
)

RECENT_DAYS = 92
FETCH_CHUNK_DAYS = 31
REQUEST_TIMEOUT_S = 30


def lat_lon_cell(lat: float, lon: float) -> tuple[float, float]:
    return round(float(lat), 3), round(float(lon), 3)


def hour_floor(ts: int) -> int:
    return int(ts) // 3600 * 3600


def _iso_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_hourly_time(raw: str) -> int:
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 16:
        text += ":00+00:00"
    elif len(text) == 19 and "+" not in text and "T" in text:
        text += "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def weather_backfill_pending(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (WEATHER_BACKFILL_META,)
    ).fetchone()
    return row is None


def _format_label(payload: dict[str, Any]) -> str:
    temp = payload.get("temp_c")
    cloud = payload.get("cloud_pct")
    precip = payload.get("precip_mm")
    wind = payload.get("wind_ms")
    parts: list[str] = []
    if temp is not None:
        parts.append(f"{temp:.0f} °C ambient")
    if cloud is not None:
        parts.append(f"{cloud:.0f}% cloud")
    if precip is not None and precip > 0.05:
        parts.append(f"{precip:.1f} mm/h precip")
    else:
        parts.append("dry")
    if wind is not None:
        if wind < 1.0:
            parts.append("calm")
        else:
            parts.append(f"{wind:.1f} m/s wind")
    return " · ".join(parts)


def _payload_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mapping = {
        "temperature_2m": "temp_c",
        "cloud_cover": "cloud_pct",
        "shortwave_radiation": "shortwave_wm2",
        "precipitation": "precip_mm",
        "weather_code": "weather_code",
        "wind_speed_10m": "wind_ms",
        "wind_gusts_10m": "wind_gust_ms",
        "wind_direction_10m": "wind_dir",
    }
    for src, dst in mapping.items():
        val = raw.get(src)
        if val is None:
            continue
        try:
            out[dst] = float(val)
        except (TypeError, ValueError):
            continue
    if "weather_code" in raw and raw["weather_code"] is not None:
        try:
            out["weather_code"] = int(raw["weather_code"])
        except (TypeError, ValueError):
            pass
    out["label"] = _format_label(out)
    return out


def lookup_weather(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    ts: int,
) -> dict[str, Any] | None:
    lat_c, lon_c = lat_lon_cell(lat, lon)
    hour = hour_floor(ts)
    row = conn.execute(
        "SELECT payload FROM weather_hourly "
        "WHERE lat_cell = ? AND lon_cell = ? AND hour_ts = ?",
        (lat_c, lon_c, hour),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return _payload_from_raw(raw)


def _insert_hours(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    hours: dict[int, dict[str, Any]],
) -> int:
    if not hours:
        return 0
    lat_c, lon_c = lat_lon_cell(lat, lon)
    n = 0
    for hour_ts, payload in hours.items():
        conn.execute(
            "INSERT OR REPLACE INTO weather_hourly (lat_cell, lon_cell, hour_ts, payload) "
            "VALUES (?, ?, ?, ?)",
            (lat_c, lon_c, int(hour_ts), json.dumps(payload, default=str)),
        )
        n += 1
    conn.commit()
    return n


def _fetch_open_meteo(url: str, params: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": "envybot/0.1 (MeshEnvy fleet weather)"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Open-Meteo response is not an object")
    return data


def _parse_hourly_response(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time")
    if not isinstance(times, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for i, when in enumerate(times):
        try:
            hour_ts = _parse_hourly_time(str(when))
        except (TypeError, ValueError):
            continue
        raw: dict[str, Any] = {}
        for var in HOURLY_VARS:
            series = hourly.get(var)
            if not isinstance(series, list) or i >= len(series):
                continue
            raw[var] = series[i]
        out[hour_ts] = raw
    return out


def _fetch_range(
    lat: float,
    lon: float,
    start_ts: int,
    end_ts: int,
) -> dict[int, dict[str, Any]]:
    start = hour_floor(start_ts)
    end = hour_floor(end_ts)
    if end < start:
        start, end = end, start
    now = int(time.time())
    recent_cutoff = now - RECENT_DAYS * 86400
    merged: dict[int, dict[str, Any]] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + FETCH_CHUNK_DAYS * 86400)
        use_forecast = chunk_start >= recent_cutoff - 86400
        url = FORECAST_URL if use_forecast else ARCHIVE_URL
        params = {
            "latitude": f"{lat:.6f}",
            "longitude": f"{lon:.6f}",
            "hourly": ",".join(HOURLY_VARS),
            "start_date": _iso_date(chunk_start),
            "end_date": _iso_date(chunk_end),
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        data = _fetch_open_meteo(url, params)
        merged.update(_parse_hourly_response(data))
        chunk_start = chunk_end + 3600
    return merged


def missing_hours(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    start_ts: int,
    end_ts: int,
) -> list[int]:
    start = hour_floor(start_ts)
    end = hour_floor(end_ts)
    if end < start:
        start, end = end, start
    lat_c, lon_c = lat_lon_cell(lat, lon)
    rows = conn.execute(
        "SELECT hour_ts FROM weather_hourly "
        "WHERE lat_cell = ? AND lon_cell = ? AND hour_ts >= ? AND hour_ts <= ?",
        (lat_c, lon_c, start, end),
    ).fetchall()
    have = {int(r["hour_ts"]) for r in rows}
    missing: list[int] = []
    t = start
    while t <= end:
        if t not in have:
            missing.append(t)
        t += 3600
    return missing


def ensure_weather_cached(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    start_ts: int,
    end_ts: int,
) -> int:
    """Fetch Open-Meteo for any missing hours in range. Returns rows inserted."""
    if is_placeholder_gps(lat, lon):
        return 0
    missing = missing_hours(conn, lat, lon, start_ts, end_ts)
    if not missing:
        return 0
    fetched = _fetch_range(lat, lon, missing[0], missing[-1])
    to_store = {h: fetched[h] for h in missing if h in fetched}
    return _insert_hours(conn, lat, lon, to_store)


def weather_at(
    conn: sqlite3.Connection,
    lat: Any,
    lon: Any,
    ts: int,
) -> dict[str, Any] | None:
    if is_placeholder_gps(lat, lon):
        return None
    try:
        la = float(lat)
        lo = float(lon)
        when = int(ts)
    except (TypeError, ValueError):
        return None
    ensure_weather_cached(conn, la, lo, when, when)
    return lookup_weather(conn, la, lo, when)


def stamp_weather(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    loc: tuple[float, float] | None = None,
) -> None:
    ts = row.get("ts")
    if ts is None:
        return
    try:
        when = int(ts)
    except (TypeError, ValueError):
        return
    lat = row.get("lat")
    lon = row.get("lon")
    if lat is None or lon is None:
        if not loc:
            return
        lat, lon = loc
    payload = weather_at(conn, lat, lon, when)
    if payload is not None:
        row["weather"] = payload


def attach_weather(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]] | None,
    loc: tuple[float, float] | None = None,
) -> None:
    if not rows:
        return
    effective_loc = loc
    for row in rows:
        lat = row.get("lat")
        lon = row.get("lon")
        if lat is not None and lon is not None and not is_placeholder_gps(lat, lon):
            effective_loc = (float(lat), float(lon))
            break
    if not effective_loc or is_placeholder_gps(*effective_loc):
        return
    times: list[int] = []
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            continue
        try:
            times.append(int(ts))
        except (TypeError, ValueError):
            continue
    if times:
        ensure_weather_cached(conn, effective_loc[0], effective_loc[1], min(times), max(times))
    for row in rows:
        stamp_weather(conn, row, loc=effective_loc)


def run_weather_backfill(
    conn: sqlite3.Connection,
    book: Any,
    *,
    force: bool = False,
    sleep_s: float = 0.2,
) -> dict[str, Any]:
    """One-shot cache fill for bound sites × history span."""
    from pathlib import Path

    from envybot.nodes_doc import load_nodes_doc
    from envybot.position import index_sites_by_node, load_sites, site_loc, site_loc_for_unit

    if not force and not weather_backfill_pending(conn):
        return {"skipped": True, "reason": "already done"}

    book = Path(book)
    nodes_path = book / "nodes.yaml"
    sites_path = book / "sites.yaml"
    if not nodes_path.is_file():
        return {"skipped": True, "reason": "no nodes.yaml"}

    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    sites = load_sites(sites_path) if sites_path.is_file() else {}
    by_node = index_sites_by_node(sites)

    loc_jobs: dict[tuple[float, float], list[str]] = {}
    for unit, node in nodes.items():
        if not isinstance(node, dict):
            continue
        key = str(unit).strip().lower()
        loc = site_loc_for_unit(key, node, sites, doc=doc)
        if not loc:
            bind = by_node.get(key)
            if bind:
                _slug, site = bind
                loc = site_loc(site)
        if not loc:
            continue
        loc_jobs.setdefault(loc, []).append(key)

    total_hours = 0
    site_count = 0
    for loc, units in loc_jobs.items():
        placeholders = ",".join("?" * len(units))
        row = conn.execute(
            f"""
            SELECT MIN(mn) AS mn, MAX(mx) AS mx FROM (
              SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM status
                WHERE unit IN ({placeholders})
              UNION ALL
              SELECT MIN(ts), MAX(ts) FROM telemetry
                WHERE unit IN ({placeholders})
            )
            """,
            (*units, *units),
        ).fetchone()
        if not row or row["mn"] is None or row["mx"] is None:
            continue
        start_ts = int(row["mn"])
        end_ts = int(row["mx"])
        # Pad one hour each side for boundary samples.
        start_ts = hour_floor(start_ts) - 3600
        end_ts = hour_floor(end_ts) + 3600
        total_hours += ensure_weather_cached(conn, loc[0], loc[1], start_ts, end_ts)
        site_count += 1
        if sleep_s > 0:
            time.sleep(sleep_s)

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (WEATHER_BACKFILL_META, str(int(time.time()))),
    )
    conn.commit()
    return {
        "skipped": False,
        "sites": site_count,
        "hours_inserted": total_hours,
    }
