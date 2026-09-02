"""Book channels.yaml — group channel catalog and companion slot planner."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from envybot.keys_doc import PERSON_RE, TrustError

CHANNELS_NAME = "channels.yaml"
CHANNELS_YAML_HEADER = (
    "# MeshEnvy group channels (private).\n"
    "# name: {key?: 32-hex PSK, people: everyone | [person, …]}\n"
    "# public is ignored (stock MeshCore Public is never applied).\n"
    "# people: everyone grants every companion. Named people match keys.yaml.\n"
)

HEX_PSK_RE = re.compile(r"^[0-9a-fA-F]{32}$")
CHANNEL_NAME_RE = re.compile(r"^[\x20-\x7E]{1,31}$")
EVERYONE = "everyone"
PUBLIC_YAML_NAME = "public"
PUBLIC_FIRMWARE_NAME = "Public"
PUBLIC_GROUP_PSK_B64 = "izOH6cXN6mrJ5e26oRXNcg=="
PUBLIC_GROUP_PSK = base64.b64decode(PUBLIC_GROUP_PSK_B64)
DEFAULT_MAX_CHANNELS = 40


class ChannelsError(ValueError):
    """channels.yaml parse or planner error."""


@dataclass(frozen=True)
class ChannelDef:
    yaml_name: str
    firmware_name: str
    secret: bytes
    is_public: bool


@dataclass(frozen=True)
class ChannelSlot:
    idx: int
    name: str
    secret: bytes


@dataclass(frozen=True)
class ChannelOp:
    idx: int
    name: str
    secret: bytes
    label: str


def channels_path(book_or_nodes: Path) -> Path:
    path = Path(book_or_nodes)
    if path.is_file() and path.name == "nodes.yaml":
        return path.parent / CHANNELS_NAME
    return path / CHANNELS_NAME


def _parse_people(raw: Any, channel_name: str) -> frozenset[str] | str:
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == EVERYONE:
            return EVERYONE
        if PERSON_RE.match(token):
            return frozenset({token})
        raise ChannelsError(f"{channel_name}: invalid people {raw!r}")
    if isinstance(raw, list):
        out: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                raise ChannelsError(f"{channel_name}: people list must be strings")
            person = item.strip().lower()
            if not PERSON_RE.match(person):
                raise ChannelsError(f"{channel_name}: invalid person {item!r}")
            out.add(person)
        if not out:
            raise ChannelsError(f"{channel_name}: people list is empty")
        return frozenset(out)
    raise ChannelsError(f"{channel_name}: people is required (everyone or list)")


def _parse_channel(name: str, raw: Any) -> tuple[frozenset[str] | str, ChannelDef]:
    yaml_name = name.strip()
    if not CHANNEL_NAME_RE.match(yaml_name):
        raise ChannelsError(f"invalid channel name {name!r}")
    if not isinstance(raw, dict):
        raise ChannelsError(f"{yaml_name}: expected mapping")
    people = _parse_people(raw.get("people"), yaml_name)
    is_public = yaml_name.lower() == PUBLIC_YAML_NAME
    if is_public:
        return people, ChannelDef(
            yaml_name=yaml_name,
            firmware_name=PUBLIC_FIRMWARE_NAME,
            secret=PUBLIC_GROUP_PSK,
            is_public=True,
        )
    key_raw = raw.get("key")
    if not isinstance(key_raw, str):
        raise ChannelsError(f"{yaml_name}: key is required (32 hex)")
    key = key_raw.strip().lower()
    if not HEX_PSK_RE.match(key):
        raise ChannelsError(f"{yaml_name}: key must be 32 hex chars")
    return people, ChannelDef(
        yaml_name=yaml_name,
        firmware_name=yaml_name,
        secret=bytes.fromhex(key),
        is_public=False,
    )


def load_channels(path: Path) -> dict[str, tuple[frozenset[str] | str, ChannelDef]]:
    if not path.is_file():
        return {}
    yaml = YAML()
    raw = yaml.load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ChannelsError(f"{path}: root must be a mapping")
    out: dict[str, tuple[frozenset[str] | str, ChannelDef]] = {}
    for name, body in raw.items():
        yaml_key = str(name).strip()
        people, ch = _parse_channel(yaml_key, body)
        out[ch.yaml_name] = (people, ch)
    return out


def resolve_person_channels(
    catalog: dict[str, tuple[frozenset[str] | str, ChannelDef]],
    person: str | None,
) -> list[ChannelDef]:
    """Channels granted to person (or everyone-only when person is None)."""
    want: list[ChannelDef] = []
    for _people, ch in catalog.values():
        if ch.is_public:
            continue
        if _people == EVERYONE:
            want.append(ch)
            continue
        if person and person in _people:
            want.append(ch)
    return want


def _slot_name(raw: str) -> str:
    return (raw or "").strip("\x00 ").strip()


def _secret_hex(secret: bytes) -> str:
    return secret.hex()


def slot_is_empty(slot: ChannelSlot) -> bool:
    return not _slot_name(slot.name) and all(b == 0 for b in slot.secret)


def names_match(slot_name: str, firmware_name: str, *, is_public: bool) -> bool:
    left = _slot_name(slot_name)
    right = firmware_name.strip()
    if is_public:
        return left.lower() == right.lower()
    return left == right


def secrets_match(have: bytes, want: bytes) -> bool:
    return have[:16] == want[:16]


def find_first_empty(
    heard: list[ChannelSlot],
    *,
    used: set[int],
    allow_slot_0: bool,
) -> int | None:
    for slot in heard:
        if slot.idx in used:
            continue
        if not allow_slot_0 and slot.idx == 0:
            continue
        if slot_is_empty(slot):
            return slot.idx
    return None


def plan_channel_ops(
    want: list[ChannelDef],
    heard: list[ChannelSlot],
) -> tuple[list[ChannelOp], list[str]]:
    """Add/update only. Extras on the tag are left alone. Public is never SET."""
    ops: list[ChannelOp] = []
    failures: list[str] = []
    used: set[int] = set()

    for ch in want:
        if ch.is_public:
            continue
        existing: ChannelSlot | None = None
        for slot in heard:
            if names_match(slot.name, ch.firmware_name, is_public=ch.is_public):
                existing = slot
                break

        if existing is not None:
            if secrets_match(existing.secret, ch.secret):
                continue
            if ch.is_public and existing.idx == 0 and not slot_is_empty(existing):
                continue
            ops.append(
                ChannelOp(
                    idx=existing.idx,
                    name=ch.firmware_name,
                    secret=ch.secret,
                    label=f"channel {ch.yaml_name}",
                )
            )
            used.add(existing.idx)
            continue

        empty_idx = find_first_empty(
            heard,
            used=used,
            allow_slot_0=ch.is_public,
        )
        if empty_idx is None:
            failures.append(ch.yaml_name)
            continue
        ops.append(
            ChannelOp(
                idx=empty_idx,
                name=ch.firmware_name,
                secret=ch.secret,
                label=f"channel {ch.yaml_name}",
            )
        )
        used.add(empty_idx)

    return ops, failures


def heard_slot_from_payload(idx: int, payload: dict[str, Any]) -> ChannelSlot:
    secret = payload.get("channel_secret")
    if isinstance(secret, str):
        secret_bytes = bytes.fromhex(secret) if secret else b""
    elif isinstance(secret, (bytes, bytearray)):
        secret_bytes = bytes(secret)
    else:
        secret_bytes = b""
    if len(secret_bytes) < 16:
        secret_bytes = secret_bytes.ljust(16, b"\x00")
    else:
        secret_bytes = secret_bytes[:16]
    return ChannelSlot(
        idx=idx,
        name=str(payload.get("channel_name") or ""),
        secret=secret_bytes,
    )
