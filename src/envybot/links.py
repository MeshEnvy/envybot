"""Peaky book link overrides (``links.json``) and per-pair RF cache (``links/``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    a, b = a.strip(), b.strip()
    return (a, b) if a <= b else (b, a)


def load_manual_links(book_dir: Path) -> list[tuple[str, str]]:
    """Manual override pairs from ``links.json`` only."""
    path = book_dir / "links.json"
    if not path.is_file():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw = doc.get("links") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, list) and len(item) == 2:
            a, b = str(item[0]), str(item[1])
        elif isinstance(item, dict):
            a = str(item.get("a") or "")
            b = str(item.get("b") or "")
        else:
            continue
        if a and b and a != b:
            out.append(_canonical_pair(a, b))
    return out


def read_link_status(book_dir: Path, slug_a: str, slug_b: str) -> dict[str, Any] | None:
    """Cached RF result for a site pair, if present under ``links/<a>/<b>.json``."""
    a, b = _canonical_pair(slug_a, slug_b)
    path = book_dir / "links" / a / f"{b}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None
