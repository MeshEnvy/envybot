"""Per-node health checks from sqlite history and snapshot fields."""

from __future__ import annotations

from typing import Any, Literal

from envybot.history import count_reboots

CheckStatus = Literal["ok", "warn", "bad", "unknown"]
Grade = Literal["ok", "warn", "bad", "unknown"]
Headline = Literal["paused", "healthy", "unreachable", "attention"]

_IN_FLIGHT = frozenset({"queued", "refreshing", "pulling", "pushing", "polling"})

# Power (volts)
POWER_WARN_V = 3.7
POWER_BAD_V = 3.5
POWER_TREND_DROP_V = 0.15
POWER_TREND_POLLS = 3

# Stability
STABILITY_WARN_REBOOTS = 1
STABILITY_BAD_REBOOTS = 2

# Traffic
TRAFFIC_DEAD_AIR_HOURS = 6
TRAFFIC_DEAF_HOURS = 12

# RF quality
RF_WARN_LIFETIME_DELTA_PTS = 15.0
RF_BAD_PCT = 90.0

# Channel utilization (RX airtime / wall clock)
UTIL_WARN_PCT = 30.0
UTIL_BAD_PCT = 60.0

# Temperature (°C)
TEMP_WARN_HIGH = 60.0
TEMP_WARN_LOW = -20.0
TEMP_BAD_HIGH = 75.0

_GRADE_RANK = {"ok": 0, "warn": 1, "bad": 2, "unknown": -1}


def _check(
    name: str,
    status: CheckStatus,
    reason: str | None = None,
    fix: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "status": status, "reason": reason}
    if fix:
        row["fix"] = fix
    return row


def _voltage_volts(status: dict[str, Any] | None) -> float | None:
    """Status ``battery_mv`` only. Telemetry voltage is stored, not used."""
    if not status:
        return None
    mv = status.get("battery_mv")
    if mv is None:
        return None
    try:
        return float(mv) / 1000.0
    except (TypeError, ValueError):
        return None


def _unreadable_pct(recv_errors: int | None, packets_recv: int | None) -> float | None:
    if recv_errors is None:
        return None
    recv = packets_recv or 0
    total = recv_errors + recv
    if total <= 0:
        return 0.0
    return (recv_errors / total) * 100.0


def _voltages_from_rows(status_rows: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for row in status_rows:
        mv = row.get("battery_mv")
        if mv is None:
            continue
        try:
            out.append(float(mv) / 1000.0)
        except (TypeError, ValueError):
            continue
    return out


def _worst_grade(checks: list[dict[str, Any]]) -> Grade:
    worst: Grade = "ok"
    for chk in checks:
        st = chk.get("status")
        if st not in _GRADE_RANK or st == "unknown":
            continue
        if _GRADE_RANK[st] > _GRADE_RANK[worst]:
            worst = st  # type: ignore[assignment]
    return worst


def compute_health(
    *,
    freshness: str,
    session: dict[str, Any] | None,
    drift: str | None,
    status: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
    traffic_interval: dict[str, Any] | None,
    traffic_window: dict[str, Any] | None = None,
    status_rows: list[dict[str, Any]],
    reboot_count: int | None = None,
    paused: bool = False,
) -> dict[str, Any]:
    """Return grade + component checks for a fleet unit."""
    checks: list[dict[str, Any]] = []

    # Reachability
    session_state = (session or {}).get("state")
    if paused:
        checks.append(_check("Reachability", "unknown", "Polling paused"))
    elif session_state in ("queued", "refreshing", "pulling", "pushing", "polling"):
        checks.append(_check("Reachability", "unknown", "Poll in progress"))
    elif freshness == "never":
        checks.append(
            _check(
                "Reachability",
                "bad",
                "Never heard from node",
                fix="Refresh. If it stays silent, check companion path and that the unit is on.",
            )
        )
    elif session_state == "unreachable":
        checks.append(
            _check(
                "Reachability",
                "bad",
                "Unreachable this poll pass",
                fix="Refresh again. If login keeps timing out, check path, flood, and that the radio is up.",
            )
        )
    elif freshness == "stale":
        checks.append(
            _check(
                "Reachability",
                "warn",
                "Last heard is stale",
                fix="Refresh. If it stays stale, check the path and companion.",
            )
        )
    else:
        checks.append(_check("Reachability", "ok"))

    # Power
    volts = _voltage_volts(status)
    volt_history = _voltages_from_rows(status_rows)
    if volts is None:
        checks.append(_check("Power", "unknown", "No voltage reading"))
    else:
        trend_drop = False
        if len(volt_history) >= POWER_TREND_POLLS:
            recent = volt_history[-POWER_TREND_POLLS :]
            if recent[0] - recent[-1] >= POWER_TREND_DROP_V:
                trend_drop = True
        if volts < POWER_BAD_V:
            checks.append(
                _check(
                    "Power",
                    "bad",
                    f"{volts:.2f} V below {POWER_BAD_V:.1f} V",
                    fix="Check solar, battery, and charger. Replace the pack if it does not recover in sun.",
                )
            )
        elif volts < POWER_WARN_V or trend_drop:
            reason = f"{volts:.2f} V"
            if trend_drop:
                reason += " falling"
            checks.append(
                _check(
                    "Power",
                    "warn",
                    reason,
                    fix="Watch voltage. Inspect the power path if it keeps dropping.",
                )
            )
        else:
            checks.append(_check("Power", "ok", f"{volts:.2f} V"))

    # Stability
    if reboot_count is None:
        reboot_count = count_reboots(status_rows)
    if len(status_rows) < 2:
        checks.append(_check("Stability", "unknown", "Not enough poll history"))
    elif reboot_count >= STABILITY_BAD_REBOOTS:
        checks.append(
            _check(
                "Stability",
                "bad",
                f"{reboot_count} reboots in recent history",
                fix="Look for brownouts, OTA reboot loops, or a hot enclosure.",
            )
        )
    elif reboot_count >= STABILITY_WARN_REBOOTS:
        checks.append(
            _check(
                "Stability",
                "warn",
                f"{reboot_count} reboot in recent history",
                fix="Refresh again to confirm it stays up.",
            )
        )
    else:
        checks.append(_check("Stability", "ok"))

    # Traffic (rolling window for dead air / deaf; last poll kept for detail UI)
    window = traffic_window or {}
    min_cover = TRAFFIC_DEAD_AIR_HOURS * 3600
    if window.get("reboot_reset") and window.get("packets_recv") is None:
        checks.append(_check("Traffic", "unknown", "Counters reset by reboot"))
    elif not window:
        checks.append(_check("Traffic", "unknown", "No 6 h traffic window"))
    else:
        d_in = window.get("packets_recv")
        d_out = window.get("packets_sent")
        covered = window.get("duration_secs") or 0
        if covered < min_cover:
            hours = covered / 3600
            checks.append(
                _check(
                    "Traffic",
                    "unknown",
                    f"Only {hours:.1f} h of status history in 6 h window",
                )
            )
        elif d_in == 0 and d_out == 0:
            checks.append(
                _check(
                    "Traffic",
                    "bad",
                    f"No packets in or out over {TRAFFIC_DEAD_AIR_HOURS} h",
                    fix="Confirm the radio is on-air. Check neighbors and duty cycle.",
                )
            )
        elif d_in == 0 and covered >= TRAFFIC_DEAF_HOURS * 3600:
            checks.append(
                _check(
                    "Traffic",
                    "warn",
                    f"No decoded RX over {TRAFFIC_DEAF_HOURS} h window",
                    fix="Node may be deaf or isolated. Check antenna, noise floor, and neighbors.",
                )
            )
        else:
            checks.append(_check("Traffic", "ok"))

    # RF quality
    interval = traffic_interval or {}
    if interval and not interval.get("reboot_reset"):
        interval_pct = _unreadable_pct(
            interval.get("recv_errors"),
            interval.get("packets_recv"),
        )
        lifetime_pct = _unreadable_pct(
            status.get("recv_errors") if status else None,
            status.get("packets_recv") if status else None,
        )
        if interval_pct is None:
            checks.append(_check("RF quality", "unknown", "No interval RF data"))
        elif interval_pct >= RF_BAD_PCT:
            checks.append(
                _check(
                    "RF quality",
                    "bad",
                    f"Unreadable {interval_pct:.0f}% this interval",
                    fix="Interference or a failing radio. Check noise floor, antenna, and nearby TX.",
                )
            )
        elif lifetime_pct is not None and interval_pct >= lifetime_pct + RF_WARN_LIFETIME_DELTA_PTS:
            checks.append(
                _check(
                    "RF quality",
                    "warn",
                    f"Unreadable {interval_pct:.0f}% vs {lifetime_pct:.0f}% lifetime",
                    fix="RF got worse this interval. Compare noise floor and channel utilization.",
                )
            )
        else:
            checks.append(_check("RF quality", "ok"))
    else:
        checks.append(_check("RF quality", "unknown"))

    # Utilization
    if interval and not interval.get("reboot_reset"):
        util = interval.get("rx_airtime_pct")
        if util is None:
            checks.append(_check("Utilization", "unknown", "No airtime data"))
        elif util >= UTIL_BAD_PCT:
            checks.append(
                _check(
                    "Utilization",
                    "bad",
                    f"RX airtime {util:.0f}%",
                    fix="Channel is jammed. Reduce flood advert or duty, or move off a busy frequency.",
                )
            )
        elif util >= UTIL_WARN_PCT:
            checks.append(
                _check(
                    "Utilization",
                    "warn",
                    f"RX airtime {util:.0f}%",
                    fix="High channel use. Watch for collisions.",
                )
            )
        else:
            checks.append(_check("Utilization", "ok"))
    else:
        checks.append(_check("Utilization", "unknown"))

    # Config / drift
    if drift == "leak":
        checks.append(
            _check(
                "Config",
                "warn",
                "Private node needs profile apply (leak)",
                fix="Push from the detail pane to SET the book profile (name, GPS off, ACL).",
            )
        )
    elif drift == "mismatch":
        checks.append(
            _check(
                "Config",
                "warn",
                "Public node profile mismatch",
                fix="Push from the detail pane to SET name, GPS, and ACL to the book.",
            )
        )
    else:
        checks.append(_check("Config", "ok"))

    # Temperature
    temp: float | None = None
    if telemetry:
        t = telemetry.get("temperature")
        if t is not None:
            try:
                temp = float(t)
            except (TypeError, ValueError):
                temp = None
    if temp is None:
        checks.append(_check("Temperature", "unknown", "No temperature reading"))
    elif temp >= TEMP_BAD_HIGH:
        checks.append(
            _check(
                "Temperature",
                "bad",
                f"{temp * 9 / 5 + 32:.0f} °F",
                fix="Shade, vent, or move the enclosure.",
            )
        )
    elif temp >= TEMP_WARN_HIGH or temp <= TEMP_WARN_LOW:
        checks.append(
            _check(
                "Temperature",
                "warn",
                f"{temp * 9 / 5 + 32:.0f} °F",
                fix="Check enclosure placement and sun load.",
            )
        )
    else:
        checks.append(_check("Temperature", "ok", f"{temp * 9 / 5 + 32:.0f} °F"))

    grade = _worst_grade(checks)
    issues = [c for c in checks if c["status"] not in ("ok", "unknown")]
    if not issues:
        summary = "Healthy — all checks pass"
    else:
        first = issues[0]
        summary = f"{first['name']}: {first.get('reason') or first['status']}"

    in_flight = session_state in _IN_FLIGHT
    if paused and not in_flight:
        headline: Headline = "paused"
    elif not in_flight and (session_state == "unreachable" or freshness == "never"):
        headline = "unreachable"
    elif issues:
        headline = "attention"
    else:
        headline = "healthy"

    return {
        "grade": grade,
        "headline": headline,
        "summary": summary,
        "checks": checks,
        "issues": issues,
    }
