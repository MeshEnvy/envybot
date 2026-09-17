"""Central fleet book load + validation. Commands use this, not raw YAML reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from envybot.book import BookError
from envybot.history import migrate_legacy
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import load_nodes_doc, load_sites_for_book
from envybot.public_advert import collect_advert_name_violations


@dataclass
class Book:
    nodes_path: Path
    doc: dict[str, Any]
    nodes: dict[str, Any]
    sites: dict[str, dict[str, Any]]
    keys: dict[str, list[str]]
    conn: Any


def validate_book(
    doc: dict[str, Any],
    nodes: dict[str, Any],
    sites: dict[str, dict[str, Any]],
) -> list[str]:
    """Return human-readable validation errors (empty when OK)."""
    errors: list[str] = []
    errors.extend(collect_advert_name_violations(doc, nodes, sites))
    return errors


def format_book_errors(errors: list[str]) -> str:
    lines = ["book validation failed:"]
    for err in errors:
        lines.append(f"  {err}")
    lines.append("fix nodes.yaml / sites.yaml before running fleet")
    return "\n".join(lines)


def raise_on_book_errors(errors: list[str]) -> None:
    if errors:
        raise BookError(format_book_errors(errors))


def load_book(nodes_path: Path) -> Book:
    """Load nodes.yaml, sites.yaml, keys.yaml; validate; open history sqlite."""
    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        nodes = {}
        doc["nodes"] = nodes
    sites = load_sites_for_book(nodes_path)
    keys = load_keys(keys_path(nodes_path))
    raise_on_book_errors(validate_book(doc, nodes, sites))
    conn = migrate_legacy(nodes_path.parent, nodes)
    return Book(
        nodes_path=nodes_path,
        doc=doc,
        nodes=nodes,
        sites=sites,
        keys=keys,
        conn=conn,
    )
