"""Resolve fleet --only / --skip specs to nodes.yaml keys (comma lists + globs)."""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from envybot.nodes_doc import HEX_PUBKEY_RE
from envybot.position import lookup_site_name, node_alias, site_binding

HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]+$")
UNIT_KEY_SHAPE_RE = re.compile(r"^me[0-9a-f]+$", re.IGNORECASE)
MIN_PUBKEY_PREFIX_LEN = 4

GLOB_METACHAR = frozenset("*?[]")


class UnitFilterError(ValueError):
    """Invalid or unmatched --only / --skip token."""


def parse_unit_specs(values: list[str] | None) -> list[str]:
    """Flatten repeatable flag values; comma-split each."""
    if not values:
        return []
    tokens: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                tokens.append(part)
    return tokens


def _has_glob_metachar(token: str) -> bool:
    return any(ch in token for ch in GLOB_METACHAR)


def _glob_match(pattern: str, value: str) -> bool:
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _identity_strings(
    key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
) -> set[str]:
    out = {key.lower(), str(node.get("unit_id") or key.upper()).lower()}
    alias = node_alias(node)
    if alias:
        out.add(alias.lower())
    bind = site_binding(key, node, sites)
    if bind:
        slug, _site = bind
        out.add(slug.lower())
        site_name = lookup_site_name(slug, sites)
        if site_name:
            out.add(site_name.lower())
    pubkey = node.get("identity_pubkey")
    if isinstance(pubkey, str) and pubkey.strip():
        out.add(pubkey.strip().lower())
    return out


def _keys_by_pubkey_token(token: str, nodes: dict[str, Any]) -> set[str]:
    """Prefix or exact pubkey match (companion-style hints)."""
    if not HEX_TOKEN_RE.fullmatch(token) or UNIT_KEY_SHAPE_RE.fullmatch(token):
        return set()
    prefix = token.lower()
    matched: set[str] = set()
    if len(prefix) == 64 and HEX_PUBKEY_RE.match(prefix):
        for key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            pub = str(node.get("identity_pubkey") or "").strip().lower()
            if pub == prefix:
                matched.add(key.lower())
        return matched
    if len(prefix) < MIN_PUBKEY_PREFIX_LEN:
        return set()
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        pub = str(node.get("identity_pubkey") or "").strip().lower()
        if pub.startswith(prefix):
            matched.add(key.lower())
    return matched


def _keys_for_token(
    token: str,
    doc: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
) -> set[str]:
    nodes = doc.get("nodes") or {}
    matched: set[str] = set()
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        identities = _identity_strings(key, node, sites)
        if any(_glob_match(token, ident) for ident in identities):
            matched.add(key.lower())
    if not matched:
        matched |= _keys_by_pubkey_token(token, nodes)
    return matched


def key_in_unit_filter(
    key: str,
    *,
    include: set[str] | None,
    skip: set[str] | None,
) -> bool:
    """True when key passes --only / --skip (earliest fleet node filter)."""
    k = key.lower()
    if include is not None and k not in include:
        return False
    if skip is not None and k in skip:
        return False
    return True


def resolve_unit_specs(
    doc: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    specs: list[str],
    *,
    flag: str = "--only",
) -> set[str]:
    """OR-match every token; return lowercase book keys."""
    if not specs:
        return set()

    matched_keys: set[str] = set()
    for token in specs:
        token_keys = _keys_for_token(token, doc, sites)
        if not token_keys:
            if _has_glob_metachar(token):
                raise UnitFilterError(f"{flag} {token!r} matched nothing")
            raise UnitFilterError(f"unknown unit {token!r}")
        matched_keys |= token_keys
    return matched_keys
