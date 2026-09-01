"""Resolve a fleet book selector to one MeshCore RouterTarget."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from envybot.nodes_doc import (
    HEX_PUBKEY_RE,
    PLACEHOLDER_PW,
    UNIT_NUM_RE,
    is_decommissioned,
    is_meshcore_platform,
    normalize_fleet_node,
)
from envybot.position import site_binding
from envybot.radio import RouterTarget, target_label

ADV_NAME_BRACE_RE = re.compile(r"\{[^}]*\}")
ADV_NAME_PIPE_RE = re.compile(r"\s*\|.*$")
ADV_NAME_SPACE_RE = re.compile(r"\s+")


@dataclass
class ResolveResult:
    target: RouterTarget | None = None
    error: str | None = None
    candidates: list[RouterTarget] = field(default_factory=list)


def normalize_adv_name(name: str) -> str:
    s = name.strip().lower()
    s = ADV_NAME_BRACE_RE.sub("", s)
    s = ADV_NAME_PIPE_RE.sub("", s)
    s = ADV_NAME_SPACE_RE.sub(" ", s).strip()
    return s


def _node_to_target(
    key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None = None,
) -> RouterTarget:
    normalize_fleet_node(node)
    unit_id = str(node.get("unit_id") or key.upper())
    pubkey = str(node["identity_pubkey"]).strip().lower()
    admin_pw = str(node["admin_password"]).strip()
    bind = site_binding(key, node, sites)
    return RouterTarget(
        key=key,
        unit_id=unit_id,
        name=str(node.get("name") or unit_id),
        site=bind[0] if bind else None,
        pubkey_hex=pubkey,
        admin_password=admin_pw,
    )


def iter_cmd_eligible(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    nodes = doc.get("nodes") or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if is_decommissioned(node):
            continue
        if not is_meshcore_platform(node):
            continue
        notes = str(node.get("notes") or "")
        if "RETIRED" in notes.upper():
            continue
        pubkey = node.get("identity_pubkey")
        if not pubkey or not isinstance(pubkey, str) or not HEX_PUBKEY_RE.match(pubkey.strip()):
            continue
        admin_pw = node.get("admin_password")
        if not admin_pw or not isinstance(admin_pw, str):
            continue
        admin_pw = admin_pw.strip()
        if not admin_pw or admin_pw in PLACEHOLDER_PW:
            continue
        out.append((key, node))
    return out


def _ineligible_reason(doc: dict[str, Any], selector: str) -> str | None:
    """One-line refusal when selector hits a row that cannot take remote CLI."""
    sel = selector.strip()
    sel_lower = sel.lower()
    nodes = doc.get("nodes") or {}
    for key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        unit_id = str(node.get("unit_id") or key.upper())
        name = str(node.get("name") or unit_id)
        matched = (
            key.lower() == sel_lower
            or unit_id.lower() == sel_lower
            or name.lower() == sel.lower()
            or normalize_adv_name(name) == normalize_adv_name(sel)
        )
        if not matched:
            continue
        if not is_meshcore_platform(node):
            return f"{unit_id}: firmware_platform is not meshcore (remote CLI unavailable)"
        admin_pw = node.get("admin_password")
        if not admin_pw or not isinstance(admin_pw, str) or not str(admin_pw).strip():
            platform = node.get("firmware_platform") or "meshtastic"
            return f"{unit_id}: no admin password ({platform}; remote CLI unavailable)"
        if str(admin_pw).strip() in PLACEHOLDER_PW:
            return f"{unit_id}: placeholder admin password (remote CLI unavailable)"
        if is_decommissioned(node):
            return f"{unit_id}: decommissioned"
        if "RETIRED" in str(node.get("notes") or "").upper():
            return f"{unit_id}: retired"
        pubkey = node.get("identity_pubkey")
        if not pubkey or not isinstance(pubkey, str) or not HEX_PUBKEY_RE.match(pubkey.strip()):
            return f"{unit_id}: missing or invalid identity pubkey"
    return None


def _tier_matches(
    eligible: list[tuple[str, dict[str, Any]]],
    *,
    predicate,
    sites: dict[str, dict[str, Any]] | None = None,
) -> list[RouterTarget]:
    hits: list[RouterTarget] = []
    for key, node in eligible:
        if predicate(key, node):
            hits.append(_node_to_target(key, node, sites))
    return hits


def resolve_selector(
    doc: dict[str, Any],
    selector: str,
    sites: dict[str, dict[str, Any]] | None = None,
) -> ResolveResult:
    sel = selector.strip()
    if not sel or sel == "-":
        return ResolveResult(error="selector required")

    eligible = iter_cmd_eligible(doc)
    sel_lower = sel.lower()

    tiers: list[list[RouterTarget]] = []

    tiers.append(
        _tier_matches(
            eligible,
            sites=sites,
            predicate=lambda key, _node: key.lower() == sel_lower,
        )
    )
    tiers.append(
        _tier_matches(
            eligible,
            sites=sites,
            predicate=lambda _key, node: str(node.get("unit_id") or "").lower() == sel_lower,
        )
    )

    if HEX_PUBKEY_RE.match(sel):
        tiers.append(
            _tier_matches(
                eligible,
                sites=sites,
                predicate=lambda _key, node: str(node["identity_pubkey"]).strip().lower() == sel.lower(),
            )
        )
    elif re.fullmatch(r"[0-9a-fA-F]{8,64}", sel):
        prefix = sel.lower()
        tiers.append(
            _tier_matches(
                eligible,
                sites=sites,
                predicate=lambda _key, node: str(node["identity_pubkey"]).strip().lower().startswith(prefix),
            )
        )

    tiers.append(
        _tier_matches(
            eligible,
            sites=sites,
            predicate=lambda _key, node: str(node.get("name") or "").lower() == sel.lower(),
        )
    )

    norm_sel = normalize_adv_name(sel)
    tiers.append(
        _tier_matches(
            eligible,
            sites=sites,
            predicate=lambda _key, node: normalize_adv_name(str(node.get("name") or "")) == norm_sel,
        )
    )

    tiers.append(
        _tier_matches(
            eligible,
            sites=sites,
            predicate=lambda _key, node: normalize_adv_name(str(node.get("name") or "")).startswith(norm_sel)
            and norm_sel,
        )
    )

    def _site_slug_match(key: str, node: dict[str, Any]) -> bool:
        bind = site_binding(key, node, sites)
        return bool(bind and bind[0].lower() == sel_lower)

    tiers.append(_tier_matches(eligible, sites=sites, predicate=_site_slug_match))

    for hits in tiers:
        if len(hits) == 1:
            return ResolveResult(target=hits[0])
        if len(hits) > 1:
            return ResolveResult(error="ambiguous selector", candidates=hits)

    reason = _ineligible_reason(doc, sel)
    if reason:
        return ResolveResult(error=reason)

    return ResolveResult(error=f"unknown selector {sel!r}")


def format_candidates(candidates: list[RouterTarget]) -> str:
    lines = ["Candidates:"]
    for target in candidates:
        lines.append(f"  {target_label(target)}")
    return "\n".join(lines)
