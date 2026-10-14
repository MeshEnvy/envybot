"""Peaky book manual link overrides and per-pair RF records under ``links/``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    a, b = a.strip(), b.strip()
    return (a, b) if a <= b else (b, a)


def load_manual_links(book_dir: Path) -> list[tuple[str, str]]:
    """Manual override pairs from ``links/<a>/<b>.json`` with ``manual: true``."""
    root = book_dir / "links"
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for path in sorted(slug_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not data.get("manual"):
                continue
            a = str(data.get("a") or slug_dir.name)
            b = str(data.get("b") or path.stem)
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
