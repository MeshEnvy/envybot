#!/usr/bin/env python3
"""Poll deployed MeshCore repeaters over LoRa; snapshot firmware + mesh stats.

Requires a local MeshCore companion (BLE, USB serial, or TCP) with mesh reach to targets.
Full poll flow: admin login (RTC from ``LOGIN_SUCCESS``) → binary status → authed
CLI/binary queries as due (name, advert, flood_advert, path_hash,
dutycycle, firmware, bootloader, telemetry, neighbors, acl). Clock sync via
mesh CLI when skew exceeds threshold. path_hash / dutycycle / lat / lon are SET
to book policy; an OK reply stamps them. Pre-1.15 MeshCore has no
``set dutycycle``; monitor uses ``set af 0`` (same 100% policy). GPS is never
pulled from the radio.

``nodes.yaml`` holds last-known state as flat fields with a ``*_pulled_at``
timestamp per query. Mesh ``name`` overwrites the registry ``name`` on pull.
Full pull snapshots append to ``data/fleet/polls.jsonl`` for audit.
Prefer ``./envybot monitor``.

Path strategy (mirrors meshcore-open): reuse the companion's saved direct path when
one exists; fall back to flood only after a timeout. A flood login makes the repeater
send a path return, so after LOGIN_SUCCESS the companion has a fresh direct path.
CLI commands ride that path; flooded CLI replies are unreliable.

All fleet queries run post-login over authed CLI or binary requests. Anonymous
requests are not used.

By default, inventory queries (firmware, bootloader, name, lat, lon, advert,
flood_advert, path_hash, dutycycle) run once and then skip; periodic
queries (status, telemetry, neighbors, acl) re-pull when older than
``--min-interval`` (default 24h). ``path_hash``, ``dutycycle``, ``lat``, and
``lon`` stay due until SET returns OK. Book lat/lon (else site loc) is the
position SoT. Use ``--force`` for everything, ``--live`` for periodic only,
``--group NAME`` to refresh specific queries. ``--unit`` filters targets only
(does not imply ``--force``). Each query has its own ``*_pulled_at``.

Login: always send admin login (guest-or-admin ACL session). Do not skip because
the companion is already in stored ACL. Binary STATUS has uptime, not wall clock.
The live RTC is ``LOGIN_SUCCESS.server_timestamp``. Login is one packet and also
refreshes the direct path. Skipping it left only a stale ``node_clock`` snapshot
or an expensive CLI clock set. After login, if that live clock is unset or
behind host time, push ``time <host epoch>`` (not ``clock sync``).

Unreachable nodes are retried until every target succeeds (or you interrupt with Ctrl+C).

Greenfield: no legacy pull group names, field aliases, or migration shims. Obsolete
``nodes.yaml`` keys are dropped on write, not mapped forward. After schema changes,
use ``--force`` or ``--group`` to repopulate.

Companion discovery (default ``--transport auto``):
  1. BLE scan for Nordic UART Service (6E400001-…) — works for BLE companion firmware
  2. Serial ports probed with appstart handshake — skips CDC/charge-only USB

Examples:
  ./envybot monitor --probe
  ./envybot monitor --transport ble
  ./envybot monitor --ble AA:BB:CC:DD:EE:FF
  ./envybot monitor --skip me0001 --skip me0006
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML

from envybot.position import book_coord, load_sites

try:
    from meshcore import EventType, MeshCore
    from meshcore.parsing import parse_acl
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "meshcore not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot monitor"
    ) from exc

HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UNIT_NUM_RE = re.compile(r"^me(\d+)$", re.I)
PLACEHOLDER_PW = frozenset({"<mt>", "<bear changed to mt>"})
VER_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)\s*\(Build:", re.I)
BL_RE = re.compile(r"^>\s*(.+)$")
VERSION_POLL_NOTE_RE = re.compile(r"(?:\s*[—–-]\s*)?version poll (\d{4}-\d{2}-\d{2})", re.I)

NODES_YAML_HEADER = (
    "# MeshEnvy fleet nodes — canonical unit registry (private).\n"
    "# One entry per physical unit (ME####): identity, credentials, deployment site,\n"
    "# and last-known mesh state (flat fields + per-query *_pulled_at unix epochs).\n"
    "# name: on-device adv name (overwritten by envybot monitor). owner: owner.info string.\n"
    "# lat/lon: book-canonical GPS (from sites.yaml / onX). Monitor SETs the radio;\n"
    "#   never overwrites these from a GET. node_clock, status, telemetry: last pull.\n"
    "# advert_interval_min, flood_advert_interval_h: local (minutes) and flood (hours) cadence.\n"
    "# path_hash_mode: MeshCore advert path hash (0=1-byte, 1=2-byte, 2=3-byte).\n"
    "# dutycycle: transmit duty cycle percent (100 = no airtime cap).\n"
    "# acl: [{key, perm}, …] access list. neighbors: [{pubkey, secs_ago, snr}, …].\n"
    "# firmware_pulled_at, bootloader_pulled_at, name_pulled_at, lat_pulled_at,\n"
    "# lon_pulled_at, advert_pulled_at, flood_advert_pulled_at, path_hash_pulled_at,\n"
    "# dutycycle_pulled_at, status_pulled_at, telemetry_pulled_at, acl_pulled_at,\n"
    "# neighbors_pulled_at.\n"
    "# Monitor policy (envybot monitor): one stamp per query; inventory once unless\n"
    "# --force/--group; status, telemetry, neighbors, acl periodic (--min-interval, default 24h).\n"
    "# Radio policy: path.hash.mode=1 (2-byte), dutycycle=100, lat/lon from book.\n"
    "#   Monitor SETs; OK stamps. MeshCore <1.15: set af 0 (dutycycle CLI added 1.15).\n"
    "# Login: always send admin login (live RTC + path). STATUS is uptime, not clock.\n"
    "# firmware_platform: meshcore | meshtastic (no admin_password => meshtastic).\n"
    "# site: sites.yaml slug, or null while in the bag / decommissioned.\n"
    "# decommissioned: unix epoch when unit was pulled from service (null = active inventory).\n"
    "# SoT for last-known reachability / fw / battery: cite *_pulled_at.\n"
    "# GPS SoT is the book lat/lon (or site loc). lat_pulled_at / lon_pulled_at =\n"
    "#   last successful SET, not a device read.\n"
    "# Stale stamp = refresh via ./envybot monitor. Never copy secrets\n"
    "# (passwords, keypairs) into this repo or other public trees.\n"
    "# next_unit: next free ME number (never reuse; onboard allocates and bumps).\n"
    "# Tool: envybot. Monitor: ./envybot monitor. Cmd: ./envybot cmd. Onboard: ./envybot onboard.\n"
    "# Greenfield: no legacy pull groups/fields; obsolete keys dropped on write.\n"
    "# Audit log: data/fleet/polls.jsonl.\n"
)

DEFAULT_LOG_PATH = Path("data/fleet/polls.jsonl")
DEFAULT_MIN_POLL_INTERVAL = 86400.0  # 24h
DEFAULT_MESH_ATTEMPTS = 10
CLOCK_SKEW_MAX = 300  # seconds; sync when *live login* RTC vs host exceeds this
FLEET_PATH_HASH_MODE = 1  # 2-byte advert path hashes
FLEET_DUTYCYCLE_PCT = 100.0
DUTYCYCLE_MATCH_EPS = 0.5
DUTYCYCLE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
FIRMWARE_CORE_RE = re.compile(r"v?(\d+(?:\.\d+)*)", re.I)
DUTYCYCLE_CLI_SINCE = (1, 15)

# Decision notes (do not reintroduce the opposite without updating this):
# - Always login. STATUS (GET_STATUS) is uptime/packets/RSSI, not wall-clock RTC.
#   Login is the cheap live clock and path refresh. ACL-skip saved a packet but
#   made node_clock a snapshot; comparing that to host time false-triggered sync.
# - Clock set is one CLI: ``time <host epoch>``. Drift is vs host, so set from
#   host, not companion RTC (``clock sync``). Only from a live login timestamp,
#   or stored clock that is unset (0 / pre-2020). Log drift before sending.
#   If the node is ahead, skip: firmware will not set backwards. A
#   "cannot go backwards" reply is a valid heard result, not a gap.
# - One retry cap for every send that expects a reply (login, CLI, binary,
#   clock set). Default 10 via --attempts. Do not add per-command caps.
# - Late replies after the wait unsubscribes are otherwise dropped. A
#   session orphan watcher logs them (CLI ``NN|``, binary tag, login) so
#   short timeouts are visible. Login has no echo id until EC-015.
#   Any late reply from a unit raises wait slack for that destination
#   (login, CLI, and binary, not just the late command). Slack grows by
#   lateness + 2s, capped by --timeout / --login-timeout.
# - Heard replies (including empty/zero) write and complete. Only a timeout
#   or ERROR/auth is a gap. Empty ACL/neighbors [], GPS 0,0, advert 0, empty
#   name, empty telemetry, unknown bootloader are all valid heard values.
# - One query, one stamp. Do not bundle independent CLI/binary sends
#   (ver vs bootloader.ver, lat vs lon, advert vs flood advert).
# - Radio policy (US field repeaters): path.hash.mode=1 (2-byte advert
#   hashes; firmware sendFlood uses mode+1), dutycycle=100, and GPS from
#   the book. SET only; stamp on OK. Never GET lat/lon into nodes.yaml.
#   MeshCore <1.15 has no set dutycycle; use set af 0 (100% = af 0) and stamp.
# - --unit filters targets only; it does not imply --force.


@dataclass(frozen=True)
class PullGroupSpec:
    mode: str  # "inventory" | "periodic"
    pulled_at_key: str
    interval: float | None = None


PULL_GROUPS: dict[str, PullGroupSpec] = {
    "firmware": PullGroupSpec("inventory", "firmware_pulled_at"),
    "bootloader": PullGroupSpec("inventory", "bootloader_pulled_at"),
    "name": PullGroupSpec("inventory", "name_pulled_at"),
    "lat": PullGroupSpec("inventory", "lat_pulled_at"),
    "lon": PullGroupSpec("inventory", "lon_pulled_at"),
    "advert": PullGroupSpec("inventory", "advert_pulled_at"),
    "flood_advert": PullGroupSpec("inventory", "flood_advert_pulled_at"),
    "path_hash": PullGroupSpec("inventory", "path_hash_pulled_at"),
    "dutycycle": PullGroupSpec("inventory", "dutycycle_pulled_at"),
    "acl": PullGroupSpec("periodic", "acl_pulled_at"),
    "status": PullGroupSpec("periodic", "status_pulled_at"),
    "telemetry": PullGroupSpec("periodic", "telemetry_pulled_at"),
    "neighbors": PullGroupSpec("periodic", "neighbors_pulled_at"),
}

PULL_GROUP_ORDER = tuple(PULL_GROUPS.keys())
AUTH_FAILURE_HINTS = ("login required", "not authorized", "not authorised", "denied")


@dataclass
class PullPolicy:
    force: bool = False
    live_only: bool = False
    force_groups: frozenset[str] = frozenset()
    min_interval: float = DEFAULT_MIN_POLL_INTERVAL


def companion_identity(client: MeshCore) -> str | None:
    """Stable id for the connected companion (pubkey preferred)."""
    info = client.self_info or {}
    for key in ("public_key", "pubkey", "identity_pubkey"):
        val = info.get(key)
        if val:
            return str(val).lower()
    return None


def companion_acl_prefix(client: MeshCore) -> str | None:
    """First 12 hex chars of companion pubkey (matches ACL ``key`` entries)."""
    identity = companion_identity(client)
    if identity and len(identity) >= 12:
        return identity[:12].lower()
    return None


def resolve_companion_acl_prefix(
    client: MeshCore, session: FleetSession | None = None
) -> str | None:
    """Companion ACL prefix from live SELF_INFO, else session cache."""
    cap = companion_acl_prefix(client)
    if cap:
        if session is not None:
            session.companion_acl_prefix = cap
        return cap
    if session is not None and session.companion_acl_prefix:
        return session.companion_acl_prefix
    return None


ORPHAN_KEEP_S = 180.0
WAIT_SLACK_MARGIN_S = 2.0  # extra cushion beyond observed lateness


@dataclass
class ReplyExpect:
    kind: str  # "login" | "cli" | "binary"
    label: str
    unit: str
    pubkey_prefix: str
    cli_token: str | None
    binary_tag: str | None
    deadline: float
    n_of: str
    resolved: bool = False


@dataclass
class FleetSession:
    """Per-run poll state. Login is always sent; this tracks companion + auth drops."""

    authed_units: set[str] = field(default_factory=set)
    companion_id: str | None = None
    companion_acl_prefix: str | None = None
    expects: list[ReplyExpect] = field(default_factory=list)
    wait_slack: dict[str, float] = field(default_factory=dict)
    _orphan_log: PollLog | None = None
    _orphan_subs: list[Any] = field(default_factory=list)
    _binary_inflight: dict[str, Any] | None = None
    _register_binary_orig: Any = None

    def dest_slack(self, unit: str) -> float:
        return self.wait_slack.get(unit, 0.0)

    def stretch_wait(self, wait_s: float, unit: str, *, cap: float = 0.0) -> float:
        """Companion estimate plus per-destination slack from late replies."""
        out = wait_s + self.dest_slack(unit)
        if cap > 0:
            out = min(out, cap)
        return out

    def note_slow_dest(self, unit: str, late_s: float) -> float:
        """Any late reply: this destination needs more wait on every request."""
        add = max(late_s, 0.0) + WAIT_SLACK_MARGIN_S
        self.wait_slack[unit] = self.wait_slack.get(unit, 0.0) + add
        now = time.monotonic()
        for exp in self.expects:
            if not exp.resolved and exp.unit == unit and now <= exp.deadline:
                exp.deadline += add
        return self.wait_slack[unit]

    def bind_companion(self, client: MeshCore) -> None:
        cid = companion_identity(client)
        cap = companion_acl_prefix(client)
        if cid is None and cap is None:
            return
        if (
            cid is not None
            and self.companion_id is not None
            and self.companion_id != cid
        ):
            self.authed_units.clear()
        if cid is not None:
            self.companion_id = cid
        if cap is not None:
            self.companion_acl_prefix = cap

    def is_authed(self, unit_key: str) -> bool:
        return unit_key in self.authed_units

    def mark_authed(self, unit_key: str) -> None:
        self.authed_units.add(unit_key)

    def clear_auth(self, unit_key: str) -> None:
        self.authed_units.discard(unit_key)

    def attach_orphan_watch(self, client: MeshCore, log: PollLog) -> None:
        """Log replies that arrive after their wait was cancelled (late mesh)."""
        self._orphan_log = log

        def on_login(event: Any) -> None:
            self._note_orphan("login", event)

        def on_cli(event: Any) -> None:
            self._note_orphan("cli", event)

        def on_binary(event: Any) -> None:
            self._note_orphan("binary", event)

        reader = getattr(client, "_reader", None)
        if reader is not None and self._register_binary_orig is None:
            orig = reader.register_binary_request
            self._register_binary_orig = orig

            def hooked(
                prefix: str,
                tag: str,
                request_type: Any,
                timeout_seconds: float,
                context: dict[str, Any] | None = None,
                is_anon: bool = False,
            ) -> None:
                orig(
                    prefix,
                    tag,
                    request_type,
                    timeout_seconds,
                    context=context or {},
                    is_anon=is_anon,
                )
                self._on_binary_registered(prefix, tag)

            reader.register_binary_request = hooked

        self._orphan_subs = [
            client.subscribe(EventType.LOGIN_SUCCESS, on_login),
            client.subscribe(EventType.LOGIN_FAILED, on_login),
            client.subscribe(EventType.CONTACT_MSG_RECV, on_cli),
            client.subscribe(EventType.BINARY_RESPONSE, on_binary),
        ]

    def begin_binary(
        self,
        *,
        label: str,
        unit: str,
        pubkey_prefix: str,
        n_of: str,
        deadline: float,
    ) -> object:
        token = object()
        self._binary_inflight = {
            "token": token,
            "label": label,
            "unit": unit,
            "pubkey_prefix": pubkey_prefix[:12].lower(),
            "n_of": n_of,
            "deadline": deadline,
            "expects": [],
        }
        return token

    def end_binary(self, token: object, *, resolved: bool) -> None:
        inf = self._binary_inflight
        if inf is not None and inf["token"] is token and resolved:
            for exp in inf["expects"]:
                exp.resolved = True
        if inf is not None and inf["token"] is token:
            self._binary_inflight = None

    def _on_binary_registered(self, prefix: str, tag: str) -> None:
        if not tag or str(tag).startswith("_pending_"):
            return
        inf = self._binary_inflight
        if inf is None:
            return
        exp = self.track_expect(
            kind="binary",
            label=inf["label"],
            unit=inf["unit"],
            pubkey_prefix=inf["pubkey_prefix"],
            deadline=inf["deadline"],
            n_of=inf["n_of"],
            binary_tag=str(tag).lower(),
        )
        inf["expects"].append(exp)

    def track_expect(
        self,
        *,
        kind: str,
        label: str,
        unit: str,
        pubkey_prefix: str,
        deadline: float,
        n_of: str,
        cli_token: str | None = None,
        binary_tag: str | None = None,
    ) -> ReplyExpect:
        now = time.monotonic()
        self.expects = [e for e in self.expects if now - e.deadline < ORPHAN_KEEP_S]
        exp = ReplyExpect(
            kind=kind,
            label=label,
            unit=unit,
            pubkey_prefix=pubkey_prefix[:12].lower(),
            cli_token=cli_token,
            binary_tag=binary_tag.lower() if binary_tag else None,
            deadline=deadline,
            n_of=n_of,
        )
        self.expects.append(exp)
        return exp

    def resolve_expect(self, exp: ReplyExpect) -> None:
        exp.resolved = True

    def _event_binary_tag(self, event: Any) -> str:
        attrs = event.attributes or {}
        payload = event.payload or {}
        return str(attrs.get("tag") or payload.get("tag") or "").lower()

    def _expect_matches(self, exp: ReplyExpect, kind: str, event: Any) -> bool:
        if exp.kind != kind:
            return False
        attrs = event.attributes or {}
        payload = event.payload or {}
        if kind == "binary":
            tag = self._event_binary_tag(event)
            return bool(exp.binary_tag) and tag == exp.binary_tag
        prefix = str(attrs.get("pubkey_prefix") or payload.get("pubkey_prefix") or "")
        if prefix[:12].lower() != exp.pubkey_prefix:
            return False
        if kind != "cli":
            return True
        text = str(payload.get("text") or "")
        token = text[:3] if len(text) >= 3 and text[2] == "|" else None
        return exp.cli_token is None or token == exp.cli_token

    def _note_orphan(self, kind: str, event: Any) -> None:
        now = time.monotonic()
        log = self._orphan_log
        if log is None:
            return
        matched = [
            exp
            for exp in self.expects
            if not exp.resolved and self._expect_matches(exp, kind, event)
        ]
        live = [exp for exp in matched if now <= exp.deadline]
        stale = [exp for exp in matched if now > exp.deadline]
        if kind == "login" and live:
            # Same pubkey, no attempt id: this is the current wait's reply.
            # Do not also call timed-out waits orphans.
            return
        if not stale:
            return
        if kind in ("cli", "binary"):
            exp = stale[0]
            late = now - exp.deadline
            extra = ""
            if kind == "cli" and isinstance(event.payload, dict):
                raw = str(event.payload.get("text") or "")
                extra = f" {raw[:40]!r}" if raw else ""
            elif kind == "binary" and exp.binary_tag:
                extra = f" tag={exp.binary_tag}"
            log.step(
                f"orphan: {exp.unit} late {exp.label} {exp.n_of} "
                f"+{late:.1f}s after timeout{extra}"
            )
            exp.resolved = True
            self._raise_dest_wait(exp.unit, late, log)
            return
        newest = max(stale, key=lambda e: e.deadline)
        late = now - newest.deadline
        waits = ", ".join(e.n_of for e in stale)
        log.step(
            f"orphan: {newest.unit} late login after all waits ended "
            f"+{late:.1f}s after {newest.n_of}; expired: {waits}"
        )
        for exp in stale:
            exp.resolved = True
        self._raise_dest_wait(newest.unit, late, log)

    def _raise_dest_wait(self, unit: str, late_s: float, log: PollLog) -> None:
        extra = self.note_slow_dest(unit, late_s)
        log.step(f"slow path {unit}: wait +{extra:.0f}s on all requests")


async def ensure_companion_identity(
    client: MeshCore, *, timeout: float = 5.0
) -> str | None:
    """Wait for SELF_INFO public_key (re-send appstart if missing)."""
    cap = companion_acl_prefix(client)
    if cap:
        return cap
    await client.commands.send_appstart()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cap = companion_acl_prefix(client)
        if cap:
            return cap
        await asyncio.sleep(0.05)
    return None


FLEET_PULL_AT_KEYS = tuple(spec.pulled_at_key for spec in PULL_GROUPS.values())


def load_nodes_doc(nodes_path: Path) -> dict[str, Any]:
    """Load nodes.yaml body without preserving stale comment headers on rewrite."""
    yaml = YAML()
    raw = nodes_path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            start = i
            break
    payload = "".join(lines[start:])
    return yaml.load(payload) or {}


def max_unit_num(nodes: dict[str, Any] | None) -> int:
    nums: list[int] = []
    for key, node in (nodes or {}).items():
        m = UNIT_NUM_RE.match(str(key))
        if m:
            nums.append(int(m.group(1)))
        if isinstance(node, dict):
            m2 = UNIT_NUM_RE.match(str(node.get("unit_id") or ""))
            if m2:
                nums.append(int(m2.group(1)))
    return max(nums) if nums else 0


def ensure_next_unit(doc: dict[str, Any]) -> int:
    """Next free ME number: never reuse. At least max(existing)+1."""
    floor = max_unit_num(doc.get("nodes") or {}) + 1
    try:
        claimed = int(doc.get("next_unit") or 0)
    except (TypeError, ValueError):
        claimed = 0
    nxt = max(floor, claimed, 1)
    doc["next_unit"] = nxt
    return nxt


def allocate_unit_id(doc: dict[str, Any]) -> str:
    n = ensure_next_unit(doc)
    doc["next_unit"] = n + 1
    return f"ME{n:04d}"


def remember_unit_id(doc: dict[str, Any], unit_id: str) -> None:
    m = UNIT_NUM_RE.match(unit_id.strip())
    if not m:
        return
    n = int(m.group(1))
    doc["next_unit"] = max(ensure_next_unit(doc), n + 1)


def write_nodes_doc(nodes_path: Path, doc: dict[str, Any]) -> None:
    ensure_next_unit(doc)
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    ordered: dict[str, Any] = {"next_unit": doc["next_unit"]}
    for key, val in doc.items():
        if key == "next_unit":
            continue
        ordered[key] = val
    tmp_path = nodes_path.with_name(nodes_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(NODES_YAML_HEADER)
        yaml.dump(ordered, fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(nodes_path)


# Nordic UART Service — same filter meshcore-open uses for BLE discovery
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
MESH_NAME_PREFIXES = (
    "MeshCore-",
    "Whisper-",
    "WisCore-",
    "Seeed",
    "Lilygo",
    "HT-",
    "LowMesh_MC_",
    "NRF52",
)
SERIAL_GLOB = ("cu.usbmodem*", "cu.wchusbserial*", "ttyUSB*", "ttyACM*")


def attempt_label(attempt: int, attempts: int) -> str:
    """`n/M` when capped, else `n` (unlimited)."""
    return f"{attempt}/{attempts}" if attempts else str(attempt)


def format_interval(seconds: float) -> str:
    if seconds <= 0:
        return "0"
    if seconds >= 86400 and seconds % 86400 == 0:
        days = int(seconds // 86400)
        return f"{days}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours}h"
    return f"{int(seconds)}s"


def poll_age_label(pulled_at: int | None, *, now: int) -> str:
    if pulled_at is None:
        return "never"
    age = max(0, now - int(pulled_at))
    if age < 3600:
        return f"{age / 60:.0f}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def _firmware_pulled_at(node: dict[str, Any]) -> int | None:
    stamp = node.get("firmware_pulled_at")
    return int(stamp) if stamp is not None else None


def normalize_fleet_node(node: dict[str, Any]) -> None:
    """Drop obsolete keys before read/write (greenfield — no field aliasing)."""
    node.pop("live", None)
    node.pop("last_version_poll", None)
    node.pop("basic_pulled_at", None)
    node.pop("features", None)
    node.pop("position_pulled_at", None)


def _stamp_and_value(node: dict[str, Any], stamp_key: str, value_key: str) -> bool:
    return node.get(stamp_key) is not None and node.get(value_key) is not None


def fleet_group_complete(
    node: dict[str, Any],
    group: str,
    sites: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if group == "firmware":
        return _firmware_pulled_at(node) is not None and bool(node.get("firmware_version"))
    if group == "bootloader":
        return _stamp_and_value(node, "bootloader_pulled_at", "bootloader_version")
    if group == "name":
        return node.get("name_pulled_at") is not None
    if group == "lat":
        if book_coord(node, "lat", sites) is None:
            return True
        return node.get("lat_pulled_at") is not None
    if group == "lon":
        if book_coord(node, "lon", sites) is None:
            return True
        return node.get("lon_pulled_at") is not None
    if group == "telemetry":
        return _stamp_and_value(node, "telemetry_pulled_at", "telemetry")
    if group == "status":
        return (
            node.get("status_pulled_at") is not None
            and isinstance(node.get("status"), dict)
            and node["status"].get("uptime_secs") is not None
        )
    if group == "advert":
        return _stamp_and_value(node, "advert_pulled_at", "advert_interval_min")
    if group == "flood_advert":
        return _stamp_and_value(node, "flood_advert_pulled_at", "flood_advert_interval_h")
    if group == "path_hash":
        return (
            node.get("path_hash_pulled_at") is not None
            and path_hash_matches_policy(node.get("path_hash_mode"))
        )
    if group == "dutycycle":
        return (
            node.get("dutycycle_pulled_at") is not None
            and dutycycle_matches_policy(node.get("dutycycle"))
        )
    if group == "acl":
        return _stamp_and_value(node, "acl_pulled_at", "acl")
    if group == "neighbors":
        return _stamp_and_value(node, "neighbors_pulled_at", "neighbors")
    return False


def fleet_group_is_due(
    node: dict[str, Any],
    group: str,
    *,
    policy: PullPolicy,
    now: int,
    sites: dict[str, dict[str, Any]] | None = None,
) -> bool:
    spec = PULL_GROUPS[group]
    if group in ("lat", "lon") and book_coord(node, group, sites) is None:
        return False
    if policy.force or group in policy.force_groups:
        return True
    if policy.live_only and spec.mode != "periodic":
        return False
    if not fleet_group_complete(node, group, sites):
        return True
    if spec.mode == "inventory":
        return False
    stamp = node.get(spec.pulled_at_key)
    if stamp is None:
        return True
    interval = spec.interval if spec.interval is not None else policy.min_interval
    if interval <= 0:
        return True
    return (now - int(stamp)) >= interval


def fleet_due_groups(
    node: dict[str, Any],
    *,
    policy: PullPolicy,
    now: int,
    sites: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    due = [
        group
        for group in PULL_GROUP_ORDER
        if fleet_group_is_due(node, group, policy=policy, now=now, sites=sites)
    ]
    return due


def _group_need_note(
    node: dict[str, Any],
    group: str,
    sites: dict[str, dict[str, Any]] | None = None,
) -> str:
    if fleet_group_complete(node, group, sites):
        return "refresh"
    if group in ("lat", "lon"):
        want = book_coord(node, group, sites)
        if want is None:
            return "no book position"
        return f"push {want:.5f}"
    if group == "path_hash" and node.get("path_hash_mode") is not None:
        return f"mode {node['path_hash_mode']} (want {FLEET_PATH_HASH_MODE})"
    if group == "dutycycle" and node.get("dutycycle") is not None:
        return f"{node['dutycycle']}% (want {int(FLEET_DUTYCYCLE_PCT)})"
    return "missing"


def format_pull_plan(
    node: dict[str, Any],
    due_groups: list[str],
    *,
    policy: PullPolicy,
    now: int,
    sites: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Human plan for one unit: what this session will fetch vs skip."""
    need = ", ".join(f"{g} ({_group_need_note(node, g, sites)})" for g in due_groups) or "none"
    skip_inv: list[str] = []
    skip_fresh: list[str] = []
    for group in PULL_GROUP_ORDER:
        if group in due_groups:
            continue
        spec = PULL_GROUPS[group]
        if spec.mode == "inventory":
            skip_inv.append(group)
        else:
            skip_fresh.append(group)
    skip_bits: list[str] = []
    if skip_inv:
        skip_bits.append(f"{', '.join(skip_inv)} (have)")
    if skip_fresh:
        skip_bits.append(f"{', '.join(skip_fresh)} (fresh <{format_interval(policy.min_interval)})")
    skip = "; ".join(skip_bits) if skip_bits else "none"
    return need, skip


def fleet_incomplete_groups(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Groups missing data regardless of pull policy."""
    return [group for group in PULL_GROUP_ORDER if not fleet_group_complete(node, group, sites)]


def gaps_from_poll_result(res: PollResult) -> list[str]:
    gaps: list[str] = []
    if "firmware" in res.polled_groups and not res.firmware_version:
        gaps.append("firmware")
    if "bootloader" in res.polled_groups and res.bootloader_version is None:
        gaps.append("bootloader")
    if "name" in res.polled_groups and res.name is None:
        gaps.append("name")
    if "lat" in res.polled_groups and res.lat is None:
        gaps.append("lat")
    if "lon" in res.polled_groups and res.lon is None:
        gaps.append("lon")
    if "telemetry" in res.polled_groups and res.telemetry is None:
        gaps.append("telemetry")
    if "status" in res.polled_groups and not res.status:
        gaps.append("status")
    if "advert" in res.polled_groups and res.advert_interval_min is None:
        gaps.append("advert")
    if "flood_advert" in res.polled_groups and res.flood_advert_interval_h is None:
        gaps.append("flood_advert")
    if "path_hash" in res.polled_groups and not path_hash_matches_policy(res.path_hash_mode):
        gaps.append("path_hash")
    if "dutycycle" in res.polled_groups and not dutycycle_matches_policy(res.dutycycle):
        gaps.append("dutycycle")
    if "acl" in res.polled_groups and res.acl is None:
        gaps.append("acl")
    if "neighbors" in res.polled_groups and res.neighbors is None:
        gaps.append("neighbors")
    return gaps


def partition_due_targets(
    targets: list[RouterTarget],
    nodes: dict[str, Any],
    *,
    policy: PullPolicy,
    now: int,
    sites: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[RouterTarget], list[RouterTarget]]:
    due: list[RouterTarget] = []
    skipped: list[RouterTarget] = []
    for target in targets:
        node = nodes.get(target.key) or {}
        target.due_groups = fleet_due_groups(node, policy=policy, now=now, sites=sites)
        if target.due_groups:
            due.append(target)
        else:
            skipped.append(target)
    return due, skipped


@dataclass
class CompanionCandidate:
    transport: str
    label: str
    ble_address: str | None = None
    ble_device: Any = None
    serial_port: str | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None
    node_name: str | None = None


@dataclass
class RouterTarget:
    key: str
    unit_id: str
    name: str
    site: str | None
    pubkey_hex: str
    admin_password: str
    due_groups: list[str] = field(default_factory=list)


def poll_staleness_key(target: RouterTarget) -> tuple[int, int, str]:
    """Due groups first; inventory gaps before live-only refresh."""
    if not target.due_groups:
        return (2, 0, target.key)
    has_inventory = any(
        PULL_GROUPS[g].mode == "inventory" for g in target.due_groups
    )
    return (0 if has_inventory else 1, len(target.due_groups), target.key)


@dataclass
class PollResult:
    key: str
    ok: bool
    firmware_version: str | None = None
    bootloader_version: str | None = None
    firmware_platform: str | None = None
    error: str | None = None
    raw_ver: str | None = None
    raw_bl: str | None = None
    node_clock: int | None = None
    status: dict[str, Any] | None = None
    name: str | None = None
    owner: str | None = None
    lat: float | None = None
    lon: float | None = None
    position: dict[str, float] | None = None
    telemetry: list[Any] | None = None
    advert_interval_min: int | None = None
    flood_advert_interval_h: int | None = None
    path_hash_mode: int | None = None
    dutycycle: float | None = None
    acl: list[dict[str, Any]] | None = None
    neighbors: list[dict[str, Any]] | None = None
    polled_groups: frozenset[str] = frozenset()
    stat_errors: list[str] = field(default_factory=list)


def load_targets(
    nodes_path: Path,
    *,
    deployed_only: bool,
    include: set[str] | None,
    skip: set[str] | None = None,
) -> list[RouterTarget]:
    doc = load_nodes_doc(nodes_path)
    nodes = doc.get("nodes") or {}
    out: list[RouterTarget] = []

    for key, node in nodes.items():
        if include and key not in include:
            continue
        if skip and key in skip:
            continue
        unit_id = str(node.get("unit_id") or key.upper())
        site = node.get("site")
        if node.get("decommissioned"):
            continue
        if deployed_only and not site:
            continue
        notes = str(node.get("notes") or "")
        if "RETIRED" in notes.upper():
            continue
        pubkey = node.get("identity_pubkey")
        if not pubkey or not isinstance(pubkey, str) or not HEX_PUBKEY_RE.match(pubkey.strip()):
            continue
        admin_pw = node.get("admin_password")
        if not admin_pw or not isinstance(admin_pw, str):
            continue
        admin_pw = admin_pw.strip()
        if not admin_pw or admin_pw in PLACEHOLDER_PW:
            continue
        normalize_fleet_node(node)
        out.append(
            RouterTarget(
                key=key,
                unit_id=unit_id,
                name=str(node.get("name") or unit_id),
                site=site,
                pubkey_hex=pubkey.strip().lower(),
                admin_password=admin_pw,
            )
        )
    out.sort(key=poll_staleness_key)
    return out


def parse_firmware_core(version: str | None) -> tuple[int, ...] | None:
    """Leading dotted ints from ``v1.14.1-467959c`` / ``1.16.0.1`` / ``v0.1.3``."""
    if not version:
        return None
    match = FIRMWARE_CORE_RE.search(str(version).strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _version_at_least(have: tuple[int, ...], need: tuple[int, ...]) -> bool:
    width = max(len(have), len(need))
    padded_have = have + (0,) * (width - len(have))
    padded_need = need + (0,) * (width - len(need))
    return padded_have >= padded_need


def firmware_has_dutycycle_cli(version: str | None) -> bool | None:
    """True if ``set dutycycle`` exists. False if MeshCore 1.x before 1.15. None if unknown.

    EnvyOS ``0.x`` already has the command. Unparseable strings (``ShortTurbo``) are unknown.
    """
    core = parse_firmware_core(version)
    if core is None:
        return None
    if core[0] == 0:
        return True
    return _version_at_least(core, DUTYCYCLE_CLI_SINCE)


def airtime_factor_for_dutycycle(pct: float) -> float:
    """MeshCore ``af = (100 / dutycycle) - 1``. ``100%`` → ``0``."""
    return (100.0 / pct) - 1.0


def parse_firmware(text: str) -> tuple[str | None, str | None]:
    text = text.strip()
    m = VER_RE.match(text)
    if m:
        return m.group(1), "meshcore"
    if text:
        return text.split()[0], "meshcore"
    return None, None


def parse_bootloader(text: str) -> str | None:
    text = text.strip()
    m = BL_RE.match(text)
    if m:
        val = m.group(1).strip()
        return None if val.lower() == "unknown" else val
    if text.upper().startswith("ERROR"):
        return None
    return text or None


def cli_error_reply(text: str | None) -> bool:
    if not text:
        return False
    for line in text.splitlines():
        trimmed = line.strip()
        body = trimmed[1:].strip() if trimmed.startswith(">") else trimmed
        if body.upper().startswith("ERROR"):
            return True
    return False


def parse_get_value(text: str | None) -> str | None:
    """Extract ``> value`` from a MeshCore GET reply."""
    if not text:
        return None
    for line in text.strip().splitlines():
        trimmed = line.strip()
        if trimmed.startswith(">"):
            val = trimmed[1:].strip()
            if val.upper().startswith("ERROR"):
                return None
            return val or None
    trimmed = text.strip()
    return None if trimmed.upper().startswith("ERROR") else trimmed or None


def parse_coord(text: str | None) -> float | None:
    val = parse_get_value(text)
    if val is None:
        return None
    try:
        return float(val.split()[0])
    except ValueError:
        return None


def parse_int_get_value(text: str | None) -> int | None:
    val = parse_get_value(text)
    if val is None:
        return None
    try:
        return int(val.split()[0])
    except ValueError:
        return None


def parse_dutycycle(text: str | None) -> float | None:
    if not text:
        return None
    val = parse_get_value(text)
    blob = val if val is not None else text.strip()
    match = DUTYCYCLE_PCT_RE.search(blob)
    if match:
        return float(match.group(1))
    token = blob.split()[0].rstrip("%")
    try:
        return float(token)
    except ValueError:
        return None


def store_dutycycle(value: float) -> int | float:
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return int(rounded)
    return value


def path_hash_matches_policy(value: Any) -> bool:
    try:
        return int(value) == FLEET_PATH_HASH_MODE
    except (TypeError, ValueError):
        return False


def dutycycle_matches_policy(value: Any) -> bool:
    try:
        return abs(float(value) - FLEET_DUTYCYCLE_PCT) < DUTYCYCLE_MATCH_EPS
    except (TypeError, ValueError):
        return False


def cli_set_ok(text: str | None) -> bool:
    if not text or cli_error_reply(text):
        return False
    return "ok" in text.lower()


def cli_unknown_reply(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    return "unknown config" in lower or "unknown command" in lower


def normalize_status_payload(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map meshcore STATUS fields to fleet registry names."""
    if not raw:
        return None
    uptime = raw.get("uptime")
    if uptime is None:
        return None
    return {
        "battery_mv": raw.get("bat"),
        "tx_queue_len": raw.get("tx_queue_len"),
        "noise_floor": raw.get("noise_floor"),
        "last_rssi": raw.get("last_rssi"),
        "packets_recv": raw.get("nb_recv"),
        "packets_sent": raw.get("nb_sent"),
        "tx_airtime_secs": raw.get("airtime"),
        "uptime_secs": uptime,
        "sent_flood": raw.get("sent_flood"),
        "sent_direct": raw.get("sent_direct"),
        "recv_flood": raw.get("recv_flood"),
        "recv_direct": raw.get("recv_direct"),
        "err_events": raw.get("full_evts"),
        "last_snr": raw.get("last_snr"),
        "direct_dups": raw.get("direct_dups"),
        "flood_dups": raw.get("flood_dups"),
        "rx_airtime_secs": raw.get("rx_airtime"),
        "recv_errors": raw.get("recv_errors"),
    }


def node_clock_is_unset(node_clock: int | None) -> bool:
    """True when stored/login RTC is 0 or pre-2020 (not a stale-but-valid snapshot)."""
    if node_clock is None:
        return False
    return node_clock == 0 or node_clock < 1_600_000_000


def node_clock_needs_sync(node_clock: int | None, *, now: int | None = None) -> bool:
    """True when a *live* login RTC looks unset or skewed vs host time."""
    if node_clock is None:
        return False
    if node_clock_is_unset(node_clock):
        return True
    now = now or int(time.time())
    return abs(node_clock - now) > CLOCK_SKEW_MAX


def format_clock_drift(node_clock: int, *, now: int) -> str:
    """Login RTC minus host. Positive = node ahead."""
    drift = node_clock - now
    sign = "+" if drift > 0 else ""
    abs_d = abs(drift)
    if abs_d >= 86400:
        return f"{sign}{drift}s ({sign}{drift / 86400:.1f}d)"
    if abs_d >= 3600:
        return f"{sign}{drift}s ({sign}{drift / 3600:.1f}h)"
    return f"{sign}{drift}s"


def classify_clock_sync(text: str | None) -> str | None:
    """``set`` if RTC changed, ``unchanged`` if device refused, else None."""
    if not text:
        return None
    lower = text.lower()
    if "cannot go backwards" in lower:
        return "unchanged"
    if "ok" in lower and "clock set" in lower:
        return "set"
    return None


async def sync_repeater_clock(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> str | None:
    """Set repeater RTC to host time. Returns ``set``, ``unchanged``, or None."""
    now = int(time.time()) + 1
    text = await send_cmd_sync(
        client,
        target,
        f"time {now}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    kind = classify_clock_sync(text)
    if kind:
        if kind == "set":
            log.step(f"clock set OK (time {now})")
        else:
            log.step("clock set: refused (valid; firmware will not go backwards)")
        return kind
    if text:
        log.step(f"clock set failed: {text.strip()[:80]}")
    else:
        log.step("clock set failed: no response")
    return None


async def set_path_hash_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> int | None:
    """``set path.hash.mode 1`` (2-byte). Stamp on OK."""
    raw = await send_cmd_sync(
        client,
        target,
        f"set path.hash.mode {FLEET_PATH_HASH_MODE}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        log.step("path.hash: no response")
        return None
    if cli_unknown_reply(raw):
        log.step("path.hash: unsupported")
        return None
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"path.hash: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"path.hash set OK ({FLEET_PATH_HASH_MODE} = 2-byte)")
    return FLEET_PATH_HASH_MODE


async def set_dutycycle_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    firmware_version: str | None = None,
) -> float | None:
    """``set dutycycle 100``, or ``set af 0`` on MeshCore <1.15. Stamp on OK."""
    want = int(FLEET_DUTYCYCLE_PCT)
    af = airtime_factor_for_dutycycle(FLEET_DUTYCYCLE_PCT)
    try_native = firmware_has_dutycycle_cli(firmware_version) is not False
    if try_native:
        raw = await send_cmd_sync(
            client,
            target,
            f"set dutycycle {want}",
            timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
        )
        if raw is None:
            log.step("dutycycle: no response")
            return None
        if not cli_unknown_reply(raw):
            if cli_error_reply(raw) or not cli_set_ok(raw):
                log.step(f"dutycycle: set failed ({raw.strip()[:40]})")
                return None
            confirmed = parse_dutycycle(raw)
            if confirmed is not None:
                log.step(f"dutycycle set OK ({confirmed:g}%)")
                return confirmed
            log.step(f"dutycycle set OK ({want}%)")
            return FLEET_DUTYCYCLE_PCT
        log.step("dutycycle: unsupported, using af")
    else:
        log.step("dutycycle: pre-1.15, using af")
    raw = await send_cmd_sync(
        client,
        target,
        f"set af {af:g}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        log.step("dutycycle af: no response")
        return None
    if cli_unknown_reply(raw):
        log.step("dutycycle af: unsupported")
        return None
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"dutycycle af: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"dutycycle set OK ({want}% via af {af:g})")
    return FLEET_DUTYCYCLE_PCT


def format_book_coord(value: float) -> str:
    return f"{value:.6f}"


async def set_book_coord(
    client: MeshCore,
    target: RouterTarget,
    axis: str,
    want: float,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> float | None:
    """``set lat|lon <book>``. Stamp on OK. Never GET."""
    raw = await send_cmd_sync(
        client,
        target,
        f"set {axis} {format_book_coord(want)}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        log.step(f"{axis}: no response")
        return None
    if cli_unknown_reply(raw):
        log.step(f"{axis}: unsupported")
        return None
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"{axis}: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"{axis} set OK ({want:.5f})")
    return want


async def pull_repeater_status(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> dict[str, Any] | None:
    """Binary GET_STATUS (packets, airtime, RSSI, uptime, …). Requires ACL session."""
    wait_cap = mesh_wait_seconds(6000, cap=cmd_timeout)

    async def fetch(dest_wait: float) -> dict[str, Any] | None:
        return await client.commands.req_status_sync(
            target.pubkey_hex, timeout=dest_wait, min_timeout=8
        )

    async def flood_on_retry(_attempt: int) -> None:
        await reset_to_flood(client, target, log=log)

    raw = await retry_binary_req(
        "GET_STATUS",
        fetch,
        attempts=attempts,
        log=log,
        on_retry=flood_on_retry,
        session=session,
        target=target,
        wait_s=wait_cap,
        cap=cmd_timeout,
    )
    return normalize_status_payload(raw)


async def maybe_sync_repeater_clock(
    client: MeshCore,
    target: RouterTarget,
    *,
    login_clock: int | None,
    stored_clock: int | None,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> int | None:
    """Sync from a live login timestamp, or when stored RTC is unset (0 / pre-2020).

    Login is required for a live clock (STATUS has uptime only). Stored
    ``node_clock`` is a last-pull snapshot: do not treat its age vs host as skew.
    """
    now_ts = int(time.time())
    if login_clock is not None:
        if node_clock_is_unset(login_clock):
            log.step(f"clock unset ({login_clock}), syncing …")
        else:
            drift = login_clock - now_ts
            if abs(drift) <= CLOCK_SKEW_MAX:
                return login_clock
            log.step(f"clock drift {format_clock_drift(login_clock, now=now_ts)} (login vs host)")
            if drift > 0:
                log.step("clock: node ahead, skip sync (firmware will not go backwards)")
                return login_clock
        kind = await sync_repeater_clock(
            client,
            target,
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
        )
        if kind == "set":
            return now_ts + 1
        return login_clock
    if stored_clock is not None and node_clock_is_unset(stored_clock):
        log.step(f"clock unset (stored {stored_clock}), syncing …")
        kind = await sync_repeater_clock(
            client,
            target,
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
        )
        if kind == "set":
            return now_ts + 1
    return None


CONTACT_FLAG_FAVORITE = 0x01
CONTACT_TYPE_REPEATER = 2
TXT_TYPE_CLI_DATA = 1
_cli_prefix_counter = 0


@dataclass
class PollLog:
    """Progress = one line per mesh step; verbose = protocol/debug detail."""

    progress: bool = True
    verbose: bool = False

    def step(self, msg: str) -> None:
        if self.progress:
            print(f"  {msg}", flush=True)

    def detail(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}", flush=True)


def contact_display_name(target: RouterTarget) -> str:
    return f"{target.unit_id} {target.name}"[:32]


def flood_contact_stub(target: RouterTarget) -> dict[str, Any]:
    return {
        "public_key": target.pubkey_hex.lower(),
        "type": CONTACT_TYPE_REPEATER,
        "flags": CONTACT_FLAG_FAVORITE,
        "out_path_len": -1,
        "out_path_hash_mode": -1,
        "out_path": "",
        "adv_name": contact_display_name(target),
        "last_advert": 0,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
    }


async def ensure_contact_favorited(
    client: MeshCore, contact: dict[str, Any], *, log: PollLog | None = None
) -> dict[str, Any]:
    log = log or PollLog()
    flags = contact.get("flags", 0)
    if flags & CONTACT_FLAG_FAVORITE:
        return contact
    new_flags = flags | CONTACT_FLAG_FAVORITE
    updated = dict(contact)
    updated["flags"] = new_flags
    res = await client.commands.change_contact_flags(updated, new_flags)
    if res.type == EventType.ERROR:
        log.detail(f"favorite failed: {res.payload}")
        return contact
    pk = contact.get("public_key")
    if pk and pk in client.contacts:
        client.contacts[pk]["flags"] = new_flags
    log.detail(f"favorited {contact.get('adv_name', '?')}")
    return updated


async def ensure_contact_on_device(
    client: MeshCore, target: RouterTarget, *, log: PollLog | None = None
) -> dict[str, Any]:
    log = log or PollLog()
    """Companion firmware rejects login to pubkeys not in its contact table."""
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    if contact:
        return await ensure_contact_favorited(client, contact, log=log)
    await client.ensure_contacts(follow=True)
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    if not contact:
        stub = flood_contact_stub(target)
        res = await client.commands.add_contact(stub)
        if res.type == EventType.ERROR:
            log.detail(f"add_contact: {res.payload}")
        await client.ensure_contacts(follow=True)
        contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12]) or stub
        log.detail(f"added contact {contact_display_name(target)}")
    return await ensure_contact_favorited(client, contact, log=log)


async def sync_fleet_contacts(
    client: MeshCore, targets: list[RouterTarget], *, log: PollLog | None = None
) -> None:
    log = log or PollLog()
    """Pre-create and favorite all poll targets on the companion."""
    print(f"Ensuring {len(targets)} fleet contact(s) exist and are favorited …")
    added = 0
    favorited = 0
    async with client.commands._mesh_request_lock:
        await client.ensure_contacts(follow=True)
        for target in targets:
            had = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
            was_fav = bool(had and had.get("flags", 0) & CONTACT_FLAG_FAVORITE)
            contact = await ensure_contact_on_device(client, target, log=log)
            if not had:
                added += 1
            if not was_fav and contact.get("flags", 0) & CONTACT_FLAG_FAVORITE:
                favorited += 1
    print(
        f"Fleet contacts ready: {added} added, {favorited} newly favorited, "
        f"{len(client.contacts)} total on companion"
    )


def next_cli_prefix() -> str:
    """3-char prefix echoed by repeater CLI replies (meshcore-open RepeaterCommandService)."""
    global _cli_prefix_counter
    _cli_prefix_counter = (_cli_prefix_counter + 1) & 0xFF
    return f"{_cli_prefix_counter:02X}|"


def strip_cli_prefix(text: str) -> str:
    if len(text) >= 3 and text[2] == "|":
        return text[3:].lstrip()
    return text.strip()


def is_cli_txt_type(txt_type: int) -> bool:
    return txt_type == TXT_TYPE_CLI_DATA or (txt_type >> 2) == TXT_TYPE_CLI_DATA


async def send_cli_frame(
    client: MeshCore, dst_hex: str, command: str, *, attempt: int = 0
) -> Any:
    """CMD_SEND_TXT_MSG / TXT_TYPE_CLI_DATA (matches meshcore-open buildSendCliCommandFrame)."""
    dst_bytes = bytes.fromhex(dst_hex)[:6]
    timestamp = int(time.time())
    data = (
        b"\x02\x01"
        + bytes([attempt & 0xFF])
        + timestamp.to_bytes(4, "little")
        + dst_bytes
        + command.encode("utf-8")
        + b"\x00"
    )
    return await client.commands.send(data, [EventType.MSG_SENT, EventType.ERROR])


def mesh_wait_seconds(suggested_ms: int, *, cap: float, floor: float = 8) -> float:
    """Derive wait from companion suggested_timeout (matches meshcore-open scale)."""
    est = suggested_ms / 1000 * 1.5 + 3
    wait = max(est, floor)
    if cap > 0:
        wait = min(wait, cap)
    return wait


def parse_cli_contact_msg(event: Any, *, prefix_token: str) -> str | None:
    txt_type = int(event.payload.get("txt_type", 0))
    text = str(event.payload.get("text") or "")
    if not is_cli_txt_type(txt_type):
        return None
    if len(text) >= 3 and text[2] == "|" and text[:3] != prefix_token:
        return None
    return strip_cli_prefix(text)


async def drain_offline_queue(client: MeshCore, *, max_rounds: int = 6) -> None:
    """Pull pending companion messages (auto-fetch stops after NO_MORE_MSGS)."""
    for _ in range(max_rounds):
        res = await client.commands.get_msg(timeout=2)
        if res is None or res.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
            break


async def wait_cli_response(
    client: MeshCore,
    *,
    pubkey_prefix: str,
    prefix_token: str,
    timeout: float,
    log: PollLog | None = None,
    session: FleetSession | None = None,
    unit: str | None = None,
    slack0: float = 0.0,
) -> str | None:
    """Wait for CLI reply, keeping the message listener armed the whole time.

    The reply is dispatched during the offline-queue fetch (ours or the lib's
    auto-fetch). The listener must persist across drains. Cancelling and
    re-creating it around drain_offline_queue loses the dispatch.
    Slack growth on this destination extends the wait in place.
    """
    log = log or PollLog()
    filters = {"pubkey_prefix": pubkey_prefix[:12]}
    start = time.monotonic()

    def remaining() -> float:
        extra = 0.0
        if session is not None and unit:
            extra = session.dest_slack(unit) - slack0
        return start + timeout + extra - time.monotonic()

    def arm_msg_listener(left: float) -> asyncio.Task:
        return asyncio.create_task(
            client.dispatcher.wait_for_event(
                EventType.CONTACT_MSG_RECV,
                attribute_filters=filters,
                timeout=left,
            )
        )

    left = remaining()
    msg_task = arm_msg_listener(left)
    try:
        while True:
            left = remaining()
            if left <= 0:
                return None
            wake_task = asyncio.create_task(
                client.dispatcher.wait_for_event(
                    EventType.MESSAGES_WAITING, timeout=min(left, 2.0)
                )
            )
            done, _ = await asyncio.wait(
                {msg_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if msg_task not in done:
                if wake_task.result() is not None:
                    await drain_offline_queue(client)
                continue
            wake_task.cancel()
            event = msg_task.result()
            if event is None:
                left = remaining()
                if left <= 0:
                    return None
                msg_task = arm_msg_listener(left)
                continue
            text = parse_cli_contact_msg(event, prefix_token=prefix_token)
            if text is not None:
                return text
            log.detail(
                f"ignore CONTACT_MSG txt_type={event.payload.get('txt_type')} "
                f"text={str(event.payload.get('text') or '')[:60]!r}"
            )
            left = remaining()
            if left <= 0:
                return None
            msg_task = arm_msg_listener(left)
    finally:
        msg_task.cancel()


async def send_login_frame(client: MeshCore, dst_hex: str, password: str) -> Any:
    """CMD_SEND_LOGIN with null-terminated password (matches meshcore-open)."""
    dst_bytes = bytes.fromhex(dst_hex)[:32]
    data = b"\x1a" + dst_bytes + password.encode("utf-8") + b"\x00"
    return await client.commands.send(data, [EventType.MSG_SENT, EventType.ERROR])


async def reset_to_flood(
    client: MeshCore, target: RouterTarget, *, log: PollLog | None = None
) -> None:
    log = log or PollLog()
    """Clear the companion's saved out_path so the next send floods.

    Fallback only, after a direct-path timeout — a flood login makes the repeater
    send a path return, re-establishing the direct path on success.
    """
    res = await client.commands.reset_path(target.pubkey_hex)
    if res.type == EventType.ERROR:
        log.detail(f"path: flood (reset_path warning: {res.payload})")
    else:
        log.step("path: flood")


async def wait_login_response(
    client: MeshCore, prefix: str, *, timeout: float
) -> tuple[str, Any | None]:
    filters = {"pubkey_prefix": prefix}
    success_task = asyncio.create_task(
        client.dispatcher.wait_for_event(
            EventType.LOGIN_SUCCESS, attribute_filters=filters, timeout=timeout
        )
    )
    failed_task = asyncio.create_task(
        client.dispatcher.wait_for_event(
            EventType.LOGIN_FAILED, attribute_filters=filters, timeout=timeout
        )
    )
    done, pending = await asyncio.wait(
        {success_task, failed_task}, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
    )
    for task in pending:
        task.cancel()
    for task in done:
        event = task.result()
        if event is None:
            continue
        if task is success_task:
            return "success", event
        return "failed", event
    return "timeout", None


async def admin_login(
    client: MeshCore,
    target: RouterTarget,
    *,
    login_timeout: float,
    attempts: int,
    session: FleetSession | None = None,
    log: PollLog | None = None,
) -> tuple[bool, str | None, int | None]:
    """Always login. Live RTC is only on LOGIN_SUCCESS (STATUS has uptime, not clock)."""
    log = log or PollLog()

    dst = target.pubkey_hex
    prefix = dst[:12]
    attempt = 0

    while attempts == 0 or attempt < attempts:
        attempt += 1
        async with client.commands._mesh_request_lock:
            await ensure_contact_on_device(client, target, log=log)
            if attempt == 2:
                # Saved direct path didn't answer — fall back to flood. The flood
                # login triggers a path return, restoring the direct path on success.
                await reset_to_flood(client, target, log=log)
            sent = await send_login_frame(client, dst, target.admin_password)
            if sent is None or sent.type == EventType.ERROR:
                err = sent.payload if sent else "no response"
                log.step(f"login {attempt_label(attempt, attempts)}: send error ({err}), retrying …")
                continue
            suggested_ms = sent.payload.get("suggested_timeout", 60000)

        wait_s = mesh_wait_seconds(suggested_ms, cap=login_timeout)
        if session is not None:
            wait_s = session.stretch_wait(wait_s, target.unit_id, cap=login_timeout)
        n_of = attempt_label(attempt, attempts)
        log.step(f"send login 0x1a {n_of} (≤{wait_s:.0f}s, no echo id) …")
        log.detail(
            f"login {n_of}: dst={prefix}…, "
            f"wait={wait_s:.0f}s (suggested={suggested_ms}ms)"
        )

        exp = None
        if session is not None:
            exp = session.track_expect(
                kind="login",
                label="login",
                unit=target.unit_id,
                pubkey_prefix=prefix,
                deadline=time.monotonic() + wait_s,
                n_of=n_of,
            )
        status, event = await wait_login_response(client, prefix, timeout=wait_s)
        if exp is not None and status != "timeout":
            session.resolve_expect(exp)
        if status == "success":
            node_clock: int | None = None
            if event and event.payload:
                ts = event.payload.get("server_timestamp")
                if ts is not None:
                    node_clock = int(ts)
            if node_clock is not None:
                log.step(f"login OK (clock={node_clock})")
            else:
                log.step("login OK")
            log.detail(f"LOGIN_SUCCESS {event.payload if event else ''}")
            if session is not None:
                session.mark_authed(target.key)
            return True, None, node_clock
        if status == "failed":
            log.step("login rejected")
            log.detail(f"LOGIN_FAILED {event.payload if event else ''}")
            if session is not None:
                session.clear_auth(target.key)
            return False, "login rejected (bad password?)", None
        if attempts and attempt >= attempts:
            log.step(f"login {n_of}: timeout after {wait_s:.0f}s")
            break
        log.step(f"login {n_of}: timeout after {wait_s:.0f}s, retrying …")

    return False, f"login failed after {attempts} attempts", None


async def refresh_contact_from_device(
    client: MeshCore, target: RouterTarget
) -> dict[str, Any] | None:
    """Pull one contact record from the companion (includes out_path after flood login)."""
    prefix = target.pubkey_hex[:12]
    async with client.commands._mesh_request_lock:
        res = await client.commands.get_contact_by_key(bytes.fromhex(target.pubkey_hex))
    if res.type != EventType.NEXT_CONTACT or not res.payload:
        return client.get_contact_by_key_prefix(prefix)
    contact = res.payload
    pk = contact.get("public_key")
    if pk:
        if pk in client.contacts:
            client.contacts[pk].update(contact)
        else:
            client.contacts[pk] = contact
    return client.get_contact_by_key_prefix(prefix)


async def send_cmd_sync(
    client: MeshCore,
    target: RouterTarget,
    cmd: str,
    *,
    timeout: float,
    attempts: int = DEFAULT_MESH_ATTEMPTS,
    log: PollLog | None = None,
    session: FleetSession | None = None,
) -> str | None:
    log = log or PollLog()
    """Send CLI command; rides the direct path learned at login, floods as fallback."""
    dst_hex = target.pubkey_hex
    pubkey_prefix = dst_hex[:12]
    attempt = 0

    while attempts == 0 or attempt < attempts:
        attempt += 1
        prefix_token = next_cli_prefix()
        framed = f"{prefix_token}{cmd}"
        async with client.commands._mesh_request_lock:
            if attempt == 2:
                # Saved direct path didn't answer — fall back to flood (same as login).
                await reset_to_flood(client, target, log=log)
            sent = await send_cli_frame(client, dst_hex, framed, attempt=attempt - 1)
            if sent is None or sent.type == EventType.ERROR:
                err = sent.payload if sent else "no response"
                log.step(
                    f"send {framed!r} {attempt_label(attempt, attempts)}: "
                    f"send error ({err}), retrying …"
                )
                continue
            suggested_ms = sent.payload.get("suggested_timeout", 60000)

        wait_s = mesh_wait_seconds(suggested_ms, cap=timeout)
        slack0 = 0.0
        if session is not None:
            slack0 = session.dest_slack(target.unit_id)
            wait_s = session.stretch_wait(wait_s, target.unit_id, cap=timeout)
        n_of = attempt_label(attempt, attempts)
        log.step(f"send {framed!r} {n_of} (≤{wait_s:.0f}s) …")
        log.detail(f"cli {framed!r} {n_of}: wait {wait_s:.0f}s")

        exp = None
        if session is not None:
            exp = session.track_expect(
                kind="cli",
                label=repr(cmd),
                unit=target.unit_id,
                pubkey_prefix=pubkey_prefix,
                deadline=time.monotonic() + wait_s,
                n_of=n_of,
                cli_token=prefix_token,
            )
        text = await wait_cli_response(
            client,
            pubkey_prefix=pubkey_prefix,
            prefix_token=prefix_token,
            timeout=wait_s,
            log=log,
            session=session,
            unit=target.unit_id,
            slack0=slack0,
        )
        if exp is not None and text:
            session.resolve_expect(exp)
        if text:
            log.detail(f"cli reply {prefix_token}{text[:120]}")
            return text
        if attempts and attempt >= attempts:
            log.step(f"send {framed!r} {n_of}: timeout after {wait_s:.0f}s")
            break
        log.step(f"send {framed!r} {n_of}: timeout after {wait_s:.0f}s, retrying …")

    return None


async def retry_binary_req(
    label: str,
    fetch: Callable[[float], Awaitable[Any]],
    *,
    attempts: int,
    log: PollLog,
    success: Callable[[Any], bool] | None = None,
    on_retry: Callable[[int], Awaitable[None]] | None = None,
    session: FleetSession | None = None,
    target: RouterTarget | None = None,
    wait_s: float = 0.0,
    cap: float = 0.0,
) -> Any:
    """Retry binary mesh requests (status, telemetry, neighbors, acl, …)."""
    def ok(val: Any) -> bool:
        if success is not None:
            return success(val)
        return val is not None

    attempt = 0
    while attempts == 0 or attempt < attempts:
        attempt += 1
        n_of = attempt_label(attempt, attempts)
        dest_wait = wait_s
        if session is not None and target is not None:
            dest_wait = session.stretch_wait(wait_s, target.unit_id, cap=cap or 0.0)
        log.step(f"send binary {label} {n_of} (≤{dest_wait:.0f}s) …")
        inflight = None
        if session is not None and target is not None:
            inflight = session.begin_binary(
                label=label,
                unit=target.unit_id,
                pubkey_prefix=target.pubkey_hex,
                n_of=n_of,
                deadline=time.monotonic() + max(dest_wait, 8.0) + 1.0,
            )
        result = None
        try:
            result = await fetch(dest_wait)
        finally:
            if session is not None and inflight is not None:
                session.end_binary(inflight, resolved=ok(result))
        if ok(result):
            if attempt > 1:
                log.detail(f"{label} succeeded on {n_of}")
            return result
        if attempts and attempt >= attempts:
            log.step(f"{label} {n_of}: no response")
            break
        if on_retry is not None:
            await on_retry(attempt)
        log.step(f"{label} {n_of}: no response, retrying …")
    return None


def normalize_force_groups(groups: list[str] | None) -> frozenset[str]:
    if not groups:
        return frozenset()
    unknown = set(groups) - set(PULL_GROUPS)
    if unknown:
        raise SystemExit(f"Unknown pull group(s): {', '.join(sorted(unknown))}")
    return frozenset(groups)


def normalize_acl_payload(raw: Any) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return parse_acl(raw)
    if isinstance(raw, str):
        try:
            return parse_acl(bytes.fromhex(raw))
        except ValueError:
            return None
    return None


def normalize_neighbors_payload(raw: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("neighbours")
    if items is None:
        return []
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pubkey = item.get("pubkey")
        if not pubkey:
            continue
        out.append(
            {
                "pubkey": str(pubkey).lower(),
                "secs_ago": item.get("secs_ago"),
                "snr": item.get("snr"),
            }
        )
    return out


def cli_suggests_auth_failure(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(hint in lower for hint in AUTH_FAILURE_HINTS)


async def poll_one(
    client: MeshCore,
    target: RouterTarget,
    *,
    node: dict[str, Any],
    due_groups: frozenset[str],
    cmd_timeout: float,
    login_timeout: float,
    attempts: int = DEFAULT_MESH_ATTEMPTS,
    session: FleetSession | None = None,
    log: PollLog | None = None,
    sites: dict[str, dict[str, Any]] | None = None,
) -> PollResult:
    log = log or PollLog()
    stat_errors: list[str] = []
    polled = frozenset(due_groups)
    node_clock: int | None = None
    status: dict[str, Any] | None = None
    live_name: str | None = None
    live_owner: str | None = None
    telemetry: list[Any] | None = None
    fw: str | None = None
    platform: str | None = None
    bl: str | None = None
    raw_ver: str | None = None
    raw_bl: str | None = None
    lat: float | None = None
    lon: float | None = None
    advert_min: int | None = None
    flood_h: int | None = None
    path_hash_mode: int | None = None
    dutycycle: float | None = None
    acl: list[dict[str, Any]] | None = None
    neighbors: list[dict[str, Any]] | None = None
    try:
        ok, err, login_clock = await admin_login(
            client,
            target,
            login_timeout=login_timeout,
            attempts=attempts,
            session=session,
            log=log,
        )
        if not ok:
            return PollResult(target.key, ok=False, error=err or "admin login failed", polled_groups=polled)

        stored_clock = node.get("node_clock")
        stored_int = int(stored_clock) if stored_clock is not None else None
        node_clock = await maybe_sync_repeater_clock(
            client,
            target,
            login_clock=login_clock,
            stored_clock=stored_int,
            cmd_timeout=cmd_timeout,
            attempts=attempts,
            log=log,
            session=session,
        )
        if node_clock is None and login_clock is not None:
            node_clock = login_clock
        elif node_clock is None and stored_int is not None and not node_clock_is_unset(stored_int):
            node_clock = stored_int

        wait_cap = mesh_wait_seconds(6000, cap=cmd_timeout)

        async def flood_on_retry(_attempt: int) -> None:
            await reset_to_flood(client, target, log=log)

        if "status" in polled:
            status = await pull_repeater_status(
                client,
                target,
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
            )
            if status:
                uptime = status.get("uptime_secs")
                bat = status.get("battery_mv")
                log.step(f"status OK (uptime={uptime}s bat={bat}mV)")
            else:
                stat_errors.append("status: no response")
                log.step("status: no response")

        if "name" in polled:
            raw_name = await send_cmd_sync(
                client, target, "get name", timeout=cmd_timeout, attempts=attempts, log=log, session=session
            )
            if raw_name is None:
                stat_errors.append("name: no response")
                log.step("name: no response")
            elif cli_suggests_auth_failure(raw_name) or cli_error_reply(raw_name):
                if session is not None and cli_suggests_auth_failure(raw_name):
                    session.clear_auth(target.key)
                    log.step("auth cleared (CLI denied)")
                stat_errors.append("name: error")
                log.step("name: error")
            else:
                live_name = parse_get_value(raw_name) or ""
                log.step(f"name OK ({live_name or 'empty'})")

        if "telemetry" in polled:
            telemetry = await retry_binary_req(
                "GET_TELEMETRY",
                lambda dest_wait: client.commands.req_telemetry_sync(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                attempts=attempts,
                log=log,
                on_retry=flood_on_retry,
                session=session,
                target=target,
                wait_s=wait_cap,
                cap=cmd_timeout,
            )
            if telemetry is not None:
                channels = len(telemetry) if isinstance(telemetry, list) else 1
                log.step(f"telemetry OK ({channels} reading(s))")
            else:
                stat_errors.append("telemetry: no response")
                log.step("telemetry: no response")

        if "acl" in polled:
            acl_raw = await retry_binary_req(
                "GET_ACL",
                lambda dest_wait: client.commands.req_acl_sync(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                attempts=attempts,
                log=log,
                on_retry=flood_on_retry,
                session=session,
                target=target,
                wait_s=wait_cap,
                cap=cmd_timeout,
            )
            acl = normalize_acl_payload(acl_raw)
            if acl is not None:
                log.step(f"acl OK ({len(acl)} entry/entries)")
            else:
                stat_errors.append("acl: no response")
                log.step("acl: no response")
                if session is not None and session.is_authed(target.key):
                    session.clear_auth(target.key)
                    log.step("auth cleared (suspected ACL drop)")

        if "neighbors" in polled:
            neigh_raw = await retry_binary_req(
                "GET_NEIGHBOURS",
                lambda dest_wait: client.commands.fetch_all_neighbours(
                    target.pubkey_hex, timeout=dest_wait, min_timeout=8
                ),
                attempts=attempts,
                log=log,
                on_retry=flood_on_retry,
                session=session,
                target=target,
                wait_s=wait_cap,
                cap=cmd_timeout,
            )
            neighbors = normalize_neighbors_payload(neigh_raw)
            if neighbors is not None:
                log.step(f"neighbors OK ({len(neighbors)} node(s))")
            else:
                stat_errors.append("neighbors: no response")
                log.step("neighbors: no response")

        if "firmware" in polled:
            raw_ver = await send_cmd_sync(
                client, target, "ver", timeout=cmd_timeout, attempts=attempts, log=log, session=session
            )
            if raw_ver is None:
                stat_errors.append("firmware: no response")
                log.step("ver: no response")
            elif cli_suggests_auth_failure(raw_ver) or cli_error_reply(raw_ver):
                if session is not None and cli_suggests_auth_failure(raw_ver):
                    session.clear_auth(target.key)
                    log.step("auth cleared (CLI denied)")
                stat_errors.append("firmware: error")
                log.step("ver: error")
            else:
                fw, platform = parse_firmware(raw_ver)
                if not fw:
                    stat_errors.append("firmware: unparsed ver")
                    log.step("ver: unparsed")
                else:
                    log.step(f"ver OK ({fw})")

        if "bootloader" in polled:
            raw_bl = await send_cmd_sync(
                client,
                target,
                "get bootloader.ver",
                timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
            )
            if raw_bl is None:
                stat_errors.append("bootloader: no response")
                log.step("bootloader: no response")
            elif cli_suggests_auth_failure(raw_bl) or cli_error_reply(raw_bl):
                if session is not None and cli_suggests_auth_failure(raw_bl):
                    session.clear_auth(target.key)
                    log.step("auth cleared (CLI denied)")
                stat_errors.append("bootloader: error")
                log.step("bootloader: error")
            else:
                bl = parse_bootloader(raw_bl) or ""
                if bl:
                    log.step(f"bootloader OK ({bl})")
                else:
                    log.step(f"bootloader unknown ({raw_bl.strip()[:60]})")

        if "lat" in polled:
            want_lat = book_coord(node, "lat", sites)
            if want_lat is None:
                log.step("lat: skipped (no book position)")
            else:
                lat = await set_book_coord(
                    client,
                    target,
                    "lat",
                    want_lat,
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log,
                    session=session,
                )
                if lat is None:
                    stat_errors.append("lat: set failed")

        if "lon" in polled:
            want_lon = book_coord(node, "lon", sites)
            if want_lon is None:
                log.step("lon: skipped (no book position)")
            else:
                lon = await set_book_coord(
                    client,
                    target,
                    "lon",
                    want_lon,
                    cmd_timeout=cmd_timeout,
                    attempts=attempts,
                    log=log,
                    session=session,
                )
                if lon is None:
                    stat_errors.append("lon: set failed")

        if "advert" in polled:
            raw_advert = await send_cmd_sync(
                client,
                target,
                "get advert.interval",
                timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
            )
            if raw_advert is None:
                stat_errors.append("advert: no response")
                log.step("advert: no response")
            elif cli_error_reply(raw_advert):
                stat_errors.append("advert: error")
                log.step("advert: error")
            else:
                advert_min = parse_int_get_value(raw_advert)
                if advert_min is None:
                    advert_min = 0
                log.step(f"advert OK ({advert_min}m)")

        if "flood_advert" in polled:
            raw_flood_advert = await send_cmd_sync(
                client,
                target,
                "get flood.advert.interval",
                timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
            )
            if raw_flood_advert is None:
                stat_errors.append("flood_advert: no response")
                log.step("flood advert: no response")
            elif cli_error_reply(raw_flood_advert):
                stat_errors.append("flood_advert: error")
                log.step("flood advert: error")
            else:
                flood_h = parse_int_get_value(raw_flood_advert)
                if flood_h is None:
                    flood_h = 0
                log.step(f"flood advert OK ({flood_h}h)")

        if "path_hash" in polled:
            path_hash_mode = await set_path_hash_policy(
                client,
                target,
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
            )
            if path_hash_mode is None:
                stat_errors.append("path_hash: no response")
            elif not path_hash_matches_policy(path_hash_mode):
                stat_errors.append(f"path_hash: mode {path_hash_mode}")

        if "dutycycle" in polled:
            dutycycle = await set_dutycycle_policy(
                client,
                target,
                cmd_timeout=cmd_timeout,
                attempts=attempts,
                log=log,
                session=session,
                firmware_version=fw or node.get("firmware_version"),
            )
            if dutycycle is None:
                stat_errors.append("dutycycle: no response")
            elif not dutycycle_matches_policy(dutycycle):
                stat_errors.append(f"dutycycle: {dutycycle:g}%")

        position = None
        if lat is not None or lon is not None:
            position = {
                "lat": lat if lat is not None else 0.0,
                "lon": lon if lon is not None else 0.0,
            }

        return PollResult(
            target.key,
            ok=True,
            firmware_version=fw,
            bootloader_version=bl,
            firmware_platform=platform,
            raw_ver=raw_ver,
            raw_bl=raw_bl,
            node_clock=node_clock,
            status=status,
            name=live_name,
            owner=live_owner,
            lat=lat,
            lon=lon,
            position=position,
            telemetry=telemetry if isinstance(telemetry, list) else None,
            advert_interval_min=advert_min,
            flood_advert_interval_h=flood_h,
            path_hash_mode=path_hash_mode,
            dutycycle=dutycycle,
            acl=acl,
            neighbors=neighbors,
            polled_groups=polled,
            stat_errors=stat_errors,
        )
    except Exception as exc:  # pragma: no cover
        return PollResult(target.key, ok=False, error=str(exc), polled_groups=polled)


def _self_info_name(client: MeshCore) -> str | None:
    info = client._self_info or {}
    for key in ("name", "adv_name", "node_name"):
        val = info.get(key)
        if val:
            return str(val)
    return None


def _name_looks_like_meshcore(name: str | None) -> bool:
    if not name:
        return False
    return any(name.startswith(p) for p in MESH_NAME_PREFIXES)


def _device_adv_name(device: Any) -> str:
    name = getattr(device, "name", None) or ""
    meta = getattr(device, "metadata", None) or {}
    return name or str(meta.get("local_name") or "")


def _device_has_nus_service(device: Any) -> bool:
    meta = getattr(device, "metadata", None) or {}
    uuids = {str(u).lower() for u in (meta.get("uuids") or [])}
    return NUS_SERVICE_UUID in uuids


async def scan_ble_candidates(scan_timeout: float) -> list[CompanionCandidate]:
    try:
        from bleak import BleakScanner
    except ImportError:
        print("BLE scan skipped: bleak not installed", file=sys.stderr)
        return []

    print(f"Scanning BLE for MeshCore companions ({scan_timeout:.0f}s) …")
    seen: set[str] = set()
    out: list[CompanionCandidate] = []

    try:
        devices = await BleakScanner.discover(timeout=scan_timeout, service_uuids=[NUS_SERVICE_UUID])
    except TypeError:
        devices = await BleakScanner.discover(timeout=scan_timeout)

    for dev in devices:
        addr = dev.address
        if addr in seen:
            continue
        name = _device_adv_name(dev)
        if not _device_has_nus_service(dev) and not _name_looks_like_meshcore(name):
            continue
        seen.add(addr)
        out.append(
            CompanionCandidate(
                transport="ble",
                label=f"BLE {name or '?'} ({addr})",
                ble_address=addr,
                ble_device=dev,
            )
        )
    return out


def serial_port_candidates() -> list[str]:
    ports: list[str] = []
    for pattern in SERIAL_GLOB:
        ports.extend(str(p) for p in sorted(Path("/dev").glob(pattern)))
    return ports


async def try_open_companion(
    *,
    transport: str,
    verbose: bool,
    baud: int,
    ble_address: str | None = None,
    ble_device: Any = None,
    serial_port: str | None = None,
    tcp_host: str | None = None,
    tcp_port: int | None = None,
) -> tuple[MeshCore | None, str | None]:
    """Connect and verify companion responds to appstart. Returns (client, error)."""
    client: MeshCore | None = None
    try:
        if transport == "ble":
            client = await MeshCore.create_ble(
                address=ble_address,
                device=ble_device,
                auto_reconnect=True,
                max_reconnect_attempts=0,
                debug=verbose,
            )
        elif transport == "serial":
            if not serial_port:
                return None, "no serial port"
            client = await MeshCore.create_serial(
                serial_port,
                baudrate=baud,
                auto_reconnect=True,
                max_reconnect_attempts=0,
                debug=verbose,
            )
        elif transport == "tcp":
            if not tcp_host or tcp_port is None:
                return None, "no tcp target"
            client = await MeshCore.create_tcp(
                tcp_host,
                tcp_port,
                auto_reconnect=True,
                max_reconnect_attempts=0,
                debug=verbose,
            )
        else:
            return None, f"unknown transport {transport}"
        if client is None:
            return None, "no appstart response (not a companion?)"
        return client, None
    except Exception as exc:
        if client is not None:
            await client.disconnect()
        return None, str(exc)


async def probe_serial_candidates(baud: int, verbose: bool) -> list[CompanionCandidate]:
    out: list[CompanionCandidate] = []
    ports = serial_port_candidates()
    if not ports:
        return out
    print(f"Probing {len(ports)} serial port(s) for companion handshake …")
    for port in ports:
        client, err = await try_open_companion(transport="serial", verbose=verbose, baud=baud, serial_port=port)
        if client is None:
            if verbose:
                print(f"  serial {port}: skip ({err})")
            continue
        name = _self_info_name(client)
        await client.disconnect()
        out.append(
            CompanionCandidate(
                transport="serial",
                label=f"serial {port}" + (f" ({name})" if name else ""),
                serial_port=port,
                node_name=name,
            )
        )
    return out


async def discover_companions(args: argparse.Namespace) -> list[CompanionCandidate]:
    order = []
    if args.transport == "auto":
        order = ["ble", "serial"]
    else:
        order = [args.transport]

    found: list[CompanionCandidate] = []
    for kind in order:
        if kind == "ble":
            found.extend(await scan_ble_candidates(args.scan_timeout))
        elif kind == "serial":
            found.extend(await probe_serial_candidates(args.baud, args.verbose))
        elif kind == "tcp" and args.tcp:
            host, _, port_s = args.tcp.partition(":")
            if port_s:
                found.append(
                    CompanionCandidate(
                        transport="tcp",
                        label=f"tcp {host}:{port_s}",
                        tcp_host=host,
                        tcp_port=int(port_s),
                    )
                )
    return found


def candidate_from_args(args: argparse.Namespace) -> CompanionCandidate | None:
    if args.ble is not None:
        addr = args.ble.strip() or None
        label = f"BLE {addr or 'auto-scan'}"
        return CompanionCandidate(transport="ble", label=label, ble_address=addr)
    if args.serial:
        return CompanionCandidate(
            transport="serial",
            label=f"serial {args.serial}",
            serial_port=args.serial,
        )
    if args.tcp:
        host, _, port_s = args.tcp.partition(":")
        if not port_s:
            raise SystemExit("--tcp requires host:port")
        return CompanionCandidate(
            transport="tcp",
            label=f"tcp {args.tcp}",
            tcp_host=host,
            tcp_port=int(port_s),
        )
    return None


async def post_connect(client: MeshCore) -> None:
    """Shared post-connect setup: clock sync, auto-fetch, contact sync.

    Clock sync matters: the companion stamps every outgoing mesh packet with its own
    RTC, and repeaters silently drop packets whose timestamp is <= the last one seen
    from this client (replay protection). A tag with a stale clock gets every login
    and command dropped with no error.
    """
    res = await client.commands.set_time(int(time.time()))
    if res.type == EventType.ERROR:
        print(f"Warning: could not set companion clock: {res.payload}", file=sys.stderr)
    await client.start_auto_message_fetching()
    client.auto_update_contacts = True
    print("Syncing companion contacts …")
    await client.ensure_contacts(follow=True)
    print(f"Companion has {len(client.contacts)} contact(s)")
    cap = await ensure_companion_identity(client)
    if cap:
        print(f"Companion ACL prefix: {cap}")
    else:
        print(
            "Companion ACL prefix: unknown (SELF_INFO missing public_key)",
            file=sys.stderr,
        )


async def connect_candidate(args: argparse.Namespace, cand: CompanionCandidate) -> MeshCore:
    if cand.transport == "ble" and cand.ble_address is None and cand.ble_device is None:
        ble_list = await scan_ble_candidates(args.scan_timeout)
        if not ble_list:
            raise SystemExit("No BLE MeshCore companions found (NUS service scan)")
        if len(ble_list) > 1:
            print("Multiple BLE companions — pass --ble ADDRESS to pick one:", file=sys.stderr)
            for i, c in enumerate(ble_list, 1):
                print(f"  {i}. {c.label}", file=sys.stderr)
            raise SystemExit(2)
        cand = ble_list[0]

    print(f"Connecting via {cand.label} …")
    client, err = await try_open_companion(
        transport=cand.transport,
        verbose=args.verbose,
        baud=args.baud,
        ble_address=cand.ble_address,
        ble_device=cand.ble_device,
        serial_port=cand.serial_port,
        tcp_host=cand.tcp_host,
        tcp_port=cand.tcp_port,
    )
    if client is None:
        raise SystemExit(f"Failed to connect ({cand.label}): {err}")
    name = _self_info_name(client)
    if name:
        print(f"Companion ready: {name}")
    await post_connect(client)
    return client


async def probe_only(args: argparse.Namespace) -> int:
    explicit = candidate_from_args(args)
    if explicit:
        candidates = [explicit]
    else:
        candidates = await discover_companions(args)

    if not candidates:
        print("No companion candidates found.")
        print("  BLE: tag must run companion firmware (not repeater-only over USB CDC)")
        print("  Serial: only ports that respond to appstart count")
        return 1

    print(f"\nFound {len(candidates)} candidate(s):")
    any_ok = False
    for cand in candidates:
        client, err = await try_open_companion(
            transport=cand.transport,
            verbose=args.verbose,
            baud=args.baud,
            ble_address=cand.ble_address,
            ble_device=cand.ble_device,
            serial_port=cand.serial_port,
            tcp_host=cand.tcp_host,
            tcp_port=cand.tcp_port,
        )
        if client is None:
            print(f"  ✗ {cand.label} — {err}")
            continue
        name = _self_info_name(client) or cand.node_name or "?"
        print(f"  ✓ {cand.label} — companion OK ({name})")
        any_ok = True
        await client.disconnect()

    if explicit and any_ok:
        hint = {
            "ble": f"--ble {explicit.ble_address}" if explicit.ble_address else "--transport ble",
            "serial": f"--serial {explicit.serial_port}",
            "tcp": f"--tcp {explicit.tcp_host}:{explicit.tcp_port}",
        }.get(explicit.transport, "")
        if hint:
            print(f"\nUse: {Path(__file__).name} {hint}")
    return 0 if any_ok else 2


async def connect(args: argparse.Namespace) -> MeshCore:
    explicit = candidate_from_args(args)
    if explicit:
        return await connect_candidate(args, explicit)

    if args.transport == "tcp" and not args.tcp:
        raise SystemExit("--transport tcp requires --tcp host:port")

    candidates = await discover_companions(args)
    if not candidates:
        raise SystemExit(
            "No MeshCore companion found. Run with --probe to diagnose.\n"
            "BLE companion tags are not USB serial companions — use --transport ble (default auto)."
        )

    last_err = "none tried"
    for cand in candidates:
        client, err = await try_open_companion(
            transport=cand.transport,
            verbose=args.verbose,
            baud=args.baud,
            ble_address=cand.ble_address,
            ble_device=cand.ble_device,
            serial_port=cand.serial_port,
            tcp_host=cand.tcp_host,
            tcp_port=cand.tcp_port,
        )
        if client is not None:
            name = _self_info_name(client)
            print(f"Using {cand.label}" + (f" ({name})" if name else ""))
            await post_connect(client)
            return client
        last_err = err or "unknown"
        if args.verbose:
            print(f"  skip {cand.label}: {last_err}")

    raise SystemExit(f"No companion accepted connection. Last error: {last_err}. Try --probe.")


def strip_legacy_version_poll_notes(node: dict[str, Any]) -> None:
    """Move legacy ``version poll YYYY-MM-DD`` stamps out of notes into firmware_pulled_at."""
    notes = str(node.get("notes") or "")
    if not notes or "version poll" not in notes.lower():
        return
    epochs: list[int] = []
    for match in VERSION_POLL_NOTE_RE.finditer(notes):
        day = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        epochs.append(int(day.timestamp()))
    cleaned = VERSION_POLL_NOTE_RE.sub("", notes).strip(" —–-")
    if epochs:
        newest = max(epochs)
        prev = _firmware_pulled_at(node)
        if prev is None or int(prev) < newest:
            node["firmware_pulled_at"] = newest
    node["notes"] = cleaned or None


def apply_poll_to_node(node: dict[str, Any], res: PollResult, *, now: int) -> None:
    """Write successful poll fields into a node (only groups in polled_groups)."""
    groups = res.polled_groups
    if "firmware" in groups and res.firmware_version:
        node["firmware_version"] = res.firmware_version
        node["firmware_pulled_at"] = now
        if res.firmware_platform:
            node["firmware_platform"] = res.firmware_platform
    if "bootloader" in groups and res.bootloader_version is not None:
        node["bootloader_version"] = res.bootloader_version
        node["bootloader_pulled_at"] = now
    if "name" in groups and res.name is not None:
        node["name"] = res.name
        node["name_pulled_at"] = now
    if "name" in groups and res.owner is not None:
        node["owner"] = res.owner
    if "lat" in groups and res.lat is not None:
        node["lat"] = res.lat
        node["lat_pulled_at"] = now
    if "lon" in groups and res.lon is not None:
        node["lon"] = res.lon
        node["lon_pulled_at"] = now
    if res.node_clock is not None:
        node["node_clock"] = res.node_clock
    if "status" in groups and res.status:
        node["status"] = res.status
        node["status_pulled_at"] = now
    if "telemetry" in groups and res.telemetry is not None:
        node["telemetry"] = res.telemetry
        node["telemetry_pulled_at"] = now
    if "advert" in groups and res.advert_interval_min is not None:
        node["advert_interval_min"] = res.advert_interval_min
        node["advert_pulled_at"] = now
    if "flood_advert" in groups and res.flood_advert_interval_h is not None:
        node["flood_advert_interval_h"] = res.flood_advert_interval_h
        node["flood_advert_pulled_at"] = now
    if "path_hash" in groups and res.path_hash_mode is not None:
        node["path_hash_mode"] = res.path_hash_mode
        node["path_hash_pulled_at"] = now
    if "dutycycle" in groups and res.dutycycle is not None:
        node["dutycycle"] = store_dutycycle(res.dutycycle)
        node["dutycycle_pulled_at"] = now
    if "acl" in groups and res.acl is not None:
        node["acl"] = res.acl
        node["acl_pulled_at"] = now
    if "neighbors" in groups and res.neighbors is not None:
        node["neighbors"] = res.neighbors
        node["neighbors_pulled_at"] = now
    strip_legacy_version_poll_notes(node)


def log_poll_record(
    log_path: Path,
    target: RouterTarget,
    res: PollResult,
    *,
    dry_run: bool,
) -> None:
    """Append-only audit log — full pull payload, not the nodes.yaml summary."""
    if dry_run or not res.ok:
        return
    record: dict[str, Any] = {
        "event": "monitor",
        "ts": int(time.time()),
        "unit": res.key,
        "unit_id": target.unit_id,
        "site": target.site,
        "polled_groups": sorted(res.polled_groups),
        "firmware_version": res.firmware_version,
        "bootloader_version": res.bootloader_version,
        "firmware_platform": res.firmware_platform,
        "node_clock": res.node_clock,
        "status": res.status,
        "adv_name": res.name,
        "owner": res.owner,
        "lat": res.lat,
        "lon": res.lon,
        "position": res.position,
        "telemetry": res.telemetry,
        "advert_interval_min": res.advert_interval_min,
        "flood_advert_interval_h": res.flood_advert_interval_h,
        "path_hash_mode": res.path_hash_mode,
        "dutycycle": res.dutycycle,
        "acl": res.acl,
        "neighbors": res.neighbors,
        "raw_ver": res.raw_ver,
        "raw_bl": res.raw_bl,
    }
    if res.stat_errors:
        record["stat_errors"] = res.stat_errors
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def poll_summary(res: PollResult) -> str:
    parts: list[str] = []
    if "firmware" in res.polled_groups:
        parts.append(f"fw={res.firmware_version or '?'}")
    if "bootloader" in res.polled_groups:
        parts.append(f"bl={res.bootloader_version if res.bootloader_version else '?'}")
    if "name" in res.polled_groups and res.name:
        parts.append(f"name={res.name}")
    if "lat" in res.polled_groups and res.lat is not None:
        parts.append(f"lat={res.lat:.4f}")
    if "lon" in res.polled_groups and res.lon is not None:
        parts.append(f"lon={res.lon:.4f}")
    if "telemetry" in res.polled_groups and res.telemetry:
        parts.append(f"tele={len(res.telemetry)}")
    if res.node_clock is not None:
        parts.append(f"clock={res.node_clock}")
    if "status" in res.polled_groups and res.status:
        uptime = res.status.get("uptime_secs")
        bat = res.status.get("battery_mv")
        if uptime is not None:
            parts.append(f"uptime={uptime}s")
        if bat is not None:
            parts.append(f"bat={bat}mV")
    if "advert" in res.polled_groups and res.advert_interval_min is not None:
        parts.append(f"adv={res.advert_interval_min}m")
    if "flood_advert" in res.polled_groups and res.flood_advert_interval_h is not None:
        parts.append(f"flood={res.flood_advert_interval_h}h")
    if "path_hash" in res.polled_groups and res.path_hash_mode is not None:
        parts.append(f"hash={res.path_hash_mode}")
    if "dutycycle" in res.polled_groups and res.dutycycle is not None:
        parts.append(f"dc={res.dutycycle:g}%")
    if "acl" in res.polled_groups and res.acl is not None:
        parts.append(f"acl={len(res.acl)}")
    if "neighbors" in res.polled_groups and res.neighbors is not None:
        parts.append(f"nb={len(res.neighbors)}")
    if not parts:
        parts.append("ok")
    if res.stat_errors:
        parts.append(f"partial({len(res.stat_errors)})")
    return " ".join(parts)


def apply_results(nodes_path: Path, results: list[PollResult], *, dry_run: bool) -> None:
    doc = load_nodes_doc(nodes_path)
    nodes = doc.setdefault("nodes", {})
    now = int(time.time())

    for res in results:
        if not res.ok:
            continue
        node = nodes.get(res.key)
        if not node:
            continue
        normalize_fleet_node(node)
        apply_poll_to_node(node, res, now=now)

    for node in nodes.values():
        if isinstance(node, dict):
            normalize_fleet_node(node)

    if dry_run:
        return

    write_nodes_doc(nodes_path, doc)


def target_label(target: RouterTarget) -> str:
    site = f" @ {target.site}" if target.site else ""
    return f"{target.unit_id} {target.name}{site}"


async def _serve_web_until_stop(web_ctx: Any, poll_exit: int) -> int:
    print(f"Monitor UI still running at {web_ctx.url} (Ctrl+C to exit).")
    await web_ctx.start_yaml_watch()
    try:
        await web_ctx.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await web_ctx.shutdown()
    return poll_exit


async def run(args: argparse.Namespace) -> int:
    web_ctx: Any | None = None
    session_states: dict[str, dict[str, Any]] = {}
    use_web = not args.no_web

    if use_web:
        from envybot.web.server import start_monitor_web

        web_ctx = await start_monitor_web(
            nodes_path=args.nodes,
            host=args.bind,
            port=args.port,
            stale_secs=args.min_interval,
            open_browser=args.open,
        )

    if args.web_only:
        if web_ctx is None:
            print("envybot monitor: --web-only conflicts with --no-web", file=sys.stderr)
            return 2
        print("Web-only mode — watching nodes.yaml. Ctrl+C to exit.")
        return await _serve_web_until_stop(web_ctx, 0)

    include = {u.lower() for u in args.unit} if args.unit else None
    skip = {u.lower() for u in args.skip} if args.skip else None
    all_targets = load_targets(
        args.nodes, deployed_only=not args.all_units, include=include, skip=skip
    )
    if skip and not args.quiet:
        print(f"Excluding {len(skip)} unit(s): {', '.join(sorted(skip))}")
    if not all_targets:
        print("No pollable routers matched filters.", file=sys.stderr)
        if web_ctx:
            await web_ctx.refresh_snapshot(poll={"phase": "idle", "error": "no targets"})
            return await _serve_web_until_stop(web_ctx, 1)
        return 1

    policy = PullPolicy(
        force=args.force,
        live_only=args.live and not args.force,
        force_groups=normalize_force_groups(args.group),
        min_interval=args.min_interval,
    )
    nodes_doc = load_nodes_doc(args.nodes)
    nodes = nodes_doc.get("nodes") or {}
    sites = load_sites(Path(args.nodes).parent / "sites.yaml")
    now = int(time.time())
    targets, skipped = partition_due_targets(
        all_targets, nodes, policy=policy, now=now, sites=sites
    )
    if skipped and not args.quiet:
        interval_label = format_interval(policy.min_interval)
        print(
            f"Skipping {len(skipped)} up-to-date router(s) "
            f"(inventory complete; periodic groups within {interval_label}; "
            f"use --force / --live / --group)"
        )
        for target in skipped:
            print(f"  skip {target_label(target)}")
    for target in targets:
        session_states[target.key] = {
            "state": "queued",
            "due_groups": list(target.due_groups),
        }
    if web_ctx:
        await web_ctx.refresh_snapshot(
            session_states=session_states,
            poll={
                "phase": "starting",
                "pending": len(targets),
                "total": len(all_targets),
            },
        )
    if not targets:
        print(f"All {len(all_targets)} router(s) up to date.")
        if web_ctx:
            await web_ctx.publish_session(
                {"phase": "done", "pending": 0, "ok": 0, "total": len(all_targets)}
            )
            return await _serve_web_until_stop(web_ctx, 0)
        return 0

    retry_mode = not args.once
    print(f"Polling {len(targets)} router(s)" + (
        " — retries until all succeed (Ctrl+C to stop) …" if retry_mode else " — single pass …"
    ))

    log = PollLog(progress=not args.quiet, verbose=args.verbose)
    session = FleetSession()
    client = await connect(args)
    companion_label = companion_identity(client)
    companion_short = companion_label[:12] if companion_label else None
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)
    await sync_fleet_contacts(client, targets, log=log)
    pending: list[RouterTarget] = list(targets)
    succeeded: dict[str, PollResult] = {}
    attempt_counts: dict[str, int] = {}
    round_num = 0
    interrupted = False

    if web_ctx:
        await web_ctx.refresh_snapshot(
            session_states=session_states,
            companion=companion_short,
            poll={"phase": "polling", "round": 0, "pending": len(pending)},
        )

    try:
        while pending:
            round_num += 1
            if args.max_rounds and round_num > args.max_rounds:
                break
            if retry_mode and round_num > 1:
                print(f"\n--- round {round_num}: {len(pending)} still pending ---")
            if web_ctx:
                await web_ctx.publish_session(
                    {
                        "phase": "polling",
                        "round": round_num,
                        "pending": len(pending),
                        "companion": companion_short,
                    }
                )
            next_pending: list[RouterTarget] = []
            for target in pending:
                attempt_counts[target.key] = attempt_counts.get(target.key, 0) + 1
                n = attempt_counts[target.key]
                if args.max_attempts and n > args.max_attempts:
                    print(f"[{target.key}] gave up after {args.max_attempts} attempts")
                    continue
                prefix = f"[{n}] " if retry_mode and n > 1 else ""
                print(f"{prefix}{target_label(target)} …", flush=True)
                node_record = nodes.get(target.key) or {}
                if n == 1:
                    need, skip_plan = format_pull_plan(
                        node_record,
                        target.due_groups,
                        policy=policy,
                        now=int(time.time()),
                        sites=sites,
                    )
                    print(f"  need: {need}")
                    print(f"  skip: {skip_plan}")
                    print("  login: always (live clock + path)")
                session_states[target.key] = {
                    "state": "polling",
                    "due_groups": list(target.due_groups),
                }
                if web_ctx:
                    await web_ctx.publish_unit(
                        target.key,
                        session=session_states[target.key],
                        session_states=session_states,
                        companion=companion_short,
                        poll={
                            "phase": "polling",
                            "round": round_num,
                            "pending": len(pending),
                            "unit": target.key,
                        },
                    )
                res = await poll_one(
                    client,
                    target,
                    node=node_record,
                    due_groups=frozenset(target.due_groups),
                    cmd_timeout=args.timeout,
                    login_timeout=args.login_timeout,
                    attempts=args.attempts,
                    session=session,
                    log=log,
                    sites=sites,
                )
                if res.ok:
                    succeeded[target.key] = res
                    apply_results(args.nodes, [res], dry_run=args.dry_run)
                    log_poll_record(args.log_file, target, res, dry_run=args.dry_run)
                    fresh_doc = load_nodes_doc(args.nodes)
                    fresh_nodes = fresh_doc.get("nodes") or {}
                    fresh_node = fresh_nodes.get(target.key) or {}
                    if fresh_node:
                        nodes[target.key] = fresh_node
                    remaining = gaps_from_poll_result(res)
                    if remaining:
                        target.due_groups = remaining
                        next_pending.append(target)
                    else:
                        target.due_groups = fleet_due_groups(
                            fresh_node, policy=policy, now=int(time.time()), sites=sites
                        )
                    if res.name:
                        target.name = res.name
                    session_states[target.key] = {
                        "state": "ok",
                        "due_groups": list(target.due_groups),
                    }
                    print(f"  OK {poll_summary(res)}")
                else:
                    session_states[target.key] = {
                        "state": "unreachable",
                        "error": res.error,
                        "due_groups": list(target.due_groups),
                    }
                    print(f"  unreachable: {res.error}")
                    next_pending.append(target)
                    if args.retry_delay > 0:
                        await asyncio.sleep(args.retry_delay)
                if web_ctx:
                    await web_ctx.publish_unit(
                        target.key,
                        session=session_states[target.key],
                        session_states=session_states,
                        companion=companion_short,
                        poll={
                            "phase": "polling",
                            "round": round_num,
                            "pending": len(next_pending) + len(pending) - 1,
                            "unit": target.key,
                        },
                    )
            pending = sorted(next_pending, key=poll_staleness_key)
            if not pending or args.once:
                break
            if args.round_delay > 0:
                print(f"Waiting {args.round_delay:.0f}s before retrying {len(pending)} node(s) …")
                await asyncio.sleep(args.round_delay)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — keeping progress from successful polls.")
    finally:
        await client.stop_auto_message_fetching()
        await client.disconnect()

    total = len(targets)
    ok_count = len(succeeded)
    if args.dry_run:
        print(f"Dry run: polled {ok_count}/{total} successfully")
    elif ok_count:
        print(f"nodes.yaml updated for {ok_count}/{total} router(s)")
        if not args.dry_run:
            print(f"Stats logged to {args.log_file}")
    if pending:
        names = ", ".join(t.unit_id for t in pending)
        print(f"Still pending ({len(pending)}): {names}")
    poll_exit = 130 if interrupted else (0 if not pending else 2)
    if web_ctx:
        await web_ctx.publish_session(
            {
                "phase": "done",
                "ok": ok_count,
                "pending": len(pending),
                "total": total,
                "round": round_num,
                "companion": companion_short,
            }
        )
        return await _serve_web_until_stop(web_ctx, poll_exit)
    return poll_exit


def add_companion_args(parser: argparse.ArgumentParser) -> None:
    """Companion transport + mesh wait flags shared by monitor and cmd."""
    parser.add_argument("--serial", help="Force USB serial companion port (must pass appstart probe)")
    parser.add_argument("--tcp", help="Companion TCP host:port")
    parser.add_argument(
        "--ble",
        nargs="?",
        const="",
        default=None,
        metavar="ADDRESS",
        help="BLE companion (optional MAC; omit value to auto-pick)",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "ble", "serial", "tcp"),
        default="auto",
        help="Companion discovery order (default: auto = BLE then serial probe)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=4.0,
        help="BLE scan duration in seconds (default: 4; use --ble ADDRESS to skip)",
    )
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Max per-command wait in seconds (default: 30; uses companion suggested_timeout when lower)",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=45,
        help="Max login wait per attempt in seconds (default: 45)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=DEFAULT_MESH_ATTEMPTS,
        help="Retries per send that expects a reply (login, CLI, binary, clock; 0 = unlimited; default: 10)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Extra protocol/debug detail (prefixes, payloads, ignored msgs)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help="Append JSONL poll snapshots here (default: data/fleet/polls.jsonl)",
    )
    add_companion_args(parser)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="List companion candidates and verify handshake; do not poll fleet",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.0,
        help="Seconds to wait after a failed node before trying the next (default: 0)",
    )
    parser.add_argument(
        "--round-delay",
        type=float,
        default=0.0,
        help="Seconds to wait between full retry rounds (default: 0)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="Stop after N rounds (0 = unlimited until all succeed)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="Give up on a node after N tries (0 = unlimited)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single pass only — do not retry unreachable nodes",
    )
    parser.add_argument("--all-units", action="store_true", help="Include bag/bench units (site null)")
    parser.add_argument(
        "--unit",
        action="append",
        metavar="me0003",
        help="Poll only this unit key (repeatable; does not imply --force)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        metavar="me0001",
        help="Do not poll this unit key (repeatable; e.g. --skip me0001 --skip me0006)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_POLL_INTERVAL,
        metavar="SECS",
        help="Re-pull periodic groups (status, telemetry, neighbors, acl) when older than this (default: 86400 = 24h)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Poll every group on every matching router",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Poll only periodic groups (status, telemetry, neighbors, acl); skip inventory unless incomplete",
    )
    parser.add_argument(
        "--group",
        action="append",
        choices=PULL_GROUP_ORDER,
        metavar="GROUP",
        help="Force this query (repeatable). lat/lon SET book coords; path_hash/dutycycle SET policy",
    )
    parser.add_argument("--dry-run", action="store_true", help="Poll but do not write nodes.yaml")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="No per-step progress (only node lines and OK/unreachable)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable local web UI (poll and exit)",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Serve book UI without polling radios",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Web UI bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Web UI port (default: 8787)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open browser to the web UI on start",
    )
    args = parser.parse_args(argv)

    if args.probe:
        return asyncio.run(probe_only(args))

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
