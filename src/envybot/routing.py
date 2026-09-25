"""Mesh send routing policy (book) and live companion route helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PATH_DISCARD_AFTER = 3
FLEET_PATH_HASH_MODE = 1


class RoutingMode(str, Enum):
    AUTO = "auto"
    DIRECT = "direct"
    PATH = "path"
    FLOOD = "flood"


_VALID = frozenset(m.value for m in RoutingMode)


def parse_routing_value(raw: str | None) -> RoutingMode | None:
    if not raw or not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val not in _VALID:
        return None
    return RoutingMode(val)


def routing_explicit(node: dict[str, Any] | None) -> str | None:
    """Book stamp when set; None when default auto applies."""
    if not node:
        return None
    explicit = node.get("routing")
    if isinstance(explicit, str) and explicit.strip():
        parsed = parse_routing_value(explicit)
        if parsed is not None:
            return parsed.value
    return None


def resolve_routing(node: dict[str, Any] | None) -> RoutingMode:
    """Effective routing policy. Default auto for all units."""
    explicit = routing_explicit(node)
    if explicit is not None:
        return RoutingMode(explicit)
    return RoutingMode.AUTO


def has_cached_route(contact: dict[str, Any] | None) -> bool:
    """True when companion has a learned route (zero-hop direct or multi-hop)."""
    if not contact:
        return False
    plen = contact.get("out_path_len")
    if plen is None:
        return False
    try:
        return int(plen) >= 0
    except (TypeError, ValueError):
        return False


def contact_out_path_label(contact: dict[str, Any] | None) -> str | None:
    """Format companion out_path as space-separated hop hashes."""
    if not contact:
        return None
    plen = contact.get("out_path_len")
    if plen is None or int(plen) <= 0:
        return None
    raw = str(contact.get("out_path") or "").strip().lower()
    if not raw:
        return None
    mode = contact.get("out_path_hash_mode")
    if mode is None or int(mode) < 0:
        mode = FLEET_PATH_HASH_MODE
    chunk = (int(mode) + 1) * 2
    hops = [raw[i : i + chunk] for i in range(0, len(raw), chunk)]
    hops = [h for h in hops if h]
    return " ".join(hops) if hops else None


def contact_route_audit_label(contact: dict[str, Any] | None) -> str:
    """Audit/log route: direct, flood, or space-separated hop hashes."""
    if not contact:
        return "flood"
    plen = contact.get("out_path_len")
    if plen is not None and int(plen) == 0:
        return "direct"
    hops = contact_out_path_label(contact)
    if hops:
        return hops
    return "flood"


def live_route_from_contact(
    contact: dict[str, Any] | None,
    *,
    policy: RoutingMode,
) -> dict[str, Any]:
    """Live route snapshot for UI."""
    if policy is RoutingMode.FLOOD:
        return {"kind": "flood", "label": "flood", "fallback": False}
    if policy is RoutingMode.DIRECT:
        return {"kind": "direct", "label": "direct", "fallback": False}
    if policy is RoutingMode.PATH:
        return {"kind": "flood", "label": "flood", "fallback": True}
    label = contact_route_audit_label(contact)
    if label == "direct":
        return {"kind": "direct", "label": "direct", "fallback": False}
    if label == "flood":
        return {"kind": "flood", "label": "flood", "fallback": True}
    return {"kind": "hops", "label": label, "fallback": False}


def live_route_from_audit_path(path: str | None, *, policy: RoutingMode) -> dict[str, Any] | None:
    """Rebuild live route from mesh_audit.path when worker session is idle."""
    if not path:
        return None
    if policy is RoutingMode.FLOOD:
        return {"kind": "flood", "label": "flood", "fallback": False}
    if policy is RoutingMode.DIRECT:
        return {"kind": "direct", "label": "direct", "fallback": False}
    if policy is RoutingMode.PATH:
        if path and path not in ("direct", "flood"):
            return {"kind": "hops", "label": path, "fallback": False}
        return None
    if path == "direct":
        return {"kind": "direct", "label": "direct", "fallback": False}
    if path == "flood":
        return {"kind": "flood", "label": "flood", "fallback": True}
    return {"kind": "hops", "label": path, "fallback": False}


def abbrev_live_route_label(label: str, *, max_len: int = 14) -> str:
    if len(label) <= max_len:
        return label
    if " " in label:
        parts = label.split()
        if len(parts) >= 2:
            return f"{parts[0][:4]}…{parts[-1][-4:]}"
    return label[: max_len - 1] + "…"


@dataclass
class RouteSession:
    """Per-unit session routing state for auto-mode stale cache handling."""

    path_failures: int = 0

    def record_success(self) -> None:
        self.path_failures = 0

    def record_timeout(self, *, had_cached_route: bool) -> bool:
        """Increment failures when a cached path timed out. Returns True if cache should be discarded."""
        if not had_cached_route:
            return False
        self.path_failures += 1
        return self.path_failures >= PATH_DISCARD_AFTER

    def reset_failures(self) -> None:
        self.path_failures = 0


def route_session_from_extra(extra: dict[str, Any] | None) -> RouteSession:
    if not extra:
        return RouteSession()
    raw = extra.get("route_session")
    if isinstance(raw, RouteSession):
        return raw
    failures = 0
    if isinstance(raw, dict):
        try:
            failures = int(raw.get("path_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
    else:
        try:
            failures = int(extra.get("path_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
    return RouteSession(path_failures=failures)


def persist_route_session(extra: dict[str, Any], session: RouteSession) -> None:
    extra["path_failures"] = session.path_failures
    extra["route_session"] = {"path_failures": session.path_failures}


@dataclass(frozen=True)
class ForcedPath:
    """Operator-pinned hop list for companion out_path."""

    hops: tuple[str, ...]
    path_hex: str
    hash_mode: int

    def label(self) -> str:
        return " ".join(self.hops)

    def to_extra(self) -> dict[str, Any]:
        return {
            "hops": list(self.hops),
            "path_hex": self.path_hex,
            "hash_mode": self.hash_mode,
        }


def path_pin_fields_from_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Snapshot/UI fields for a session-pinned companion path."""
    forced = forced_path_from_extra(extra)
    if forced is None:
        return {"path_pinned": False, "forced_path_label": None}
    return {"path_pinned": True, "forced_path_label": forced.label()}


def _normalize_path_paste_text(text: str) -> str:
    """Prefer fenced markdown blocks; drop fence lines from loose paste."""
    stripped = text.strip()
    if "```" in stripped:
        parts = stripped.split("```")
        blocks = [p.strip() for i, p in enumerate(parts) if i % 2 == 1 and p.strip()]
        if blocks:
            return max(blocks, key=len)
        stripped = stripped.replace("```", "")
    lines = [ln for ln in stripped.splitlines() if ln.strip() and not ln.strip().startswith("```")]
    return "\n".join(lines)


# Companion path paste / pins use 2-byte hop ids (4 hex chars) regardless of book pref.
PASTE_HOP_HEX_LEN = (FLEET_PATH_HASH_MODE + 1) * 2


def _extract_path_hop_tokens(text: str, *, chunk: int = PASTE_HOP_HEX_LEN) -> list[str]:
    """Non-alphanumeric → space; keep tokens that are exactly ``chunk`` hex chars."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", text)
    hop_re = re.compile(rf"^[0-9a-f]{{{chunk}}}$")
    out: list[str] = []
    for piece in cleaned.split():
        token = piece.lower()
        if hop_re.fullmatch(token):
            out.append(token)
    return out


def parse_path_paste(
    text: str | None,
    *,
    hash_mode: int = FLEET_PATH_HASH_MODE,
    drop_prefix: str | None = None,
) -> ForcedPath:
    """Extract hop hashes from paste: alphanumeric tokens, 4-hex hops only."""
    del drop_prefix
    hash_mode = FLEET_PATH_HASH_MODE
    if not text or not isinstance(text, str):
        raise ValueError("path paste is empty")
    body = _normalize_path_paste_text(text)
    if not body.strip():
        raise ValueError("path paste is empty")
    tokens = _extract_path_hop_tokens(body)
    if not tokens:
        raise ValueError("no hop hashes found in paste")
    forced = parse_force_path(",".join(tokens), hash_mode=hash_mode)
    if forced is None:
        raise ValueError("no hop hashes found in paste")
    return forced


def parse_force_path(
    raw: str | None,
    *,
    hash_mode: int = FLEET_PATH_HASH_MODE,
) -> ForcedPath | None:
    """Parse ``EA6E,E9BD,C458`` into a companion out_path (default 2-byte hashes)."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    parts = [p.strip().lower() for p in re.split(r"[\s,]+", text) if p.strip()]
    if not parts:
        return None
    chunk = (hash_mode + 1) * 2
    hops: list[str] = []
    for part in parts:
        token = part.split(":", 1)[0].replace(":", "")
        if len(token) != chunk:
            raise ValueError(
                f"hop {part!r} must be {chunk} hex chars for hash_mode {hash_mode}"
            )
        try:
            int(token, 16)
        except ValueError as exc:
            raise ValueError(f"hop {part!r} is not valid hex") from exc
        hops.append(token)
    return ForcedPath(hops=tuple(hops), path_hex="".join(hops), hash_mode=hash_mode)


def forced_path_from_extra(extra: dict[str, Any] | None) -> ForcedPath | None:
    if not extra:
        return None
    raw = extra.get("forced_path")
    if isinstance(raw, ForcedPath):
        return raw
    if not isinstance(raw, dict):
        return None
    hops_raw = raw.get("hops")
    path_hex = raw.get("path_hex")
    hash_mode = raw.get("hash_mode", FLEET_PATH_HASH_MODE)
    if not isinstance(hops_raw, list) or not hops_raw or not isinstance(path_hex, str):
        return None
    try:
        hash_mode = int(hash_mode)
    except (TypeError, ValueError):
        hash_mode = FLEET_PATH_HASH_MODE
    hops = tuple(str(h).strip().lower() for h in hops_raw if str(h).strip())
    if not hops:
        return None
    return ForcedPath(hops=hops, path_hex=path_hex.lower(), hash_mode=hash_mode)


def book_route_from_node(node: dict[str, Any] | None) -> ForcedPath | None:
    """Locked path hops from nodes.yaml ``route``."""
    if not node:
        return None
    raw = node.get("route")
    if isinstance(raw, str) and raw.strip():
        return parse_force_path(raw)
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw if str(x).strip()]
        if parts:
            return parse_force_path(",".join(parts))
    return None


def book_route_label(node: dict[str, Any] | None) -> str | None:
    forced = book_route_from_node(node)
    return forced.label() if forced else None


def effective_forced_path(
    node: dict[str, Any] | None,
    route_extra: dict[str, Any] | None,
) -> ForcedPath | None:
    """CLI/session pin overrides book locked path."""
    forced = forced_path_from_extra(route_extra)
    if forced is not None:
        return forced
    if resolve_routing(node) is RoutingMode.PATH:
        return book_route_from_node(node)
    return None
