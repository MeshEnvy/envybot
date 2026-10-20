"""Shared profile value formatting for book and on-radio columns."""

from __future__ import annotations

from typing import Any


def pref_display(field: str, value: Any) -> str:
    if field in ("powersaving", "fem_rxgain", "rxgain", "repeat"):
        if isinstance(value, bool):
            return "on" if value else "off"
        text = str(value).strip().lower()
        if text in ("1", "true", "on", "yes"):
            return "on"
        if text in ("0", "false", "off", "no"):
            return "off"
        return text
    if field == "dutycycle":
        try:
            return f"{int(round(float(value)))}%"
        except (TypeError, ValueError):
            return str(value)
    if field == "path_hash":
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return str(value)
        return "2-byte" if mode == 1 else str(mode)
    if field in ("advert", "flood"):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)
    return str(value)
