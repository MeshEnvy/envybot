"""Compare on-radio bootloader/firmware labels to EnvyOS bench zip names."""

from __future__ import annotations

import re
from pathlib import Path

BENCH_EV_RE = re.compile(
    r"^(?P<base>[0-9]+(?:\.[0-9]+)*)(?:-ev(?P<ev>[0-9]+))?$",
    re.I,
)
BOOTLOADER_ZIP_RE = re.compile(r"_bootloader-(.+?)\.recovery\.zip$", re.I)


def bench_version_tuple(label: str | None) -> tuple[int, ...] | None:
    """``0.9.2-ev2`` → ``(0, 9, 2, 2)``; ``1.18.0.3`` → ``(1, 18, 0, 3)``."""
    if not label:
        return None
    text = str(label).strip().lstrip("v")
    if not text:
        return None
    m = BENCH_EV_RE.match(text.split()[0])
    if not m:
        return None
    parts = tuple(int(x) for x in m.group("base").split("."))
    ev = m.group("ev")
    if ev is not None:
        return parts + (int(ev),)
    return parts


def version_tuple_at_least(have: tuple[int, ...], need: tuple[int, ...]) -> bool:
    width = max(len(have), len(need))
    padded_have = have + (0,) * (width - len(have))
    padded_need = need + (0,) * (width - len(need))
    return padded_have >= padded_need


def device_meets_bench(device_label: str | None, bench_label: str) -> bool:
    """True when the radio reports a version at or above the bench artifact label."""
    need = bench_version_tuple(bench_label)
    if need is None:
        return True
    have = bench_version_tuple(device_label)
    if have is None:
        return False
    return version_tuple_at_least(have, need)


def bootloader_label_from_zip(path: Path) -> str | None:
    m = BOOTLOADER_ZIP_RE.search(path.name)
    return m.group(1) if m else None


def firmware_label_from_zip(path: Path, *, firmware_prefix: str) -> str | None:
    name = path.name
    prefix = firmware_prefix
    if not name.lower().startswith(prefix.lower()):
        return None
    rest = name[len(prefix) :]
    if rest.lower().endswith(".zip"):
        return rest[:-4]
    return None


def firmware_stamp_for_compare(label: str) -> str:
    """``1.18.0-ev3`` → ``1.18.0.3`` (matches on-air ``ver`` stamp)."""
    text = label.strip().lstrip("v")
    m = re.match(r"^([0-9]+\.[0-9]+\.[0-9]+)-ev([0-9]+)$", text, re.I)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return text
