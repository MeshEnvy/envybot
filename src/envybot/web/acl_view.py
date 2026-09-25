"""Fleet UI: book ACL want vs last heard radio ACL."""

from __future__ import annotations

from typing import Any

from envybot.keys_doc import (
    PERM_ACL_ADMIN,
    PERM_ACL_RO,
    AclGrant,
    find_person_for_pubkey,
)


def _heard_key(entry: dict[str, Any]) -> str:
    return str(entry.get("key") or entry.get("pubkey") or "").strip().lower()


def _heard_perm(entry: dict[str, Any]) -> int | None:
    val = entry.get("perm", entry.get("permissions"))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def perm_to_role(perm: int | None) -> str:
    if perm == PERM_ACL_ADMIN:
        return "admin"
    if perm == PERM_ACL_RO:
        return "guest"
    return "?"


def _heard_by_prefix(heard: list[dict[str, Any]] | None) -> dict[str, tuple[str, int | None]]:
    """Map 12-hex prefix → (full pubkey, perm)."""
    out: dict[str, tuple[str, int | None]] = {}
    if not heard:
        return out
    for entry in heard:
        key = _heard_key(entry)
        if len(key) < 12:
            continue
        prefix = key[:12]
        out[prefix] = (key, _heard_perm(entry))
    return out


def build_acl_table_rows(
    want: list[AclGrant],
    heard: list[dict[str, Any]] | None,
    keys: dict[str, list[str]] | None,
) -> list[dict[str, Any]]:
    """One row per wanted grant plus extras on radio not in want."""
    heard_map = _heard_by_prefix(heard)
    want_prefixes: set[str] = set()
    rows: list[dict[str, Any]] = []

    for grant in want:
        pk = grant.pubkey.strip().lower()
        prefix12 = pk[:12] if len(pk) >= 12 else pk
        want_prefixes.add(prefix12)
        heard_entry = heard_map.get(prefix12)
        if heard is None:
            status = "dirty"
            perm_heard = None
        elif heard_entry is None:
            status = "dirty"
            perm_heard = None
        elif heard_entry[1] == grant.perm:
            status = "good"
            perm_heard = heard_entry[1]
        else:
            status = "dirty"
            perm_heard = heard_entry[1]
        rows.append(
            {
                "prefix4": pk[:4] if len(pk) >= 4 else pk,
                "pubkey": pk,
                "role": grant.role,
                "person": grant.person or "nobody",
                "status": status,
                "perm_want": grant.perm,
                "perm_heard": perm_heard,
            }
        )

    if heard is not None:
        for prefix12, (pk, perm) in heard_map.items():
            if prefix12 in want_prefixes:
                continue
            person = find_person_for_pubkey(keys or {}, pk)
            rows.append(
                {
                    "prefix4": pk[:4] if len(pk) >= 4 else pk,
                    "pubkey": pk,
                    "role": perm_to_role(perm),
                    "person": person or "nobody",
                    "status": "bad",
                    "perm_want": None,
                    "perm_heard": perm,
                }
            )

    rows.sort(key=lambda r: (r["status"] != "bad", r["role"], r["prefix4"]))
    return rows
