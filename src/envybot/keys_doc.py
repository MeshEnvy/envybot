"""Book keys.yaml plus nodes.yaml trust roles. MeshCore ACL only."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PERSON_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ACL_LINE_RE = re.compile(
    r"(?:^|\s)([0-9a-fA-F]{2})\s+([0-9a-fA-F]{16,64})\s*$", re.M
)

PERM_ACL_DROP = 0
PERM_ACL_RO = 1
PERM_ACL_ADMIN = 3
ROLES = frozenset({"admin", "guest"})
ROLE_PERM = {"admin": PERM_ACL_ADMIN, "guest": PERM_ACL_RO}

KEYS_NAME = "keys.yaml"
KEYS_YAML_HEADER = (
    "# MeshEnvy companion identities (private).\n"
    "# person: list of 64-hex MeshCore pubkeys.\n"
    "# nodes.yaml trust.admin / trust.guest name these people.\n"
    "# admin1_* on a node is Meshtastic. Do not put those keys here.\n"
)


class UnknownPerson(ValueError):
    """trust role names a person that is not in keys.yaml."""


class TrustError(ValueError):
    """CLI / policy parse error."""


@dataclass(frozen=True)
class AclGrant:
    pubkey: str
    perm: int
    person: str
    role: str


@dataclass(frozen=True)
class AclOp:
    key: str
    perm: int
    label: str


@dataclass(frozen=True)
class TrustPolicy:
    person: str
    fleet_role: str
    overrides: tuple[tuple[str, str], ...]  # (selector, role)


def keys_path(book_or_nodes: Path) -> Path:
    path = Path(book_or_nodes)
    if path.is_file() and path.name == "nodes.yaml":
        return path.parent / KEYS_NAME
    return path / KEYS_NAME


def load_keys(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    yaml = YAML()
    raw = yaml.load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for name, items in raw.items():
        person = str(name).strip().lower()
        if not PERSON_RE.match(person):
            continue
        out[person] = _parse_key_list(items)
    return out


def write_keys(path: Path, keys: dict[str, list[str]]) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    ordered: dict[str, list[str]] = {}
    for name in sorted(keys):
        ordered[name] = list(keys[name])
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(KEYS_YAML_HEADER)
        yaml.dump(ordered, fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(path)


def _parse_key_list(items: Any) -> list[str]:
    raw: list[Any]
    if isinstance(items, list):
        raw = items
    elif isinstance(items, dict):
        raw = items.get("keys") or items.get("pubkeys") or []
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            pk = item.strip().lower()
        elif isinstance(item, dict):
            pk = str(item.get("pubkey") or item.get("identity_pubkey") or "").strip().lower()
        else:
            continue
        if HEX_PUBKEY_RE.match(pk) and pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


def person_keys(keys: dict[str, list[str]], name: str) -> list[str]:
    return list(keys.get(name.strip().lower()) or [])


def person_has_key(keys: dict[str, list[str]], name: str, pubkey: str) -> bool:
    want = pubkey.strip().lower()
    if len(want) < 12:
        return False
    prefix = want[:12]
    for pk in person_keys(keys, name):
        if pk.startswith(prefix) or want.startswith(pk[:12]):
            return True
    return False


def find_person_for_pubkey(keys: dict[str, list[str]], pubkey: str) -> str | None:
    """First keys.yaml person whose pubkey list matches this companion."""
    want = pubkey.strip().lower()
    if len(want) < 12:
        return None
    for person in sorted(keys):
        if person_has_key(keys, person, want):
            return person
    return None


def remember_person_key(keys: dict[str, list[str]], name: str, pubkey: str) -> bool:
    person = name.strip().lower()
    if not PERSON_RE.match(person):
        raise TrustError(f"invalid person name {name!r}")
    pk = pubkey.strip().lower()
    if not HEX_PUBKEY_RE.match(pk):
        raise TrustError("companion pubkey must be 64 hex chars")
    have = keys.setdefault(person, [])
    if pk in have:
        return False
    have.append(pk)
    return True


def _name_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip().lower()
        if not PERSON_RE.match(name) or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def book_role_names(doc: dict[str, Any], role: str) -> list[str]:
    trust = doc.get("trust")
    if not isinstance(trust, dict):
        return []
    return _name_list(trust.get(role))


def node_role_names(doc: dict[str, Any], node: dict[str, Any] | None, role: str) -> list[str]:
    """Book default unless the node sets this role key (including [])."""
    trust = (node or {}).get("trust") if isinstance(node, dict) else None
    if isinstance(trust, dict) and role in trust:
        return _name_list(trust.get(role))
    return book_role_names(doc, role)


def resolve_node_acl(
    doc: dict[str, Any],
    node: dict[str, Any] | None,
    keys: dict[str, list[str]],
) -> list[AclGrant]:
    """Admin first, then guest. Same key in both roles keeps admin."""
    seen: set[str] = set()
    out: list[AclGrant] = []
    for role in ("admin", "guest"):
        for person in node_role_names(doc, node, role):
            pks = person_keys(keys, person)
            if not pks and person not in keys:
                raise UnknownPerson(person)
            for pk in pks:
                if pk in seen:
                    continue
                seen.add(pk)
                out.append(AclGrant(pubkey=pk, perm=ROLE_PERM[role], person=person, role=role))
    return out


def companion_in_desired_acl(
    doc: dict[str, Any],
    node: dict[str, Any],
    companion: str | None,
    keys: dict[str, list[str]] | None = None,
) -> bool:
    """True when the live companion is a resolved **admin** key."""
    if not companion:
        return False
    want = companion.strip().lower()
    if len(want) < 12:
        return False
    prefix = want[:12]
    try:
        grants = resolve_node_acl(doc, node, keys or {})
    except UnknownPerson:
        return False
    for grant in grants:
        if grant.perm != PERM_ACL_ADMIN:
            continue
        if grant.pubkey.startswith(prefix) or want.startswith(grant.pubkey[:12]):
            return True
    return False


def grants_payload(grants: list[AclGrant]) -> list[dict[str, Any]]:
    return [{"perm": g.perm, "pubkey": g.pubkey} for g in grants]


def _heard_key(entry: dict[str, Any]) -> str:
    return str(entry.get("key") or entry.get("pubkey") or "").strip().lower()


def _heard_perm(entry: dict[str, Any]) -> int | None:
    val = entry.get("perm", entry.get("permissions"))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def plan_acl_ops(
    want: list[AclGrant],
    heard: list[dict[str, Any]] | None,
) -> list[AclOp]:
    """setperm grants, then drop heard keys that are not wanted."""
    ops: list[AclOp] = []
    heard_by_prefix: dict[str, int | None] = {}
    if heard is not None:
        for entry in heard:
            key = _heard_key(entry)
            if len(key) < 12:
                continue
            heard_by_prefix[key[:12]] = _heard_perm(entry)
    want_prefixes: set[str] = set()
    for grant in want:
        want_prefixes.add(grant.pubkey[:12])
        have = heard_by_prefix.get(grant.pubkey[:12])
        if have == grant.perm:
            continue
        ops.append(
            AclOp(
                key=grant.pubkey,
                perm=grant.perm,
                label=f"acl {grant.pubkey[:8]} {grant.role}",
            )
        )
    if heard is None:
        return ops
    for entry in heard:
        key = _heard_key(entry)
        if not key:
            continue
        prefix = key[:12] if len(key) >= 12 else key
        if prefix in want_prefixes:
            continue
        ops.append(AclOp(key=key, perm=PERM_ACL_DROP, label=f"acl drop {prefix}"))
    return ops


def parse_serial_acl(text: str | None) -> list[dict[str, Any]]:
    """MeshCore USB ``get acl`` lines: ``<perm-hex> <pubkey-hex>``."""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for match in ACL_LINE_RE.finditer(text):
        perm = int(match.group(1), 16)
        pk = match.group(2).lower()
        if perm == PERM_ACL_DROP:
            continue
        out.append({"key": pk, "perm": perm})
    return out


def parse_trust_token(token: str) -> tuple[str, str] | None:
    if ":" not in token:
        return None
    name, role = token.rsplit(":", 1)
    role = role.strip().lower()
    name = name.strip()
    if not name or role not in ROLES:
        return None
    return name, role


def parse_trust_policy(tokens: list[str]) -> TrustPolicy:
    if not tokens:
        raise TrustError("person name required")
    first = tokens[0]
    parsed = parse_trust_token(first)
    if parsed:
        person, fleet_role = parsed
    else:
        person, fleet_role = first.strip().lower(), "admin"
    if not PERSON_RE.match(person):
        raise TrustError(f"invalid person name {first!r}")
    overrides: list[tuple[str, str]] = []
    for token in tokens[1:]:
        item = parse_trust_token(token)
        if item is None:
            raise TrustError(f"expected selector:role, got {token!r}")
        selector, role = item
        overrides.append((selector, role))
    return TrustPolicy(person=person, fleet_role=fleet_role, overrides=tuple(overrides))


def _ensure_book_trust(doc: dict[str, Any]) -> dict[str, Any]:
    trust = doc.get("trust")
    if not isinstance(trust, dict):
        trust = {}
        doc["trust"] = trust
    return trust


def add_book_role(doc: dict[str, Any], person: str, role: str) -> bool:
    trust = _ensure_book_trust(doc)
    other = "guest" if role == "admin" else "admin"
    other_list = [n for n in _name_list(trust.get(other)) if n != person]
    trust[other] = other_list
    have = _name_list(trust.get(role))
    if person in have:
        trust[role] = have
        return False
    have.append(person)
    trust[role] = have
    return True


def apply_node_override(
    doc: dict[str, Any],
    node: dict[str, Any],
    person: str,
    role: str,
) -> bool:
    """Write a full admin+guest pair so inherit does not keep both roles."""
    book_admin = book_role_names(doc, "admin")
    book_guest = book_role_names(doc, "guest")
    cur_admin = list(node_role_names(doc, node, "admin"))
    cur_guest = list(node_role_names(doc, node, "guest"))
    if role == "guest":
        new_admin = [n for n in cur_admin if n != person]
        new_guest = cur_guest if person in cur_guest else [*cur_guest, person]
    else:
        new_guest = [n for n in cur_guest if n != person]
        new_admin = cur_admin if person in cur_admin else [*cur_admin, person]
    if new_admin == book_admin and new_guest == book_guest:
        if "trust" in node:
            node.pop("trust", None)
            return True
        return False
    node["trust"] = {"admin": new_admin, "guest": new_guest}
    return True
