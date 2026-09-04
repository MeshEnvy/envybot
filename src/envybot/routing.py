"""Mesh send routing policy (book) and live companion route helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PATH_DISCARD_AFTER = 3
FLEET_PATH_HASH_MODE = 1


class RoutingMode(str, Enum):
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
    """Book stamp when set; None when default path applies."""
    if not node:
        return None
    explicit = node.get("routing")
    if isinstance(explicit, str) and explicit.strip():
        parsed = parse_routing_value(explicit)
        if parsed is not None:
            return parsed.value
    return None


def resolve_routing(node: dict[str, Any] | None) -> RoutingMode:
    """Effective routing policy. Default path for all units."""
    explicit = routing_explicit(node)
    if explicit is not None:
        return RoutingMode(explicit)
    return RoutingMode.PATH


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
    """Per-unit session routing state for path-mode stale cache handling."""

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
