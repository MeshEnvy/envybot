"""Companion transport + mesh CLI/binary session for fleet poll and apply."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from envybot.keys_doc import find_person_for_pubkey, keys_path, load_keys
from envybot.nodes_doc import (
    HEX_PUBKEY_RE,
    PLACEHOLDER_PW,
    UNIT_NUM_RE,
    is_decommissioned,
    is_meshcore_platform,
    load_nodes_doc,
    normalize_fleet_node,
)
from envybot.position import book_coord, display_name, load_sites, site_binding
from envybot.history import begin_mesh_audit, finish_mesh_audit, mark_mesh_audit_late
from envybot.routing import (
    FLEET_PATH_HASH_MODE,
    RouteSession,
    RoutingMode,
    contact_out_path_label,
    contact_route_audit_label,
    has_cached_route,
    live_route_from_contact,
    persist_route_session,
    resolve_routing,
    routing_explicit,
    route_session_from_extra,
)

try:
    from meshcore import EventType, MeshCore
    from meshcore.parsing import parse_acl
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "meshcore not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot fleet"
    ) from exc

VER_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)\s*\(Build:", re.I)
BL_RE = re.compile(r"^>\s*(.+)$")
OTA_SELF_HASH_RE = re.compile(r"base_hash=([0-9A-Fa-f]{16})", re.I)
VERSION_POLL_NOTE_RE = re.compile(r"(?:\s*[—–-]\s*)?version poll (\d{4}-\d{2}-\d{2})", re.I)
CLOCK_CLI_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*UTC", re.I
)

DEFAULT_MIN_POLL_INTERVAL = 3600.0  # status + telemetry
NEIGHBOR_POLL_INTERVAL = 86400.0  # discover is airtime-heavy
OTA_POLL_INTERVAL = 86400.0  # ota status + ls; auto + Pull, not Refresh
AUDIT_POLL_INTERVAL = 7 * 86400.0  # name/gps/advert/acl reconcile GETs
AUDIT_GET_ATTEMPTS = 3
NEIGHBOR_DISCOVER_WAIT_S = 12.0  # after remote discover.neighbors, before GET
OTA_LS_WAIT_S = 5.0  # after first `ota ls` (queryAll), before second catalog read
POST_INSTALL_WAIT_S = 20.0  # after `ota install`, before liveness GET
DEFAULT_MESH_ATTEMPTS = 10
COMPANION_RECONNECT_ATTEMPTS = 5
CLOCK_SKEW_MAX = 300  # seconds; sync when *live login* RTC vs host exceeds this
FLEET_DUTYCYCLE_PCT = 50.0
FLEET_OTA_AUTOFETCH = "off"
FLEET_FEM_RXGAIN = True
FLEET_AGC_RESET_INTERVAL = 4
OTA_AUTOFETCH_VALUES = frozenset({"off", "any", "signed"})
OTA_AUTOFETCH_RE = re.compile(r"autofetch=(off|any|signed)", re.I)
DUTYCYCLE_MATCH_EPS = 0.5
DUTYCYCLE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
FIRMWARE_CORE_RE = re.compile(r"v?(\d+(?:\.\d+)*)", re.I)
DUTYCYCLE_CLI_SINCE = (1, 15)
ADMIN_PASSWORD_NOW_RE = re.compile(r"^password now:\s*(.*)\s*$", re.I)

# Decision notes (do not reintroduce the opposite without updating this):
# - Login: path mode flood-discovers when cache empty; direct/flood policies
#   force zero-hop or flood every send. GET/CLI/binary ride cached route in path
#   mode. Timeout on cached path (3x) discards cache and re-floods.
#   Book ACL admin is not enough: remote repeaters need the login handshake
#   to register the companion and refresh mesh paths. Live RTC comes from
#   LOGIN_SUCCESS timestamp or ``clock`` CLI. STATUS is uptime, not wall
#   clock.
# - Clock set is one CLI: ``time <host epoch>``. Drift is vs host, so set from
#   host, not companion RTC (``clock sync``). Only from a live clock
#   (login timestamp or ``clock``), or stored clock that is unset (0 / pre-2020).
#   Log drift before sending.
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
#   hashes; firmware sendFlood uses mode+1), dutycycle=50 (stock MeshCore),
#   and GPS from the book. SET only; stamp on OK. Never GET lat/lon into
#   nodes.yaml. MeshCore <1.15 has no set dutycycle; use set af 1
#   (50% = af 1.0) and stamp. Seeder stays at SEEDER_DUTYCYCLE_PCT (10).
# - --unit filters targets only; it does not imply --full-sync.
# - Log a one-line result as soon as GET_STATUS, GET_TELEMETRY, or a CLI
#   command succeeds (same beat as login OK). Failures stay on the send line.


AUTH_FAILURE_HINTS = ("login required", "not authorized", "not authorised", "denied")

_INVENTORY_POLL_GROUPS = frozenset({"firmware", "bootloader", "ota"})


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
    audit_id: int | None = None


@dataclass
class FleetSession:
    """Per-run poll state. Login is always sent; this tracks companion + auth drops."""

    authed_units: set[str] = field(default_factory=set)
    companion_id: str | None = None
    companion_acl_prefix: str | None = None
    expects: list[ReplyExpect] = field(default_factory=list)
    wait_slack: dict[str, float] = field(default_factory=dict)
    conn: sqlite3.Connection | None = field(default=None, repr=False)
    _orphan_log: PollLog | None = None
    _orphan_subs: list[Any] = field(default_factory=list)
    _binary_inflight: dict[str, Any] | None = None
    _register_binary_orig: Any = None
    _recovery_client: MeshCore | None = field(default=None, repr=False)
    _recovery_targets: list[Any] = field(default_factory=list, repr=False)
    audit_source: str | None = None

    def enable_companion_recovery(self, client: MeshCore, targets: list[Any]) -> None:
        """After BLE/USB drop, reconnect transport + re-sync fleet contacts."""
        self._recovery_client = client
        self._recovery_targets = list(targets)

    async def ensure_companion_connected(self, *, log: PollLog) -> bool:
        client = self._recovery_client
        if client is None:
            return True
        if client.is_connected:
            return True
        if not self._recovery_targets:
            return False
        return await recover_companion(
            client, session=self, targets=self._recovery_targets, log=log
        )

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
        audit_id: int | None = None,
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
            "audit_id": audit_id,
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
        exp.audit_id = inf.get("audit_id")
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
            audit_reply = None
            if kind == "cli" and isinstance(event.payload, dict):
                raw = str(event.payload.get("text") or "")
                if raw:
                    audit_reply = _audit_redact(raw, max_len=40)
                    extra = f" {audit_reply!r}"
            elif kind == "binary" and exp.binary_tag:
                extra = f" tag={exp.binary_tag}"
                audit_reply = extra.strip()
            log.step(
                f"orphan: {exp.unit} late {exp.label} {exp.n_of} "
                f"+{late:.1f}s after timeout{extra}"
            )
            if self.conn is not None and exp.audit_id is not None:
                mark_mesh_audit_late(self.conn, exp.audit_id, reply=audit_reply)
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
            if self.conn is not None and exp.audit_id is not None:
                mark_mesh_audit_late(self.conn, exp.audit_id, reply="login")
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
    pubkey_hex: str | None = None
    keys_person: str | None = None
    probe_error: str | None = None


@dataclass
class RouterTarget:
    key: str
    unit_id: str
    name: str
    site: str | None
    pubkey_hex: str
    admin_password: str
    due_groups: list[str] = field(default_factory=list)
    routing: RoutingMode = RoutingMode.PATH
    routing_explicit: str | None = None


def poll_staleness_key(target: RouterTarget) -> tuple[int, int, str]:
    """Due groups first; inventory gaps before live-only refresh."""
    if not target.due_groups:
        return (2, 0, target.key)
    has_inventory = any(g in _INVENTORY_POLL_GROUPS for g in target.due_groups)
    return (0 if has_inventory else 1, len(target.due_groups), target.key)


@dataclass
class PollResult:
    key: str
    ok: bool
    firmware_version: str | None = None
    bootloader_version: str | None = None
    base_hash: str | None = None
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
    ota: dict[str, Any] | None = None
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
    sites = load_sites(nodes_path.parent / "sites.yaml")
    out: list[RouterTarget] = []

    for key, node in nodes.items():
        if include and key not in include:
            continue
        if skip and key in skip:
            continue
        unit_id = str(node.get("unit_id") or key.upper())
        bind = site_binding(key, node, sites)
        site = bind[0] if bind else None
        if is_decommissioned(node):
            continue
        if not is_meshcore_platform(node):
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
                name=display_name(key, node, sites),
                site=site,
                pubkey_hex=pubkey.strip().lower(),
                admin_password=admin_pw,
                routing=resolve_routing(node),
                routing_explicit=routing_explicit(node),
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


def ota_self_heard_empty(text: str) -> bool:
    """True when the radio answered but has no OTA identity to report."""
    from envybot.ota_parse import ota_cli_missing

    if ota_cli_missing(text):
        return True
    lower = text.strip().lower()
    if lower.startswith("err no endf"):
        return True
    return False


def parse_ota_self(text: str) -> str | None:
    """Return 16-char hex base_hash, '' if OTA absent/no EndF, None if unparsed."""
    if not text:
        return None
    stripped = text.strip()
    if ota_self_heard_empty(stripped):
        return ""
    match = OTA_SELF_HASH_RE.search(stripped)
    if match:
        return match.group(1).upper()
    return None


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


def parse_ota_autofetch(text: str | None) -> str | None:
    """Parse ``autofetch=off|any|signed`` from ``ota config`` GET or status."""
    if not text:
        return None
    m = OTA_AUTOFETCH_RE.search(text)
    if not m:
        return None
    return m.group(1).lower()


def normalize_ota_autofetch(value: Any) -> str:
    val = str(value or "").strip().lower()
    if val in OTA_AUTOFETCH_VALUES:
        return val
    return FLEET_OTA_AUTOFETCH


def cli_set_ok(text: str | None) -> bool:
    if not text or cli_error_reply(text):
        return False
    return "ok" in text.lower()


def cli_admin_password_ok(text: str | None, expected: str | None = None) -> bool:
    """MeshCore admin ``password`` replies with ``password now: <pw>`` (not ``ok``)."""
    if not text or cli_error_reply(text):
        return False
    m = ADMIN_PASSWORD_NOW_RE.match(text.strip())
    if not m:
        return cli_set_ok(text)
    if expected is None:
        return True
    from envybot.passwords import normalize_password

    return normalize_password(m.group(1)) == normalize_password(expected)


def cli_unknown_reply(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    return (
        "unknown config" in lower
        or "unknown ota command" in lower
        or "unknown command" in lower
    )


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


def _fmt_uptime(secs: Any) -> str:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return str(secs)
    if s >= 86400:
        return f"{s / 86400:.1f}d"
    if s >= 3600:
        return f"{s / 3600:.1f}h"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def _telem_ok_detail(items: Any) -> str:
    temp = None
    volt = None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            val = item.get("value")
            if val is None:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if kind == "temperature":
                temp = num
            elif kind == "voltage":
                volt = num
        n = len(items)
    else:
        n = 1
    parts: list[str] = []
    if temp is not None:
        parts.append(f"{temp:.1f}°C")
    if volt is not None:
        parts.append(f"{volt:.2f}V")
    if parts:
        return ", ".join(parts)
    return f"{n} reading(s)"


def format_binary_ok(label: str, result: Any) -> str:
    """One-line progress after a successful binary GET."""
    extra = ""
    if label == "GET_STATUS":
        raw = result if isinstance(result, dict) else None
        st = normalize_status_payload(raw)
        if st is None and raw and raw.get("uptime_secs") is not None:
            st = raw
        if st:
            bat = st.get("battery_mv")
            bat_s = f"{bat / 1000:.2f}V" if bat is not None else "—"
            extra = (
                f"up={_fmt_uptime(st.get('uptime_secs'))} bat={bat_s} "
                f"rx={st.get('packets_recv')} tx={st.get('packets_sent')}"
            )
    elif label == "GET_TELEMETRY":
        extra = _telem_ok_detail(result)
    elif label == "GET_ACL":
        acl = normalize_acl_payload(result)
        if acl is not None:
            extra = f"{len(acl)} entries"
    elif label == "GET_NEIGHBOURS":
        neighbors = normalize_neighbors_payload(result if isinstance(result, dict) else None)
        if neighbors is None and isinstance(result, list):
            neighbors = result
        if neighbors is not None:
            extra = f"{len(neighbors)} nodes"
    if extra:
        return f"{label} OK ({extra})"
    return f"{label} OK"


def format_cli_ok(cmd: str, text: str) -> str:
    """One-line progress after a successful CLI reply (passwords redacted)."""
    shown_cmd = _audit_redact(cmd, max_len=60) or cmd
    snippet = _audit_redact(" ".join(text.split()), max_len=80)
    if snippet:
        return f"{shown_cmd} OK ({snippet})"
    return f"{shown_cmd} OK"


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


def parse_clock_cli(text: str | None) -> int | None:
    """Parse CommonCLI ``clock`` (``HH:MM - D/M/Y UTC``) to epoch seconds."""
    if not text:
        return None
    match = CLOCK_CLI_RE.search(text)
    if not match:
        return None
    hour, minute, day, month, year = (int(p) for p in match.groups())
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def format_clock_drift(node_clock: int, *, now: int) -> str:
    """Live RTC minus host. Positive = node ahead."""
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
    mode: int | None = None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> int | None:
    """``set path.hash.mode`` (default 1 = 2-byte). Stamp on OK."""
    want = FLEET_PATH_HASH_MODE if mode is None else int(mode)
    raw = await send_cmd_sync(
        client,
        target,
        f"set path.hash.mode {want}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
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
    log.step(f"path.hash set OK ({want} = {want + 1}-byte)")
    return want


class OtaAutofetchUnsupported(Exception):
    """Firmware has no ``ota config autofetch`` CLI."""


class FemRxgainUnsupported(Exception):
    """Board or firmware has no ``set radio.fem.rxgain``."""


class AgcResetUnsupported(Exception):
    """Firmware has no ``set agc.reset.interval`` CLI."""


def fem_rxgain_cli_missing(text: str | None) -> bool:
    """RAK ``unsupported`` or older MeshCore ``unknown config``."""
    if not text:
        return False
    return "unsupported" in text.lower() or cli_unknown_reply(text)


def _cli_on_off_ok(text: str | None, enabled: bool) -> bool:
    if not text or cli_error_reply(text):
        return False
    low = text.strip().lower()
    if enabled:
        return low.startswith("on") or "rx gain on" in low
    return low.startswith("off") or "rx gain off" in low


async def set_powersaving_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    enabled: bool | None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> bool | None:
    """``powersaving on|off``. Reply is ``on - Immediate effect``, not ``OK``."""
    if enabled is None:
        return None
    word = "on" if enabled else "off"
    raw = await send_cmd_sync(
        client,
        target,
        f"powersaving {word}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("powersaving: no response")
        return None
    if not _cli_on_off_ok(raw, enabled):
        log.step(f"powersaving: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"powersaving set OK ({word})")
    return enabled


async def set_fem_rxgain_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    enabled: bool | None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> bool | None:
    """``set radio.fem.rxgain on|off``. Raises FemRxgainUnsupported if missing."""
    if enabled is None:
        return None
    word = "on" if enabled else "off"
    raw = await send_cmd_sync(
        client,
        target,
        f"set radio.fem.rxgain {word}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("fem.rxgain: no response")
        return None
    if fem_rxgain_cli_missing(raw):
        raise FemRxgainUnsupported()
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"fem.rxgain: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"fem.rxgain set OK ({word})")
    return enabled


async def set_rxgain_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    enabled: bool | None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> bool | None:
    """``set radio.rxgain on|off``. Raises FemRxgainUnsupported if missing."""
    if enabled is None:
        return None
    word = "on" if enabled else "off"
    raw = await send_cmd_sync(
        client,
        target,
        f"set radio.rxgain {word}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("radio.rxgain: no response")
        return None
    if fem_rxgain_cli_missing(raw):
        raise FemRxgainUnsupported()
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"radio.rxgain: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"radio.rxgain set OK ({word})")
    return enabled


async def push_flood_advert(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> bool | None:
    """Send ``advert`` (flood) after identity SETs. None on timeout."""
    raw = await send_cmd_sync(
        client,
        target,
        "advert",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("push_advert: no response")
        return None
    if cli_error_reply(raw):
        log.step(f"push_advert: failed ({raw.strip()[:40]})")
        return False
    log.step("push_advert: flood advert sent")
    return True


async def set_agc_reset_interval_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    interval: int | None = None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> int | None:
    """``set agc.reset.interval <secs>``. Raises AgcResetUnsupported if missing."""
    if interval is None:
        return None
    want = max(0, int(interval))
    raw = await send_cmd_sync(
        client,
        target,
        f"set agc.reset.interval {want}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("agc.reset: no response")
        return None
    if cli_unknown_reply(raw) or fem_rxgain_cli_missing(raw):
        log.step("agc.reset: unsupported")
        raise AgcResetUnsupported()
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"agc.reset: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"agc.reset set OK ({want})")
    return want


async def set_ota_autofetch_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    mode: str | None = None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> str | None:
    """``ota config autofetch off|any|signed``. Stamp on OK.

    Raises OtaAutofetchUnsupported when the CLI is missing.
    """
    want = normalize_ota_autofetch(mode)
    raw = await send_cmd_sync(
        client,
        target,
        f"ota config autofetch {want}",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
    )
    if raw is None:
        log.step("ota autofetch: no response")
        return None
    if cli_unknown_reply(raw):
        log.step("ota autofetch: unsupported")
        raise OtaAutofetchUnsupported()
    if cli_error_reply(raw) or not cli_set_ok(raw):
        log.step(f"ota autofetch: set failed ({raw.strip()[:40]})")
        return None
    log.step(f"ota autofetch set OK ({want})")
    return want


async def set_dutycycle_policy(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
    firmware_version: str | None = None,
    pct: float | None = None,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
) -> float | None:
    """``set dutycycle``, or ``set af`` on MeshCore <1.15. Stamp on OK."""
    want_pct = FLEET_DUTYCYCLE_PCT if pct is None else float(pct)
    want = int(round(want_pct))
    af = airtime_factor_for_dutycycle(want_pct)
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
            attempt_num=attempt_num,
            attempt_cap=attempt_cap,
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
            return float(want)
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
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
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
    return float(want)


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
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
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
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
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
        client=client,
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
    """Sync from a live clock (login timestamp or ``clock`` CLI), or unset stored RTC.

    STATUS has uptime only. Stored ``node_clock`` is a last-pull snapshot:
    do not treat its age vs host as skew.
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


def publish_live_route(
    client: MeshCore,
    target: RouterTarget,
    route_extra: dict[str, Any] | None,
) -> dict[str, Any]:
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    live = live_route_from_contact(contact, policy=target.routing)
    if route_extra is not None:
        route_extra["live_route"] = live
    return live


def log_contact_path(
    client: MeshCore, target: RouterTarget, *, log: PollLog | None = None
) -> None:
    """Log the companion's cached route for this target before a mesh send.

    ``out_path`` is learned when a flood login succeeds and the repeater
    returns a path. Later GET/CLI/binary sends ride that route until reset
    or a timeout retry falls back to flood.
    """
    log = log or PollLog()
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    log.step(f"path: {contact_route_audit_label(contact)}")


def _audit_redact(text: str | None, *, max_len: int = 500) -> str | None:
    if text is None:
        return None
    from envybot.commands.cmd import redact_snippet

    return redact_snippet(text, max_len=max_len)


def audit_path_at_send(client: MeshCore, target: RouterTarget) -> str:
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    return contact_route_audit_label(contact)


def _audit_binary_reply(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        text = json.dumps(val, default=str)
    else:
        text = str(val)
    return _audit_redact(text, max_len=500)


def _audit_begin(
    session: FleetSession | None,
    *,
    unit: str,
    kind: str,
    label: str,
    attempt: int,
    path: str,
    wait_s: float | None,
) -> int | None:
    if session is None or session.conn is None:
        return None
    return begin_mesh_audit(
        session.conn,
        unit=unit,
        kind=kind,
        label=_audit_redact(label) if label else label,
        attempt=attempt,
        path=path,
        wait_s=wait_s,
        source=session.audit_source,
    )


def _audit_finish(
    session: FleetSession | None,
    audit_id: int | None,
    *,
    ok: bool,
    outcome: str,
    reply: str | None = None,
    error: str | None = None,
) -> None:
    if session is None or session.conn is None or audit_id is None:
        return
    finish_mesh_audit(
        session.conn,
        audit_id,
        ok=ok,
        outcome=outcome,
        reply=_audit_redact(reply) if reply else None,
        error=str(error) if error is not None else None,
    )


def route_path_fields(*, flood: bool) -> dict[str, Any]:
    if flood:
        return {"out_path_len": -1, "out_path_hash_mode": -1, "out_path": ""}
    return {"out_path_len": 0, "out_path_hash_mode": 0, "out_path": ""}


def _default_out_path_fields(target: RouterTarget) -> dict[str, Any]:
    if target.routing is RoutingMode.DIRECT:
        return route_path_fields(flood=False)
    return route_path_fields(flood=True)


def pin_contact_route(contact: dict[str, Any], *, flood: bool) -> None:
    """Force cache path fields. Device snapshots can overwrite after a SET."""
    contact.update(route_path_fields(flood=flood))


def contact_stub_for_target(target: RouterTarget) -> dict[str, Any]:
    return {
        "public_key": target.pubkey_hex.lower(),
        "type": CONTACT_TYPE_REPEATER,
        "flags": CONTACT_FLAG_FAVORITE,
        **_default_out_path_fields(target),
        "adv_name": contact_display_name(target),
        "last_advert": 0,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
    }


def flood_contact_stub(target: RouterTarget) -> dict[str, Any]:
    """Alias for contact_stub_for_target (historical name)."""
    return contact_stub_for_target(target)


def pop_companion_contact(client: MeshCore, pubkey: str) -> None:
    pk = pubkey.strip().lower()
    contacts = getattr(client, "contacts", None)
    if not isinstance(contacts, dict):
        return
    for key in list(contacts):
        raw = contacts.get(key)
        key_pk = str(key).strip().lower()
        stored = ""
        if isinstance(raw, dict):
            stored = str(raw.get("public_key") or "").strip().lower()
        if key_pk == pk or stored == pk:
            contacts.pop(key, None)


async def drop_companion_contact(
    client: MeshCore, pubkey: str, *, log: PollLog, unit_id: str
) -> bool:
    pk = pubkey.strip().lower()
    res = await client.commands.remove_contact(pk)
    if res.type == EventType.ERROR:
        log.step(f"{unit_id}: remove failed ({res.payload})")
        return False
    pop_companion_contact(client, pk)
    return True


async def forget_replaced_identities(
    client: MeshCore,
    *,
    keep_pubkey: str,
    names: list[str],
    unit_id: str,
    log: PollLog | None = None,
) -> int:
    """Remove companion contacts that still name this unit under an old pubkey."""
    from envybot.selector import stale_identity_pubkeys

    log = log or PollLog()
    stale = stale_identity_pubkeys(
        getattr(client, "contacts", None) or {},
        keep_pubkey=keep_pubkey,
        names=names,
        unit_id=unit_id,
    )
    dropped = 0
    for pk in stale:
        if await drop_companion_contact(client, pk, log=log, unit_id=unit_id):
            log.step(f"{unit_id}: dropped old {pk[:12]}…")
            dropped += 1
    return dropped


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
        stub = contact_stub_for_target(target)
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
    dropped = 0
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
            dropped += await forget_replaced_identities(
                client,
                keep_pubkey=target.pubkey_hex,
                names=[target.unit_id, target.name, contact_display_name(target)],
                unit_id=target.unit_id,
                log=log,
            )
    print(
        f"Fleet contacts ready: {added} added, {favorited} newly favorited, "
        f"{dropped} old identities dropped, {len(client.contacts)} total on companion"
    )


async def refresh_fleet_paths(
    client: MeshCore,
    targets: list[RouterTarget],
    *,
    log: PollLog | None = None,
) -> int:
    """Clear companion out_path for every fleet target (operator moved; stale hops)."""
    log = log or PollLog()
    if not targets:
        return 0
    cleared = 0
    async with client.commands._mesh_request_lock:
        await client.ensure_contacts(follow=True)
        for target in targets:
            await reset_to_flood(client, target, log=log)
            cleared += 1
    print(
        f"Companion paths refreshed for {cleared} unit(s) "
        "(cached hops cleared; next login floods to rediscover)"
    )
    return cleared


async def recover_companion(
    client: MeshCore,
    *,
    session: FleetSession,
    targets: list[Any],
    log: PollLog,
    attempts: int = COMPANION_RECONNECT_ATTEMPTS,
) -> bool:
    """Reconnect companion transport after an unexpected drop (common on long BLE apply)."""
    if client.is_connected:
        return True

    for attempt in range(1, attempts + 1):
        log.step(f"companion disconnected — reconnect {attempt}/{attempts} …")
        try:
            if not client.dispatcher.running:
                await client.dispatcher.start()
            result = await client.connection_manager.connect()
            if result is None:
                await asyncio.sleep(1.0)
                continue
            res = await client.commands.send_appstart()
            if res is None or res.type == EventType.ERROR:
                await asyncio.sleep(1.0)
                continue
            res = await client.commands.set_time(int(time.time()))
            if res.type == EventType.ERROR:
                log.detail(f"recover set_time: {res.payload}")
            await client.ensure_contacts(follow=True)
            if targets:
                await sync_fleet_contacts(client, targets, log=log)
            session.authed_units.clear()
            session.bind_companion(client)
            if client.is_connected:
                log.step("companion reconnected")
                return True
        except Exception as exc:
            log.detail(f"recover attempt {attempt}: {exc}")
        await asyncio.sleep(1.0)

    log.step("companion reconnect failed")
    return False


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
    cancel_check: Any | None = None,
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
            if cancel_check is not None and cancel_check():
                return None
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
    """Clear the companion's saved out_path so the next send floods."""
    log = log or PollLog()
    prefix = target.pubkey_hex[:12]
    contact = client.get_contact_by_key_prefix(prefix)
    if not isinstance(contact, dict):
        contact = contact_stub_for_target(target)
        contacts = getattr(client, "contacts", None)
        if isinstance(contacts, dict):
            contacts[contact["public_key"]] = contact
    res = await client.commands.reset_path(target.pubkey_hex)
    if res.type == EventType.ERROR:
        log.detail(f"reset_path warning: {res.payload}")
    pin_contact_route(contact, flood=True)


async def prepare_direct_route(
    client: MeshCore, target: RouterTarget, *, log: PollLog | None = None
) -> None:
    """Force zero-hop direct before a mesh send."""
    log = log or PollLog()
    prefix = target.pubkey_hex[:12]
    contact = client.get_contact_by_key_prefix(prefix)
    if not isinstance(contact, dict):
        contact = contact_stub_for_target(target)
        contacts = getattr(client, "contacts", None)
        if isinstance(contacts, dict):
            contacts[contact["public_key"]] = contact
        log.detail("prepare_route: no cached contact, using stub")
    res = await client.commands.update_contact(contact, path="", path_hash_mode=0)
    if res.type == EventType.ERROR:
        log.detail(f"prepare_route direct warning: {res.payload}")
    pin_contact_route(contact, flood=False)


async def prepare_route(
    client: MeshCore,
    target: RouterTarget,
    *,
    log: PollLog | None = None,
    route_extra: dict[str, Any] | None = None,
) -> None:
    """Prepare companion route before login or send."""
    log = log or PollLog()
    policy = target.routing
    if policy is RoutingMode.DIRECT:
        await prepare_direct_route(client, target, log=log)
    elif policy is RoutingMode.FLOOD:
        await reset_to_flood(client, target, log=log)
    else:
        contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
        if has_cached_route(contact):
            publish_live_route(client, target, route_extra)
            return
        await reset_to_flood(client, target, log=log)
    publish_live_route(client, target, route_extra)


async def handle_path_timeout(
    client: MeshCore,
    target: RouterTarget,
    route_extra: dict[str, Any] | None,
    *,
    log: PollLog | None = None,
) -> None:
    """Path policy: count failures on cached route; discard at threshold."""
    if target.routing is not RoutingMode.PATH or route_extra is None:
        return
    log = log or PollLog()
    contact = client.get_contact_by_key_prefix(target.pubkey_hex[:12])
    if not has_cached_route(contact):
        return
    rs = route_session_from_extra(route_extra)
    if rs.record_timeout(had_cached_route=True):
        log.step("path stale, discarding cache")
        await reset_to_flood(client, target, log=log)
        rs.reset_failures()
    persist_route_session(route_extra, rs)
    publish_live_route(client, target, route_extra)


async def handle_path_success(
    client: MeshCore,
    target: RouterTarget,
    route_extra: dict[str, Any] | None,
) -> None:
    if route_extra is None:
        return
    rs = route_session_from_extra(route_extra)
    rs.record_success()
    persist_route_session(route_extra, rs)
    publish_live_route(client, target, route_extra)


async def prepare_send_route(
    client: MeshCore,
    target: RouterTarget,
    *,
    log: PollLog | None = None,
    route_extra: dict[str, Any] | None = None,
) -> None:
    await prepare_route(client, target, log=log, route_extra=route_extra)


async def prepare_login_route(
    client: MeshCore,
    target: RouterTarget,
    *,
    log: PollLog | None = None,
    route_extra: dict[str, Any] | None = None,
) -> None:
    await prepare_route(client, target, log=log, route_extra=route_extra)


async def refresh_path_after_login(
    client: MeshCore,
    target: RouterTarget,
    *,
    log: PollLog | None = None,
    route_extra: dict[str, Any] | None = None,
) -> None:
    """Pull companion out_path after a successful login (path/flood discovery)."""
    if target.routing is RoutingMode.DIRECT:
        return
    log = log or PollLog()
    await refresh_contact_from_device(client, target)
    log_contact_path(client, target, log=log)
    publish_live_route(client, target, route_extra)


async def wait_login_response(
    client: MeshCore,
    prefix: str,
    *,
    timeout: float,
    cancel_check: Any | None = None,
) -> tuple[str, Any | None]:
    filters = {"pubkey_prefix": prefix}
    deadline = time.monotonic() + timeout
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
    try:
        while True:
            if cancel_check is not None and cancel_check():
                return "cancelled", None
            left = deadline - time.monotonic()
            if left <= 0:
                return "timeout", None
            wait_t = min(left, 0.25) if cancel_check is not None else left
            done, _ = await asyncio.wait(
                {success_task, failed_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=wait_t,
            )
            if not done:
                continue
            for task in done:
                event = task.result()
                if event is None:
                    continue
                if task is success_task:
                    return "success", event
                return "failed", event
            return "timeout", None
    finally:
        success_task.cancel()
        failed_task.cancel()


async def admin_login(
    client: MeshCore,
    target: RouterTarget,
    *,
    login_timeout: float,
    attempts: int,
    session: FleetSession | None = None,
    log: PollLog | None = None,
    route_extra: dict[str, Any] | None = None,
) -> tuple[bool, str | None, int | None]:
    """Password login before poll GET or remote CLI."""
    log = log or PollLog()

    dst = target.pubkey_hex
    prefix = dst[:12]
    attempt = 0

    while attempts == 0 or attempt < attempts:
        attempt += 1
        audit_id: int | None = None
        async with client.commands._mesh_request_lock:
            await ensure_contact_on_device(client, target, log=log)
            await prepare_login_route(client, target, log=log, route_extra=route_extra)
            log_contact_path(client, target, log=log)
            path = audit_path_at_send(client, target)
            sent = await send_login_frame(client, dst, target.admin_password)
            if sent is None or sent.type == EventType.ERROR:
                err = sent.payload if sent else "no response"
                audit_id = _audit_begin(
                    session,
                    unit=target.unit_id,
                    kind="login",
                    label="login",
                    attempt=attempt,
                    path=path,
                    wait_s=0.0,
                )
                _audit_finish(
                    session,
                    audit_id,
                    ok=False,
                    outcome="send_error",
                    error=err,
                )
                log.step(f"login {attempt_label(attempt, attempts)}: send error ({err}), retrying …")
                continue
            suggested_ms = sent.payload.get("suggested_timeout", 60000)

        wait_s = mesh_wait_seconds(suggested_ms, cap=login_timeout)
        if session is not None:
            wait_s = session.stretch_wait(wait_s, target.unit_id, cap=login_timeout)
        n_of = attempt_label(attempt, attempts)
        audit_id = _audit_begin(
            session,
            unit=target.unit_id,
            kind="login",
            label="login",
            attempt=attempt,
            path=path,
            wait_s=wait_s,
        )
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
            exp.audit_id = audit_id
        status, event = await wait_login_response(client, prefix, timeout=wait_s)
        if exp is not None and status != "timeout":
            session.resolve_expect(exp)
        if status == "success":
            node_clock: int | None = None
            if event and event.payload:
                ts = event.payload.get("server_timestamp")
                if ts is not None:
                    node_clock = int(ts)
            reply = f"clock={node_clock}" if node_clock is not None else "ok"
            _audit_finish(session, audit_id, ok=True, outcome="ok", reply=reply)
            if node_clock is not None:
                log.step(f"login OK (clock={node_clock})")
            else:
                log.step("login OK")
            log.detail(f"LOGIN_SUCCESS {event.payload if event else ''}")
            if session is not None:
                session.mark_authed(target.key)
            await refresh_path_after_login(client, target, log=log, route_extra=route_extra)
            await handle_path_success(client, target, route_extra)
            return True, None, node_clock
        if status == "failed":
            _audit_finish(session, audit_id, ok=False, outcome="rejected", reply="rejected")
            log.step("login rejected")
            log.detail(f"LOGIN_FAILED {event.payload if event else ''}")
            if session is not None:
                session.clear_auth(target.key)
            return False, "login rejected (bad password?)", None
        _audit_finish(session, audit_id, ok=False, outcome="timeout")
        await handle_path_timeout(client, target, route_extra, log=log)
        if attempts and attempt >= attempts:
            log.step(f"login {n_of}: timeout after {wait_s:.0f}s")
            break
        log.step(f"login {n_of}: timeout after {wait_s:.0f}s, retrying …")

    return False, f"login failed after {attempts} attempts", None


async def admin_login_attempt(
    client: MeshCore,
    target: RouterTarget,
    *,
    login_timeout: float,
    session: FleetSession | None = None,
    log: PollLog | None = None,
    attempt_num: int = 1,
    attempt_cap: int | None = None,
    cancel_check: Any | None = None,
    route_extra: dict[str, Any] | None = None,
) -> tuple[bool, str | None, int | None]:
    """Single login send+wait."""
    log = log or PollLog()
    if session is not None:
        session.clear_auth(target.key)
    dst = target.pubkey_hex
    prefix = dst[:12]
    audit_id: int | None = None
    async with client.commands._mesh_request_lock:
        await ensure_contact_on_device(client, target, log=log)
        await prepare_login_route(client, target, log=log, route_extra=route_extra)
        log_contact_path(client, target, log=log)
        path = audit_path_at_send(client, target)
        sent = await send_login_frame(client, dst, target.admin_password)
        if sent is None or sent.type == EventType.ERROR:
            err = sent.payload if sent else "no response"
            audit_id = _audit_begin(
                session,
                unit=target.unit_id,
                kind="login",
                label="login",
                attempt=attempt_num,
                path=path,
                wait_s=0.0,
            )
            _audit_finish(
                session,
                audit_id,
                ok=False,
                outcome="send_error",
                error=err,
            )
            log.step(f"login {attempt_num}: send error ({err})")
            return False, str(err), None
        suggested_ms = sent.payload.get("suggested_timeout", 60000)

    wait_s = mesh_wait_seconds(suggested_ms, cap=login_timeout)
    if session is not None:
        wait_s = session.stretch_wait(wait_s, target.unit_id, cap=login_timeout)
    cap = attempt_cap if attempt_cap else 0
    n_of = attempt_label(attempt_num, cap)
    audit_id = _audit_begin(
        session,
        unit=target.unit_id,
        kind="login",
        label="login",
        attempt=attempt_num,
        path=path,
        wait_s=wait_s,
    )
    log.step(f"send login 0x1a {n_of} (≤{wait_s:.0f}s, no echo id) …")

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
        exp.audit_id = audit_id
    status, event = await wait_login_response(
        client, prefix, timeout=wait_s, cancel_check=cancel_check
    )
    if exp is not None and status not in ("timeout", "cancelled"):
        session.resolve_expect(exp)
    if status == "cancelled":
        _audit_finish(session, audit_id, ok=False, outcome="cancelled", error="cancelled")
        log.step(f"login {n_of}: cancelled")
        return False, "cancelled", None
    if status == "success":
        node_clock: int | None = None
        if event and event.payload:
            ts = event.payload.get("server_timestamp")
            if ts is not None:
                node_clock = int(ts)
        reply = f"clock={node_clock}" if node_clock is not None else "ok"
        _audit_finish(session, audit_id, ok=True, outcome="ok", reply=reply)
        if node_clock is not None:
            log.step(f"login OK (clock={node_clock})")
        else:
            log.step("login OK")
        if session is not None:
            session.mark_authed(target.key)
        await refresh_path_after_login(client, target, log=log, route_extra=route_extra)
        await handle_path_success(client, target, route_extra)
        return True, None, node_clock
    if status == "failed":
        _audit_finish(session, audit_id, ok=False, outcome="rejected", reply="rejected")
        log.step("login rejected")
        if session is not None:
            session.clear_auth(target.key)
        return False, "login rejected (bad password?)", None
    _audit_finish(session, audit_id, ok=False, outcome="timeout")
    await handle_path_timeout(client, target, route_extra, log=log)
    log.step(f"login {n_of}: timeout after {wait_s:.0f}s")
    return False, f"login timeout after {wait_s:.0f}s", None


async def fetch_repeater_clock(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None = None,
) -> tuple[int | None, bool]:
    """Return (epoch or None, heard). heard=False is a timeout, not an unparsed reply."""
    raw = await send_cmd_sync(
        client,
        target,
        "clock",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        return None, False
    ts = parse_clock_cli(raw)
    if ts is not None:
        log.step(f"clock {ts}")
    else:
        log.step(f"clock: unparsed ({raw.strip()[:40]})")
    return ts, True


async def maybe_admin_access(
    client: MeshCore,
    target: RouterTarget,
    *,
    node: dict[str, Any],
    doc: dict[str, Any] | None,
    login_timeout: float,
    cmd_timeout: float,
    attempts: int,
    session: FleetSession | None,
    log: PollLog,
    fetch_clock: bool = True,
    keys: dict[str, list[str]] | None = None,
) -> tuple[bool, str | None, int | None]:
    """Password login. ``node``/``doc``/``keys``/``fetch_clock`` kept for callers."""
    _ = (node, doc, keys, fetch_clock, cmd_timeout)
    return await admin_login(
        client,
        target,
        login_timeout=login_timeout,
        attempts=attempts,
        session=session,
        log=log,
    )


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
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
    cancel_check: Any | None = None,
    cancelled_out: list[bool] | None = None,
    route_extra: dict[str, Any] | None = None,
) -> str | None:
    log = log or PollLog()
    """Send CLI command; route prepared before each send (flood or bench direct)."""
    dst_hex = target.pubkey_hex
    pubkey_prefix = dst_hex[:12]
    attempt = 0
    single = attempt_num is not None
    max_attempts = 1 if single else attempts
    label_cap = attempt_cap if (single and attempt_cap) else (attempts if attempts else 0)

    while max_attempts == 0 or attempt < max_attempts:
        attempt = attempt_num if single else attempt + 1
        audit_id: int | None = None
        if session is not None and not await session.ensure_companion_connected(log=log):
            log.step("send aborted: companion not connected")
            return None
        prefix_token = next_cli_prefix()
        framed = f"{prefix_token}{cmd}"
        async with client.commands._mesh_request_lock:
            await prepare_send_route(client, target, log=log, route_extra=route_extra)
            log_contact_path(client, target, log=log)
            path = audit_path_at_send(client, target)
            sent = await send_cli_frame(client, dst_hex, framed, attempt=attempt - 1)
            if sent is None or sent.type == EventType.ERROR:
                err = sent.payload if sent else "no response"
                audit_id = _audit_begin(
                    session,
                    unit=target.unit_id,
                    kind="cli",
                    label=cmd,
                    attempt=attempt,
                    path=path,
                    wait_s=0.0,
                )
                _audit_finish(
                    session,
                    audit_id,
                    ok=False,
                    outcome="send_error",
                    error=err,
                )
                if session is not None and not client.is_connected:
                    if await session.ensure_companion_connected(log=log):
                        continue
                    log.step("send aborted: companion not connected")
                    return None
                log.step(
                    f"send {framed!r} {attempt_label(attempt, label_cap)}: "
                    f"send error ({err}), retrying …"
                )
                continue
            suggested_ms = sent.payload.get("suggested_timeout", 60000)

        wait_s = mesh_wait_seconds(suggested_ms, cap=timeout)
        slack0 = 0.0
        if session is not None:
            slack0 = session.dest_slack(target.unit_id)
            wait_s = session.stretch_wait(wait_s, target.unit_id, cap=timeout)
        n_of = attempt_label(attempt, label_cap)
        audit_id = _audit_begin(
            session,
            unit=target.unit_id,
            kind="cli",
            label=cmd,
            attempt=attempt,
            path=path,
            wait_s=wait_s,
        )
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
            exp.audit_id = audit_id
        text = await wait_cli_response(
            client,
            pubkey_prefix=pubkey_prefix,
            prefix_token=prefix_token,
            timeout=wait_s,
            log=log,
            session=session,
            unit=target.unit_id,
            slack0=slack0,
            cancel_check=cancel_check,
        )
        if cancel_check is not None and cancel_check() and text is None:
            if cancelled_out is not None:
                cancelled_out.append(True)
            _audit_finish(session, audit_id, ok=False, outcome="cancelled", error="cancelled")
            return None
        if exp is not None and text:
            session.resolve_expect(exp)
        if text:
            _audit_finish(session, audit_id, ok=True, outcome="ok", reply=text)
            log.detail(f"cli reply {prefix_token}{text[:120]}")
            if not (cli_error_reply(text) or cli_suggests_auth_failure(text)):
                log.step(format_cli_ok(cmd, text))
            await handle_path_success(client, target, route_extra)
            return text
        _audit_finish(session, audit_id, ok=False, outcome="timeout")
        if single or (attempts and attempt >= attempts):
            log.step(f"send {framed!r} {n_of}: timeout after {wait_s:.0f}s")
            break
        await handle_path_timeout(client, target, route_extra, log=log)
        if target.routing is RoutingMode.FLOOD:
            await reset_to_flood(client, target, log=log)
        log.step(f"send {framed!r} {n_of}: timeout after {wait_s:.0f}s, retrying …")

    return None


async def send_cmd_once(
    client: MeshCore,
    target: RouterTarget,
    cmd: str,
    *,
    timeout: float,
    log: PollLog | None = None,
    session: FleetSession | None = None,
    attempt_num: int = 1,
    attempt_cap: int | None = None,
    cancel_check: Any | None = None,
    cancelled_out: list[bool] | None = None,
    route_extra: dict[str, Any] | None = None,
) -> str | None:
    """Single CLI send+wait."""
    return await send_cmd_sync(
        client,
        target,
        cmd,
        timeout=timeout,
        attempts=1,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
        cancel_check=cancel_check,
        cancelled_out=cancelled_out,
        route_extra=route_extra,
    )


async def binary_req_once(
    label: str,
    fetch: Callable[[float], Awaitable[Any]],
    *,
    client: MeshCore,
    target: RouterTarget,
    log: PollLog,
    session: FleetSession | None = None,
    wait_s: float = 0.0,
    cap: float = 0.0,
    attempt_num: int = 1,
    attempt_cap: int | None = None,
    on_retry: Callable[[], Awaitable[None]] | None = None,
    success: Callable[[Any], bool] | None = None,
    route_extra: dict[str, Any] | None = None,
) -> Any:
    """Single binary mesh request attempt."""

    return await retry_binary_req(
        label,
        fetch,
        client=client,
        attempts=1,
        log=log,
        success=success,
        on_retry=on_retry,
        session=session,
        target=target,
        wait_s=wait_s,
        cap=cap,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
        route_extra=route_extra,
    )


async def retry_binary_req(
    label: str,
    fetch: Callable[[float], Awaitable[Any]],
    *,
    client: MeshCore,
    attempts: int,
    log: PollLog,
    success: Callable[[Any], bool] | None = None,
    on_retry: Callable[[int], Awaitable[None]] | None = None,
    session: FleetSession | None = None,
    target: RouterTarget | None = None,
    wait_s: float = 0.0,
    cap: float = 0.0,
    attempt_num: int | None = None,
    attempt_cap: int | None = None,
    route_extra: dict[str, Any] | None = None,
) -> Any:
    """Retry binary mesh requests (status, telemetry, neighbors, acl, …)."""
    def ok(val: Any) -> bool:
        if success is not None:
            return success(val)
        return val is not None

    async def default_on_retry(_attempt: int) -> None:
        if target is None:
            return
        await handle_path_timeout(client, target, route_extra, log=log)
        if target.routing is RoutingMode.FLOOD:
            await reset_to_flood(client, target, log=log)

    retry_hook = on_retry if on_retry is not None else default_on_retry

    attempt = 0
    single = attempt_num is not None
    max_attempts = 1 if single else attempts
    label_cap = attempt_cap if (single and attempt_cap) else (attempts if attempts else 0)

    while max_attempts == 0 or attempt < max_attempts:
        attempt = attempt_num if single else attempt + 1
        audit_id: int | None = None
        if session is not None and not await session.ensure_companion_connected(log=log):
            log.step(f"{label}: aborted — companion not connected")
            break
        if target is not None:
            await prepare_send_route(client, target, log=log, route_extra=route_extra)
            log_contact_path(client, target, log=log)
        n_of = attempt_label(attempt, label_cap)
        dest_wait = wait_s
        path = "flood"
        if session is not None and target is not None:
            dest_wait = session.stretch_wait(wait_s, target.unit_id, cap=cap or 0.0)
            path = audit_path_at_send(client, target)
        log.step(f"send binary {label} {n_of} (≤{dest_wait:.0f}s) …")
        audit_id = _audit_begin(
            session,
            unit=target.unit_id if target else "",
            kind="binary",
            label=label,
            attempt=attempt,
            path=path,
            wait_s=dest_wait,
        ) if target is not None else None
        inflight = None
        if session is not None and target is not None:
            inflight = session.begin_binary(
                label=label,
                unit=target.unit_id,
                pubkey_prefix=target.pubkey_hex,
                n_of=n_of,
                deadline=time.monotonic() + max(dest_wait, 8.0) + 1.0,
                audit_id=audit_id,
            )
        result = None
        try:
            result = await fetch(dest_wait)
        finally:
            if session is not None and inflight is not None:
                session.end_binary(inflight, resolved=ok(result))
        if ok(result):
            _audit_finish(
                session,
                audit_id,
                ok=True,
                outcome="ok",
                reply=_audit_binary_reply(result),
            )
            log.step(format_binary_ok(label, result))
            if target is not None:
                await handle_path_success(client, target, route_extra)
            return result
        _audit_finish(session, audit_id, ok=False, outcome="timeout")
        if single or (attempts and attempt >= attempts):
            log.step(f"{label} {n_of}: no response")
            break
        await retry_hook(attempt)
        log.step(f"{label} {n_of}: no response, retrying …")
    return None


async def trigger_neighbor_discover_once(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    session: FleetSession | None,
    log: PollLog,
    attempt_num: int = 1,
    attempt_cap: int | None = None,
    route_extra: dict[str, Any] | None = None,
) -> bool:
    """Send discover.neighbors CLI once (no listen window)."""
    raw = await send_cmd_once(
        client,
        target,
        "discover.neighbors",
        timeout=cmd_timeout,
        log=log,
        session=session,
        attempt_num=attempt_num,
        attempt_cap=attempt_cap,
        route_extra=route_extra,
    )
    if raw is None:
        log.step("discover.neighbors: no response")
        return False
    if cli_suggests_auth_failure(raw) or cli_error_reply(raw):
        if session is not None and cli_suggests_auth_failure(raw):
            session.clear_auth(target.key)
            log.step("auth cleared (CLI denied)")
        log.step("discover.neighbors: error")
        return False
    return True


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


async def trigger_neighbor_discover(
    client: MeshCore,
    target: RouterTarget,
    *,
    cmd_timeout: float,
    attempts: int,
    log: PollLog,
    session: FleetSession | None,
    wait_s: float,
) -> bool:
    """Ask the repeater to send a zero-hop discover, then wait for replies."""
    raw = await send_cmd_sync(
        client,
        target,
        "discover.neighbors",
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,
        session=session,
    )
    if raw is None:
        log.step("discover.neighbors: no response")
        return False
    if cli_suggests_auth_failure(raw) or cli_error_reply(raw):
        if session is not None and cli_suggests_auth_failure(raw):
            session.clear_auth(target.key)
            log.step("auth cleared (CLI denied)")
        log.step("discover.neighbors: error")
        return False
    if wait_s > 0:
        log.step(f"discover wait {wait_s:g}s")
        await asyncio.sleep(wait_s)
    return True


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


def companion_candidate_display(cand: CompanionCandidate) -> str:
    """Human label for picker/probe; includes pubkey when probed."""
    parts = [cand.label]
    if cand.pubkey_hex:
        parts.append(f"pubkey {cand.pubkey_hex[:12]}…")
        if cand.keys_person:
            parts.append(f"({cand.keys_person})")
    elif cand.probe_error:
        parts.append(f"probe failed: {cand.probe_error}")
    return " — ".join(parts)


def _book_keys_for_args(args: argparse.Namespace) -> dict[str, list[str]] | None:
    nodes = getattr(args, "nodes", None)
    if nodes is None:
        return None
    path = keys_path(Path(nodes))
    if not path.is_file():
        return None
    return load_keys(path)


async def probe_companion_candidate(
    args: argparse.Namespace, cand: CompanionCandidate, *, keys: dict[str, list[str]] | None = None
) -> CompanionCandidate:
    """Brief connect to read companion SELF_INFO pubkey, then disconnect."""
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
        return replace(cand, probe_error=err or "connect failed")
    try:
        name = _self_info_name(client)
        await ensure_companion_identity(client)
        pubkey = companion_identity(client)
        person = find_person_for_pubkey(keys, pubkey) if keys and pubkey else None
        return replace(
            cand,
            node_name=name or cand.node_name,
            pubkey_hex=pubkey,
            keys_person=person,
        )
    except Exception as exc:
        return replace(cand, probe_error=str(exc))
    finally:
        await client.disconnect()


async def probe_companion_candidates(
    args: argparse.Namespace, candidates: list[CompanionCandidate]
) -> list[CompanionCandidate]:
    if len(candidates) <= 1:
        return candidates
    keys = _book_keys_for_args(args)
    print(f"Probing {len(candidates)} companion(s) for pubkey …", file=sys.stderr)
    out: list[CompanionCandidate] = []
    for cand in candidates:
        out.append(await probe_companion_candidate(args, cand, keys=keys))
    return out


def _print_companion_candidates(candidates: list[CompanionCandidate], header: str) -> None:
    print(header, file=sys.stderr)
    for i, cand in enumerate(candidates, 1):
        print(f"  {i}. {companion_candidate_display(cand)}", file=sys.stderr)


def pick_companion(candidates: list[CompanionCandidate]) -> CompanionCandidate:
    """Return the sole candidate, or prompt on a TTY when several are found."""
    if not candidates:
        raise SystemExit("No companion candidates to pick from")
    if len(candidates) == 1:
        return candidates[0]
    if not sys.stdin.isatty():
        _print_companion_candidates(
            candidates,
            "Multiple companions — pass --ble ADDRESS, --serial PORT, or --tcp host:port:",
        )
        raise SystemExit(2)
    _print_companion_candidates(candidates, "Multiple companions — pick one:")
    while True:
        try:
            raw = input(f"Pick companion [1-{len(candidates)}]: ").strip()
        except EOFError:
            raise SystemExit("Companion pick cancelled")
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("Enter a number from the list.", file=sys.stderr)
            continue
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
        print(f"Enter 1–{len(candidates)}.", file=sys.stderr)


DISCOVER_REPEATER_FILTER = 1 << CONTACT_TYPE_REPEATER
COMPANION_DISCOVER_WAIT_S = 10.0


def normalize_discover_tag(tag: Any) -> str | None:
    """Normalize discover tag to 8-byte little-endian hex (meshcore-py wire form)."""
    if tag is None:
        return None
    if isinstance(tag, int):
        return tag.to_bytes(4, "little", signed=False).hex()
    text = str(tag).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text or None


def discover_response_row(
    payload: dict[str, Any] | None,
    *,
    expected_tag_hex: str,
) -> dict[str, Any] | None:
    """Return one repeater discover row when payload matches tag and type."""
    if not isinstance(payload, dict):
        return None
    tag_hex = normalize_discover_tag(payload.get("tag"))
    if not tag_hex or tag_hex != expected_tag_hex:
        return None
    node_type = payload.get("node_type")
    try:
        if int(node_type) != CONTACT_TYPE_REPEATER:
            return None
    except (TypeError, ValueError):
        return None
    pubkey = str(payload.get("pubkey") or "").strip().lower()
    if not pubkey:
        return None
    snr = payload.get("SNR")
    try:
        snr_f = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        snr_f = None
    return {"pubkey": pubkey, "snr": snr_f}


def discover_repeater_label(client: MeshCore, pubkey: str) -> str:
    """Resolve on-air name from companion contacts, else pubkey prefix."""
    lookup = client.get_contact_by_key_prefix(pubkey[:12]) if len(pubkey) >= 8 else None
    if lookup:
        name = str(lookup.get("adv_name") or "").strip()
        if name:
            return name[:32]
    return pubkey[:8]


def format_companion_neighbor_report(rows: list[dict[str, Any]]) -> list[str]:
    """Human lines for companion zero-hop discover replies."""
    if not rows:
        return [
            "Companion repeaters nearby: none. "
            "Fleet may not get out (antenna, channel, or radio deaf)."
        ]
    lines = [f"Companion repeaters nearby ({len(rows)}):"]
    for row in rows:
        name = str(row.get("name") or row.get("pubkey", "")[:8])
        prefix = str(row.get("pubkey") or "")[:8]
        snr = row.get("snr")
        snr_text = f"{snr:.1f} dB" if isinstance(snr, (int, float)) else "?"
        lines.append(f"  {name:<24} {prefix}  {snr_text}")
    return lines


def merge_discover_row(
    rows: dict[str, dict[str, Any]], row: dict[str, Any], *, client: MeshCore
) -> None:
    """Keep the strongest SNR per pubkey prefix."""
    pubkey = row["pubkey"]
    key = pubkey[:8] if len(pubkey) >= 8 else pubkey
    snr = row.get("snr")
    existing = rows.get(key)
    if existing is None:
        rows[key] = {
            "pubkey": pubkey if len(pubkey) >= 8 else key,
            "snr": snr,
            "name": discover_repeater_label(client, pubkey),
        }
        return
    prev = existing.get("snr")
    if snr is not None and (prev is None or snr > prev):
        existing["snr"] = snr
        if len(pubkey) > len(str(existing.get("pubkey") or "")):
            existing["pubkey"] = pubkey


async def companion_neighbor_ping(
    client: MeshCore,
    *,
    wait_s: float = COMPANION_DISCOVER_WAIT_S,
) -> None:
    """Zero-hop NODE_DISCOVER_REQ: list nearby repeaters that answer (Tools-style ping)."""
    collected: dict[str, dict[str, Any]] = {}
    expected_tag_hex: str | None = None

    def on_discover(event: Any) -> None:
        if expected_tag_hex is None:
            return
        row = discover_response_row(
            event.payload if isinstance(event.payload, dict) else None,
            expected_tag_hex=expected_tag_hex,
        )
        if row is not None:
            merge_discover_row(collected, row, client=client)

    sub = client.subscribe(EventType.DISCOVER_RESPONSE, on_discover)
    try:
        print(f"Companion neighbor ping ({wait_s:g}s) …", flush=True)
        res = await client.commands.send_node_discover_req(
            filter=DISCOVER_REPEATER_FILTER,
            prefix_only=True,
            since=0,
        )
        if res is None:
            print("Companion neighbor ping skipped (no response)")
            return
        if res.is_error():
            reason = ""
            if isinstance(res.payload, dict):
                reason = str(res.payload.get("reason") or "")
            print(f"Companion neighbor ping skipped ({reason or 'error'})")
            return
        expected_tag_hex = normalize_discover_tag(res.payload.get("tag"))
        if not expected_tag_hex:
            print("Companion neighbor ping skipped (missing tag)")
            return
        await asyncio.sleep(wait_s)
    finally:
        sub.unsubscribe()

    ordered = sorted(
        collected.values(),
        key=lambda r: (r.get("snr") is not None, r.get("snr") or -999.0),
        reverse=True,
    )
    for line in format_companion_neighbor_report(ordered):
        print(line, flush=True)


async def post_connect(client: MeshCore) -> None:
    """Shared post-connect setup: clock sync, auto-fetch, contact sync."""
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
    await companion_neighbor_ping(client)


async def connect_candidate(args: argparse.Namespace, cand: CompanionCandidate) -> MeshCore:
    if cand.transport == "ble" and cand.ble_address is None and cand.ble_device is None:
        ble_list = await scan_ble_candidates(args.scan_timeout)
        if not ble_list:
            raise SystemExit("No BLE MeshCore companions found (NUS service scan)")
        ble_list = await probe_companion_candidates(args, ble_list)
        cand = pick_companion(ble_list)

    print(f"Connecting via {companion_candidate_display(cand)} …")
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

    keys = _book_keys_for_args(args)
    print(f"\nFound {len(candidates)} candidate(s):")
    any_ok = False
    for cand in candidates:
        probed = await probe_companion_candidate(args, cand, keys=keys)
        if probed.probe_error:
            print(f"  ✗ {companion_candidate_display(probed)}")
            continue
        name = probed.node_name or "?"
        print(f"  ✓ {companion_candidate_display(probed)} — companion OK ({name})")
        any_ok = True

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

    candidates = await probe_companion_candidates(args, candidates)
    cand = pick_companion(candidates)
    print(f"Connecting via {companion_candidate_display(cand)} …")
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
        raise SystemExit(f"Failed to connect ({cand.label}): {err}. Try --probe.")
    name = _self_info_name(client)
    if name:
        print(f"Companion ready: {name}")
    await post_connect(client)
    return client


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



def poll_summary(res: PollResult) -> str:
    parts: list[str] = []
    if "firmware" in res.polled_groups:
        parts.append(f"fw={res.firmware_version or '?'}")
    if "bootloader" in res.polled_groups:
        parts.append(f"bl={res.bootloader_version if res.bootloader_version else '?'}")
    if "ota" in res.polled_groups:
        parts.append(f"base={res.base_hash if res.base_hash else '-'}")
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
            parts.append(f"bat={bat / 1000:.2f}V")
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

def target_label(target: RouterTarget) -> str:
    site = f" @ {target.site}" if target.site else ""
    return f"{target.unit_id} {target.name}{site}"

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



