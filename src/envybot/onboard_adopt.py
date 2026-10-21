"""Pull blank book fields from USB before reonboard push / flash."""

from __future__ import annotations

from typing import Any

from envybot.keys_doc import (
    PERM_ACL_ADMIN,
    PERM_ACL_RO,
    UnknownPerson,
    find_person_for_pubkey,
    node_role_names,
    parse_serial_acl,
    resolve_node_acl,
)
from envybot.onboard_book import (
    PUB_HEX_LEN,
    PRV_HEX_LEN,
    normalize_hex,
    stored_admin_password,
)
from envybot.radio import parse_get_value
from envybot.repeater_serial import CliError, RepeaterSerial


def verify_reonboard_usb_pubkey(node: dict[str, Any], usb_pubkey: str) -> None:
    book_pub = str(node.get("identity_pubkey") or "").strip().lower()
    want = usb_pubkey.strip().lower()
    if book_pub and book_pub != want:
        raise CliError(
            f"USB pubkey {want[:8]}… != book {book_pub[:8]}… (wrong unit on USB?)"
        )


def adopt_trust_from_heard_acl(
    doc: dict[str, Any],
    node: dict[str, Any],
    keys: dict[str, list[str]],
    heard: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Merge heard ACL people into a full node ``trust:`` override. Returns (changed, unknown_prefixes)."""
    try:
        grants = resolve_node_acl(doc, node, keys)
    except UnknownPerson:
        grants = []
    covered: set[str] = set()
    for grant in grants:
        covered.add(grant.pubkey[:12])

    extra_admin: list[str] = []
    extra_guest: list[str] = []
    unknown: list[str] = []

    for entry in heard:
        pk = str(entry.get("key") or entry.get("pubkey") or "").strip().lower()
        if len(pk) < 12:
            continue
        prefix = pk[:12]
        if prefix in covered:
            continue
        if any(g.pubkey.startswith(prefix) or prefix.startswith(g.pubkey[:12]) for g in grants):
            covered.add(prefix)
            continue
        person = find_person_for_pubkey(keys, pk)
        if not person:
            unknown.append(prefix)
            continue
        perm = entry.get("perm")
        if perm == PERM_ACL_ADMIN:
            if person not in extra_admin:
                extra_admin.append(person)
        elif perm == PERM_ACL_RO:
            if person not in extra_guest:
                extra_guest.append(person)
        covered.add(prefix)

    if not extra_admin and not extra_guest:
        return False, unknown

    admins = list(dict.fromkeys(node_role_names(doc, node, "admin") + extra_admin))
    guests = list(dict.fromkeys(node_role_names(doc, node, "guest") + extra_guest))
    node["trust"] = {"admin": admins, "guest": guests}
    return True, unknown


def adopt_radio_blanks(
    cli: RepeaterSerial,
    doc: dict[str, Any],
    node: dict[str, Any],
    keys: dict[str, list[str]],
    *,
    usb_pubkey: str,
) -> list[str]:
    """Fill blank yaml fields from USB. Returns adopted field names (no secrets)."""
    adopted: list[str] = []

    book_pub = str(node.get("identity_pubkey") or "").strip().lower()
    if not book_pub:
        node["identity_pubkey"] = usb_pubkey
        adopted.append("identity_pubkey")
    else:
        verify_reonboard_usb_pubkey(node, usb_pubkey)

    if not str(node.get("identity_secret") or "").strip():
        secret = normalize_hex(
            parse_get_value(cli.cmd("get prv.key")) or "",
            PRV_HEX_LEN,
            "prv.key",
        )
        pub_after = normalize_hex(
            parse_get_value(cli.cmd("get public.key")) or "",
            PUB_HEX_LEN,
            "public.key",
        )
        if pub_after != usb_pubkey:
            raise CliError("public.key changed after reading prv.key")
        node["identity_secret"] = secret
        adopted.append("identity_secret")

    if stored_admin_password(node):
        pass
    elif str(node.get("admin_password") or "").strip():
        pass

    if "guest_password" not in node:
        guest = parse_get_value(cli.cmd("get guest.password")) or ""
        node["guest_password"] = guest
        adopted.append("guest_password")

    raw_acl = cli.cmd("get acl")
    heard = parse_serial_acl(raw_acl)
    trust_changed, unknown = adopt_trust_from_heard_acl(doc, node, keys, heard)
    if trust_changed:
        adopted.append("trust")
    for prefix in unknown:
        print(f"   acl heard unknown pubkey {prefix}… (not in keys.yaml)")

    return adopted
