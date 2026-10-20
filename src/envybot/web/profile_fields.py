"""Fleet UI profile rows: book desired vs on-radio confirmation."""

from __future__ import annotations

from typing import Any

from envybot.apply import (
    applicable_field_desireds,
    board_allows_rxgain,
    desired_rxgain,
    profile_parts,
)
from envybot.keys_doc import UnknownPerson, resolve_node_acl
from envybot.nodes_doc import is_paused
from envybot.position import site_binding
from envybot.public_advert import load_public_advert_config, owner_info_for_apply
from envybot.profile_display import pref_display
from envybot.radio_confirmation import (
    OnRadio,
    confirmations_for_field,
    password_on_radio,
    profile_row_state,
    resolve_on_radio,
    row_needs_attention,
)
from envybot.web.acl_view import build_acl_table_rows

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


def _book_display(field: str, value: Any, parts: dict[str, Any]) -> str:
    if field in parts:
        return pref_display(field, value)
    return str(value or "—")


def _acl_in_sync(acl_table: list[dict[str, Any]] | None) -> bool:
    if not acl_table:
        return True
    return all(r.get("status") == "good" for r in acl_table)


def _attach_confirmation(
    row: dict[str, Any],
    field: str,
    *,
    applicable: dict[str, str],
    stamped: dict[str, str],
    times: dict[str, int],
    seen_row: dict[str, Any],
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    key: str | None,
    on_radio: OnRadio | None = None,
    acl_table: list[dict[str, Any]] | None = None,
) -> None:
    if on_radio is None:
        events = confirmations_for_field(
            field,
            apply_desired=stamped.get(field),
            apply_at=times.get(field),
            seen=seen_row,
        )
        on_radio = resolve_on_radio(field, events)
    row["on_radio"] = on_radio.to_dict()
    state = profile_row_state(
        field,
        applicable=field in applicable,
        book_desired=applicable.get(field),
        on_radio=on_radio,
        node=node,
        doc=doc,
        keys=keys,
        sites=sites,
        unit=key,
        acl_in_sync=_acl_in_sync(acl_table) if field == "acl" else None,
    )
    row["state"] = state
    row["attention"] = row_needs_attention(state, None)


def build_profile_rows(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    key: str | None = None,
    apply_desireds: dict[str, str] | None = None,
    apply_times: dict[str, int] | None = None,
    seen: dict[str, Any] | None = None,
    include_secrets: bool = False,
    heard_acl: list[dict[str, Any]] | None = None,
    acl_heard_at: int | None = None,
) -> list[dict[str, Any]]:
    parts = profile_parts(node, sites, doc=doc, keys=keys, key=key)
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=key)
    stamped = apply_desireds or {}
    times = apply_times or {}
    seen_row = dict(seen or {})
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
        on_radio: OnRadio | None = None,
        acl_table: list[dict[str, Any]] | None = None,
    ) -> None:
        display = _book_display(field, value, parts)
        row: dict[str, Any] = {
            "id": field,
            "label": label,
            "book": {"value": value if value is not None else "", "display": display},
            "kind": kind,
            "editable": editable,
            "source": source,
        }
        if note:
            row["note"] = note
        if extra:
            row.update(extra)
        _attach_confirmation(
            row,
            field,
            applicable=applicable,
            stamped=stamped,
            times=times,
            seen_row=seen_row,
            node=node,
            sites=sites,
            doc=doc,
            keys=keys,
            key=key,
            on_radio=on_radio,
            acl_table=acl_table,
        )
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
            if acl_heard_at is not None:
                seen_row["acl_at"] = acl_heard_at
            events = confirmations_for_field(
                "acl",
                apply_desired=stamped.get("acl"),
                apply_at=times.get("acl"),
                seen=seen_row,
            )
            add(
                "acl",
                label,
                value="",
                kind="acl_table",
                editable=False,
                note=acl_note,
                on_radio=resolve_on_radio("acl", events),
                acl_table=acl_table,
                extra={
                    "acl_table": acl_table,
                    "acl_heard_at": acl_heard_at,
                },
            )
            continue
        if field == "admin":
            secret = node.get("admin_password")
            display = "***" if secret else "—"
            book_val = str(secret) if include_secrets and secret else display
            add(
                "admin",
                label,
                value=book_val,
                kind="password",
                on_radio=password_on_radio(
                    "admin",
                    apply_desired=stamped.get("admin"),
                    apply_at=times.get("admin"),
                ),
                extra={"has_value": bool(secret)},
            )
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
            if include_secrets:
                book_val: Any = "" if open_guest else (str(secret) if secret else "")
            else:
                book_val = display
            add(
                "guest",
                label,
                value=book_val,
                kind="password",
                on_radio=password_on_radio(
                    "guest",
                    apply_desired=stamped.get("guest"),
                    apply_at=times.get("guest"),
                    guest_open=open_guest,
                ),
                extra={"guest_open": open_guest, "has_value": bool(secret) and not open_guest},
            )
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
            "id": "paused",
            "label": "Active (auto poll/apply)",
            "book": {
                "value": not is_paused(node),
                "display": "on" if not is_paused(node) else "off",
            },
            "on_radio": {"confirmed": False, "display": "Not confirmed"},
            "state": "book",
            "attention": False,
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
    apply_times: dict[str, int] | None = None,
    seen: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Header badges from profile rows (book value + confirmation state)."""
    rows = build_profile_rows(
        node,
        sites,
        doc=doc,
        keys=keys,
        key=key,
        apply_desireds=apply_desireds,
        apply_times=apply_times,
        seen=seen,
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
                "value": str(row.get("book", {}).get("display") or ""),
                "state": str(row.get("state") or "unconfirmed"),
                "attention": str(bool(row.get("attention"))).lower(),
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
                    "attention": "false",
                }
            )
    order = {field: idx for idx, (field, _label) in enumerate(RADIO_PREF_FIELDS)}
    prefs.sort(key=lambda row: order.get(row["id"], len(RADIO_PREF_FIELDS)))
    return prefs


def profile_has_attention(rows: list[dict[str, Any]] | None) -> bool:
    if not rows:
        return False
    return any(bool(r.get("attention")) for r in rows if r.get("id") != "paused")
