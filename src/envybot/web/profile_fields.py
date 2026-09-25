"""Fleet UI profile rows: book desired + apply stamp state."""

from __future__ import annotations

from typing import Any

from envybot.apply import (
    applicable_field_desireds,
    board_allows_rxgain,
    desired_rxgain,
    profile_parts,
)
from envybot.full_sync import node_full_sync_interval_label
from envybot.keys_doc import UnknownPerson, resolve_node_acl
from envybot.web.acl_view import build_acl_table_rows
from envybot.nodes_doc import is_paused
from envybot.position import site_binding
from envybot.public_advert import load_public_advert_config, owner_info_for_apply

# Subset shown as header badges (radio knobs).
RADIO_PREF_FIELDS = (
    ("repeat", "Repeat"),
    ("powersaving", "Power saving"),
    ("hop_retry", "Hop retry"),
    ("hop_retry_ms", "Hop retry ms"),
    ("fem_rxgain", "FEM LNA"),
    ("agc_reset_interval", "AGC reset"),
    ("rxgain", "SX1262 boost"),
    ("dutycycle", "Duty cycle"),
    ("path_hash", "Path hash"),
    ("ota_autofetch", "OTA autofetch"),
    ("advert", "Advert min"),
    ("flood", "Flood advert h"),
)

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


PROFILE_ENUM_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "path_hash": [
        {"value": 0, "label": "4-byte (0)"},
        {"value": 1, "label": "2-byte (1)"},
    ],
    "ota_autofetch": [
        {"value": "off", "label": "off"},
        {"value": "any", "label": "any"},
        {"value": "signed", "label": "signed"},
    ],
}

PROFILE_IDENTITY_FIELDS = (
    ("name", "Name"),
    ("lat", "Latitude"),
    ("lon", "Longitude"),
    ("owner", "Owner info"),
    ("acl", "ACL"),
    ("admin", "Admin password"),
    ("guest", "Guest password"),
)


def _row_state(field: str, applicable: dict[str, str], stamped: dict[str, str]) -> str:
    if field not in applicable:
        return "n/a"
    desired = applicable[field]
    if stamped.get(field) == desired:
        return "synced"
    if stamped.get(field) is None:
        return "due"
    return "due"


def build_profile_rows(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
    apply_desireds: dict[str, str] | None = None,
    include_secrets: bool = False,
    heard_acl: list[dict[str, Any]] | None = None,
    acl_heard_at: int | None = None,
) -> list[dict[str, Any]]:
    parts = profile_parts(node, sites, doc=doc, keys=keys, key=key)
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=key)
    stamped = apply_desireds or {}
    bind = site_binding(key, node, sites)
    config = load_public_advert_config(doc)
    book_owner = (config.owner_info if config else "") or ""
    rows: list[dict[str, Any]] = []

    def add(
        field: str,
        label: str,
        *,
        value: Any,
        kind: str,
        editable: bool = True,
        source: str = "node",
        note: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        state = _row_state(field, applicable, stamped)
        row: dict[str, Any] = {
            "id": field,
            "label": label,
            "value": value if value is not None else "",
            "display": pref_display(field, value) if field in parts else str(value or "—"),
            "state": state,
            "kind": kind,
            "editable": editable,
            "source": source,
        }
        if note:
            row["note"] = note
        if extra:
            row.update(extra)
        rows.append(row)

    for field, label in RADIO_PREF_FIELDS + PROFILE_IDENTITY_FIELDS:
        if field == "name":
            add("name", label, value=parts.get("name"), kind="text", source="site" if bind else "node")
            continue
        if field == "lat":
            add(
                "lat",
                label,
                value=parts.get("lat"),
                kind="number",
                source="site" if bind else "node",
                note="Apply GPS" + (" (public fuzz)" if bind and config and config.location_accuracy_mi else ""),
            )
            continue
        if field == "lon":
            add("lon", label, value=parts.get("lon"), kind="number", source="site" if bind else "node")
            continue
        if field == "owner":
            has_override = "owner_info" in node
            add(
                "owner",
                label,
                value=owner_info_for_apply(node, doc, key=key, sites=sites),
                kind="textarea",
                editable=bind is not None,
                source="node_override" if has_override else "book",
                note="Book default" if not has_override and book_owner else None,
                extra={
                    "owner_override": has_override,
                    "owner_default": book_owner,
                },
            )
            continue
        if field == "acl":
            acl_note = "Edit nodes.yaml trust and keys.yaml. Password login is separate from this list."
            try:
                want = resolve_node_acl(doc or {}, node, keys or {})
            except UnknownPerson as exc:
                want = []
                acl_note = f"Unknown person in trust: {exc}. Fix the book before apply."
            acl_table = build_acl_table_rows(want, heard_acl, keys)
            add(
                "acl",
                label,
                value="",
                kind="acl_table",
                editable=False,
                note=acl_note,
                extra={
                    "display": "",
                    "acl_table": acl_table,
                    "acl_heard_at": acl_heard_at,
                },
            )
            continue
        if field == "admin":
            secret = node.get("admin_password")
            display = "***" if secret else "—"
            add(
                "admin",
                label,
                value=display,
                kind="password",
                extra={"has_value": bool(secret)},
            )
            if include_secrets and secret:
                rows[-1]["value"] = str(secret)
            continue
        if field == "guest":
            secret = node.get("guest_password") if "guest_password" in node else None
            open_guest = "guest_password" in node and node.get("guest_password") in (None, "")
            if open_guest:
                display = ""
            elif secret:
                display = "***"
            else:
                display = "—"
            add(
                "guest",
                label,
                value=display,
                kind="password",
                extra={"guest_open": open_guest, "has_value": bool(secret) and not open_guest},
            )
            if include_secrets:
                rows[-1]["value"] = "" if open_guest else (str(secret) if secret else "")
            continue
        if field not in parts or field not in applicable:
            continue
        kind = "bool" if field in ("repeat", "powersaving", "fem_rxgain", "rxgain") else "text"
        if field in ("dutycycle", "hop_retry", "hop_retry_ms", "agc_reset_interval", "advert", "flood", "path_hash"):
            kind = "number" if field != "path_hash" else "enum"
        if field == "ota_autofetch":
            kind = "enum"
        extra_enum = {"options": PROFILE_ENUM_OPTIONS[field]} if field in PROFILE_ENUM_OPTIONS else None
        add(field, label, value=parts[field], kind=kind, extra=extra_enum)

    rows.append(
        {
            "id": "full_sync_interval",
            "label": "Full sync interval",
            "value": node_full_sync_interval_label(node),
            "display": node_full_sync_interval_label(node),
            "state": "book",
            "kind": "interval",
            "editable": True,
            "source": "node",
        }
    )
    rows.append(
        {
            "id": "paused",
            "label": "Active (auto poll/apply)",
            "value": not is_paused(node),
            "display": "on" if not is_paused(node) else "off",
            "state": "book",
            "kind": "bool",
            "editable": True,
            "source": "node",
        }
    )
    return rows


def build_radio_prefs(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
    apply_desireds: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Header badges from profile rows."""
    rows = build_profile_rows(
        node,
        sites,
        doc=doc,
        keys=keys,
        key=key,
        apply_desireds=apply_desireds,
    )
    by_id = {r["id"]: r for r in rows}
    prefs: list[dict[str, str]] = []
    for field, label in RADIO_PREF_FIELDS:
        row = by_id.get(field)
        if not row or row.get("state") == "n/a":
            continue
        prefs.append(
            {
                "id": field,
                "label": label,
                "value": str(row.get("display") or row.get("value") or ""),
                "state": row.get("state") or "due",
            }
        )
    if not any(p["id"] == "rxgain" for p in prefs) and board_allows_rxgain(node):
        if desired_rxgain(node) is None:
            prefs.append(
                {
                    "id": "rxgain",
                    "label": "SX1262 boost",
                    "value": pref_display("rxgain", True),
                    "state": "default",
                }
            )
    order = {field: idx for idx, (field, _label) in enumerate(RADIO_PREF_FIELDS)}
    prefs.sort(key=lambda row: order.get(row["id"], len(RADIO_PREF_FIELDS)))
    return prefs
