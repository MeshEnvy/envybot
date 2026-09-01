"""Clear-sky solar elevation for fleet charging-expected marks."""

from __future__ import annotations

import math
from typing import Any

from envybot.position import is_placeholder_gps

# Geometric horizon. Clear-sky only; no clouds, terrain, or panel tilt.
_CHARGING_ELEV = 0.0


def _julian_day(ts: int) -> float:
    return ts / 86400.0 + 2440587.5


def solar_elev_az(lat: float, lon: float, ts: int) -> tuple[float, float]:
    """Apparent solar elevation and azimuth (degrees). Azimuth 0=N, 90=E."""
    n = _julian_day(int(ts)) - 2451545.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    mean_anom = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic = math.radians(
        (mean_lon + 1.915 * math.sin(mean_anom) + 0.020 * math.sin(2.0 * mean_anom)) % 360.0
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))
    dec = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    lst = (gmst + lon / 15.0) % 24.0
    ha = math.radians(lst * 15.0) - ra
    lat_r = math.radians(lat)
    sin_elev = math.sin(lat_r) * math.sin(dec) + math.cos(lat_r) * math.cos(dec) * math.cos(ha)
    elev = math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))
    az = math.degrees(
        math.atan2(
            -math.sin(ha),
            math.tan(dec) * math.cos(lat_r) - math.sin(lat_r) * math.cos(ha),
        )
    )
    az = az % 360.0
    return elev, az


def sun_at(lat: Any, lon: Any, ts: int) -> dict[str, Any] | None:
    """Charging-expected mark for a site at UTC epoch ``ts``."""
    if is_placeholder_gps(lat, lon):
        return None
    try:
        la = float(lat)
        lo = float(lon)
        when = int(ts)
    except (TypeError, ValueError):
        return None
    elev, az = solar_elev_az(la, lo, when)
    charging = elev >= _CHARGING_ELEV
    if charging:
        phase, emoji, label = "day", "☀️", "charging expected"
    else:
        phase, emoji, label = "night", "🌙", "not charging"
    return {
        "elev": round(elev, 1),
        "az": round(az, 1),
        "charging": charging,
        "phase": phase,
        "emoji": emoji,
        "label": f"{label} · {elev:.0f}∠",
    }


def stamp_sun(
    row: dict[str, Any],
    loc: tuple[float, float] | None = None,
) -> None:
    """Add ``sun`` from the row's lat/lon + ts, else ``loc`` fallback."""
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
    payload = sun_at(lat, lon, when)
    if payload is not None:
        row["sun"] = payload


def attach_sun(
    rows: list[dict[str, Any]] | None,
    loc: tuple[float, float] | None = None,
) -> None:
    """Stamp ``sun`` from sample GPS, or ``loc`` when the row has none."""
    if not rows:
        return
    for row in rows:
        stamp_sun(row, loc=loc)


def sun_series(
    lat: Any,
    lon: Any,
    start_ts: int,
    end_ts: int,
    *,
    step: int = 900,
) -> list[dict[str, Any]]:
    """Dense clear-sky elevation points for a sparkline (``step`` seconds)."""
    if is_placeholder_gps(lat, lon):
        return []
    try:
        la = float(lat)
        lo = float(lon)
        start = int(start_ts)
        end = int(end_ts)
        stride = max(60, int(step))
    except (TypeError, ValueError):
        return []
    if end < start:
        start, end = end, start
    out: list[dict[str, Any]] = []
    t = start
    while t <= end:
        elev, _az = solar_elev_az(la, lo, t)
        out.append({"ts": t, "value": round(elev, 1)})
        t += stride
    if not out or out[-1]["ts"] != end:
        elev, _az = solar_elev_az(la, lo, end)
        out.append({"ts": end, "value": round(elev, 1)})
    return out
