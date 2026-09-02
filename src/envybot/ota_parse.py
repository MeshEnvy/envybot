"""Parse MeshCore `ota status` and `ota ls` CLI replies for fleet visibility."""

from __future__ import annotations

import re
from typing import Any

OTA_STATUS_PREFIX = "OTA |"
OTA_LS_HEADER = "Updates nearby"
OTA_LS_EMPTY = "No updates seen yet"

OTA_LS_ROW_RE = re.compile(
    r"^\s*(\d+)\)\s+(\S+)\s+(\S+)\s+\[([^\]]+)\]\s+(\d+)n\s+(\d+)s"
    r"(?:\s+\[(ready|paused|failed|downloading)\])?",
    re.MULTILINE,
)

DOWNLOAD_RE = re.compile(
    r"download:\s+(.+?)\s+(\d+)/(\d+)\s+\((\d+)%\)\s+id=([0-9A-Fa-f]+)\s+(\d+)s"
)
TARGET_RE = re.compile(r"target:([0-9A-Fa-f]{8})\s+\(([^)]+)\)")
THIS_FW_RE = re.compile(r"this fw ([0-9A-Fa-f?]+) \((\d+)K\)")
HW_RE = re.compile(r"hw=(\S+)")
BL_RE = re.compile(r"\|\s*bl:(apply|NONE)(?:\s+blrc:([0-9A-Fa-f]{2}))?")
SERVING_RE = re.compile(r"serving:(\w+)\s+\((\d+)\)")
KEYS_RE = re.compile(r"keys:(\d+)")
SEED_RE = re.compile(r"\|\s*seed:(on|off)")


def ota_ls_heard_empty(text: str) -> bool:
    """True when the radio answered but the catalog is not populated yet."""
    if not text:
        return False
    stripped = text.strip()
    lower = stripped.lower()
    if lower == "unknown command":
        return True
    if "unknown ota command" in lower:
        return True
    if stripped.startswith("ERR"):
        return True
    if OTA_LS_EMPTY in stripped:
        return True
    return False


def ota_status_heard_empty(text: str) -> bool:
    if not text:
        return False
    stripped = text.strip()
    lower = stripped.lower()
    if lower == "unknown command":
        return True
    if "unknown ota command" in lower:
        return True
    if stripped.upper().startswith("ERR"):
        return True
    return False


def _local_state_from_download(word: str, pct: int) -> str:
    w = word.strip().lower()
    if w == "idle":
        return "none"
    if "ready to install" in w or w == "done":
        return "ready"
    if "paused" in w:
        return "paused"
    if "failed" in w:
        return "failed"
    if pct > 0 and pct < 100:
        return "downloading"
    if pct >= 100:
        return "ready"
    return "downloading"


def parse_ota_status(text: str) -> dict[str, Any] | None:
    """Structured running + local fetch state from `ota status`. None if unparsed."""
    if not text:
        return None
    stripped = text.strip()
    if ota_status_heard_empty(stripped):
        return {"running": {}, "local": {"state": "none"}}
    if not stripped.startswith(OTA_STATUS_PREFIX):
        return None

    running: dict[str, Any] = {}
    local: dict[str, Any] = {"state": "none"}

    this_fw = THIS_FW_RE.search(stripped)
    if this_fw:
        h = this_fw.group(1)
        if h != "?":
            running["body_hash"] = h.upper()
        running["image_kib"] = int(this_fw.group(2))

    hw = HW_RE.search(stripped)
    if hw:
        tag = hw.group(1)
        running["hw_id"] = None if tag == "?" else tag

    target = TARGET_RE.search(stripped)
    if target:
        running["target_id"] = target.group(1).lower()
        running["target_env"] = target.group(2).strip()

    bl = BL_RE.search(stripped)
    if bl:
        running["bl_apply"] = bl.group(1) == "apply"
        if bl.group(2) is not None:
            running["bl_rc"] = bl.group(2).upper()

    serving = SERVING_RE.search(stripped)
    if serving:
        running["serving"] = serving.group(1) == "on"
        running["serving_count"] = int(serving.group(2))

    keys = KEYS_RE.search(stripped)
    if keys:
        running["keys"] = int(keys.group(1))

    seed = SEED_RE.search(stripped)
    if seed:
        running["seed"] = seed.group(1) == "on"

    if "no download" in stripped:
        local["state"] = "none"
    else:
        dl = DOWNLOAD_RE.search(stripped)
        if dl:
            word, have, total, pct, mid, age = dl.groups()
            local = {
                "state": _local_state_from_download(word, int(pct)),
                "mid": mid.lower(),
                "have": int(have),
                "total": int(total),
                "pct": int(pct),
                "age_s": int(age),
                "status_word": word.strip(),
            }

    return {"running": running, "local": local}


def parse_ota_ls(text: str) -> list[dict[str, Any]] | None:
    """Catalog rows from `ota ls`. [] if empty prompt, None if unparsed."""
    if not text:
        return None
    stripped = text.strip()
    if ota_ls_heard_empty(stripped):
        return []
    if OTA_LS_HEADER not in stripped and not OTA_LS_ROW_RE.search(stripped):
        return None

    heard: list[dict[str, Any]] = []
    for match in OTA_LS_ROW_RE.finditer(stripped):
        idx, version, codec, fit, seeders, age, tag = match.groups()
        row: dict[str, Any] = {
            "index": int(idx),
            "version": version,
            "codec": codec,
            "fit": fit,
            "yours": fit == "yours",
            "n_seeders": int(seeders),
            "age_s": int(age),
        }
        if tag:
            row["session_tag"] = tag
        heard.append(row)
    return heard


def merge_ota_snapshot(
    existing: dict[str, Any] | None,
    *,
    status: dict[str, Any] | None = None,
    heard: list[dict[str, Any]] | None = None,
    raw_status: str | None = None,
    raw_ls: str | None = None,
) -> dict[str, Any]:
    """Merge partial poll results into one unit OTA snapshot."""
    out: dict[str, Any] = dict(existing or {})
    if status is not None:
        out["running"] = status.get("running") or {}
        out["local"] = status.get("local") or {"state": "none"}
        if raw_status is not None:
            out["raw_status"] = raw_status
    if heard is not None:
        out["heard"] = heard
        out["heard_yours"] = [r for r in heard if r.get("yours")]
        if raw_ls is not None:
            out["raw_ls"] = raw_ls
    return out


def ota_badge(snapshot: dict[str, Any] | None) -> str | None:
    """List badge: staged | sees update | downloading | None."""
    if not snapshot:
        return None
    local = snapshot.get("local") or {}
    state = local.get("state")
    if state == "ready":
        return "staged"
    if state == "downloading":
        return "downloading"
    heard_yours = snapshot.get("heard_yours") or []
    if heard_yours and state != "ready":
        return "sees update"
    return None
