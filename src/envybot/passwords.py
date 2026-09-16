"""Per-unit admin passwords (unique+strong). Guest login is open (blank) by default."""

from __future__ import annotations

import hashlib
import secrets
import string
from typing import Any

from envybot.nodes_doc import PLACEHOLDER_PW

PW_LEN = 14
PW_ALPHABET = string.ascii_letters + string.digits + "%&@#*^$!"
MIN_PW_LEN = 12
WEAK_PASSWORDS = frozenset({
    "m35h3nvy",
    "m35h3nvyc00kie",
    "password",
    "hello",
})


def normalize_password(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def password_is_strong(value: Any) -> bool:
    pw = normalize_password(value)
    if len(pw) < MIN_PW_LEN:
        return False
    if pw in PLACEHOLDER_PW:
        return False
    if pw.casefold() in WEAK_PASSWORDS:
        return False
    return True


_PW_FIELDS = ("admin_password", "guest_password")


def book_passwords(
    doc: dict[str, Any],
    *,
    exclude: set[tuple[str, str]] | None = None,
) -> set[str]:
    skip = exclude or set()
    out: set[str] = set()
    nodes = doc.get("nodes") or {}
    if not isinstance(nodes, dict):
        return out
    for other_key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for field in _PW_FIELDS:
            if (str(other_key), field) in skip:
                continue
            pw = normalize_password(node.get(field))
            if pw:
                out.add(pw)
    return out


def password_collides(
    value: Any,
    doc: dict[str, Any],
    key: str,
    *,
    field: str,
) -> bool:
    pw = normalize_password(value)
    if not pw:
        return False
    return pw in book_passwords(doc, exclude={(key, field)})


def desired_guest_password(node: dict[str, Any]) -> str:
    if "guest_password" not in node:
        return ""
    return normalize_password(node.get("guest_password"))


def guest_needs_assign(node: dict[str, Any], doc: dict[str, Any], key: str) -> bool:
    return False


def gen_password(length: int = PW_LEN) -> str:
    chars = [secrets.choice(string.ascii_letters)]
    chars.extend(secrets.choice(PW_ALPHABET) for _ in range(length - 1))
    return "".join(chars)


def gen_unique_password(
    doc: dict[str, Any],
    key: str,
    *,
    field: str,
    length: int = PW_LEN,
) -> str:
    taken = book_passwords(doc, exclude={(key, field)})
    for _ in range(64):
        pw = gen_password(length)
        if pw not in taken and password_is_strong(pw):
            return pw
    raise RuntimeError("could not generate a unique password")


def resolve_guest_password(node: dict[str, Any], doc: dict[str, Any], key: str) -> str:
    """Book desired guest password. Blank = open login."""
    desired = desired_guest_password(node)
    if desired == "":
        return ""
    if password_is_strong(desired) and not password_collides(
        desired, doc, key, field="guest_password"
    ):
        return desired
    return ""


def password_token(value: Any) -> str:
    """Stable short digest for profile_id. Weak/blank → none (not hashed)."""
    if not password_is_strong(value):
        return "none"
    digest = hashlib.sha256(normalize_password(value).encode()).hexdigest()
    return digest[:8]


def guest_profile_token(node: dict[str, Any]) -> str:
    return password_token(node.get("guest_password"))
