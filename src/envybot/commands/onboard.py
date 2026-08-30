#!/usr/bin/env python3
"""USB-serial onboard for a MeshCore repeater (text CLI, not companion protocol).

``get prv.key`` is serial-only (sender_timestamp == 0). Do not send these
commands over the mesh.

Idempotent on identity/creds/radio/name/GPS: match existing pubkey (or --unit),
GET then SET, reuse stored passwords, reboot only if radio prefs changed.
Always pulls firmware + bootloader.ver and re-runs neighbor discover/fetch.
Writes nodes.yaml (next ME#### if new; site stays null).
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pyserial not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot onboard"
    ) from exc

from envybot.history import open_history, record_onboard_heard
from envybot.nodes_doc import (
    PLACEHOLDER_PW,
    allocate_unit_id,
    load_nodes_doc,
    remember_unit_id,
    write_nodes_doc,
)
from envybot.radio import (
    airtime_factor_for_dutycycle,
    firmware_has_dutycycle_cli,
    parse_bootloader,
    parse_coord,
    parse_dutycycle,
    parse_firmware,
    parse_get_value,
    parse_int_get_value,
    serial_port_candidates,
)

# USA/Canada recommended: 910.525 MHz, BW 62.5, SF7, CR 4/5
RADIO_FREQ = 910.525
RADIO_BW = 62.5
RADIO_SF = 7
RADIO_CR = 5
RADIO_CMD = f"set radio {RADIO_FREQ},{RADIO_BW},{RADIO_SF},{RADIO_CR}"

ONBOARD_NAME = "Repeater"
ONBOARD_LAT = 0.0
ONBOARD_LON = 0.0
DUTYCYCLE_PCT = 100
ADVERT_MIN = 0
FLOOD_ADVERT_H = 0

BAUD = 115200
SETTLE_S = 2.5
DEFAULT_TIMEOUT = 5.0
REPLY_IDLE_S = 0.25
DISCOVER_ROUNDS = 3
DISCOVER_WAIT_S = 12.0
REBOOT_SETTLE_S = 4.0
RECONNECT_TIMEOUT_S = 25.0
PW_LEN = 14
PW_ALPHABET = string.ascii_letters + string.digits + "%&@#*^$!"
PUB_HEX_LEN = 64
PRV_HEX_LEN = 128
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
NEIGH_RE = re.compile(r"^([0-9a-fA-F]{8}):(\d+):(-?\d+)$")


class CliError(RuntimeError):
    pass


def gen_password(length: int = PW_LEN) -> str:
    chars = [secrets.choice(string.ascii_letters)]
    chars.extend(secrets.choice(PW_ALPHABET) for _ in range(length - 1))
    return "".join(chars)


def resolve_port_arg(arg: str) -> str | None:
    raw = arg.strip()
    candidates = [raw]
    if not raw.startswith("/"):
        candidates.append(f"/dev/{raw}")
        if not raw.startswith(("cu.", "tty.")):
            candidates.append(f"/dev/cu.{raw}")
            candidates.append(f"/dev/tty.{raw}")
    if raw.startswith("/dev/tty."):
        candidates.append("/dev/cu." + raw.removeprefix("/dev/tty."))
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def find_unit_by_pubkey(nodes: dict[str, Any], pubkey: str) -> str | None:
    want = pubkey.strip().lower()
    for key, node in nodes.items():
        have = str(node.get("identity_pubkey") or "").strip().lower()
        if have and have == want:
            return key
    return None


def stored_admin_password(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    val = node.get("admin_password")
    if not val or not isinstance(val, str):
        return None
    val = val.strip()
    if not val or val in PLACEHOLDER_PW:
        return None
    return val


def stored_guest_password(node: dict[str, Any] | None) -> str | None:
    """None = unset in yaml. Empty string = blank guest (intentional)."""
    if not node or "guest_password" not in node:
        return None
    val = node.get("guest_password")
    if val is None:
        return None
    return str(val)


def coords_match(got: float | None, want: float) -> bool:
    return got is not None and abs(got - want) < 1e-4


def read_bootloader(cli: "RepeaterSerial") -> tuple[str, str]:
    """Same parse as fleet pull. Returns (version_or_empty, raw_reply)."""
    raw = cli.cmd("get bootloader.ver")
    parsed = parse_bootloader(raw) or ""
    if parsed.lower() == "unknown":
        parsed = ""
    return parsed, raw


def new_node(unit_id: str) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "name": ONBOARD_NAME,
        "site": None,
        "firmware_platform": "meshcore",
        "firmware_version": None,
        "bootloader_version": None,
        "hardware": None,
        "last_admin_roll": None,
        "last_guest_roll": None,
        "last_key_roll": None,
        "notes": None,
        "admin_password": None,
        "guest_password": None,
        "identity_pubkey": None,
        "identity_secret": None,
        "admin1_pubkey": None,
        "admin1_secret": None,
        "decommissioned": None,
    }


def normalize_hex(text: str, expect_len: int, label: str) -> str:
    hexed = re.sub(r"\s+", "", text).lower()
    if not HEX_RE.match(hexed) or len(hexed) != expect_len:
        raise CliError(f"{label}: expected {expect_len} hex chars, got {text!r}")
    return hexed


def reply_failed(reply: str) -> bool:
    body = reply.strip()
    if body.startswith(">"):
        body = body[1:].strip()
    upper = body.upper()
    return upper.startswith("ERR") or upper.startswith("UNKNOWN") or upper.startswith("ERROR")


def parse_radio(text: str | None) -> tuple[float, float, int, int] | None:
    val = parse_get_value(text) or (text or "").strip()
    parts = [p.strip() for p in val.replace(" ", ",").split(",") if p.strip()]
    if len(parts) < 4:
        return None
    try:
        return float(parts[0]), float(parts[1]), int(float(parts[2])), int(float(parts[3]))
    except ValueError:
        return None


def radio_matches(got: tuple[float, float, int, int] | None) -> bool:
    if got is None:
        return False
    freq, bw, sf, cr = got
    return abs(freq - RADIO_FREQ) < 0.01 and abs(bw - RADIO_BW) < 0.1 and sf == RADIO_SF and cr == RADIO_CR


class RepeaterSerial:
    """MeshCore repeater USB text CLI (CR-terminated, ``  -> `` replies)."""

    def __init__(self, port: str, *, baud: int = BAUD, timeout: float = DEFAULT_TIMEOUT, verbose: bool = False):
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.05
        self.ser.write_timeout = 2
        self.ser.dsrdtr = False
        self.ser.rtscts = False

    def __enter__(self) -> RepeaterSerial:
        self.ser.open()
        time.sleep(SETTLE_S)
        self.ser.reset_input_buffer()
        self._write_raw(b"\x03")
        time.sleep(0.15)
        self._drain()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.ser.is_open:
            self.ser.close()

    def _write_raw(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def _drain(self) -> str:
        chunks: list[bytes] = []
        while True:
            chunk = self.ser.read(512)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def cmd(self, command: str, *, expect_reply: bool = True, timeout: float | None = None) -> str:
        self._drain()
        if self.verbose:
            print(f"    > {command}")
        self._write_raw((command + "\r").encode("ascii", errors="strict"))
        if not expect_reply:
            return ""
        wait = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + wait
        buf = ""
        saw = False
        last = time.monotonic()
        while time.monotonic() < deadline:
            chunk = self.ser.read(512)
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
                last = time.monotonic()
                if self._reply_start(buf) is not None:
                    saw = True
            elif saw and time.monotonic() - last >= REPLY_IDLE_S:
                break
            else:
                time.sleep(0.02)
        reply = self._extract_reply(buf)
        if reply is None:
            raise CliError(f"{command!r}: no CLI reply (not a repeater text console?)")
        if self.verbose:
            shown = reply.replace("\n", " | ")
            print(f"    <- {shown}")
        return reply

    @staticmethod
    def _reply_start(buf: str) -> int | None:
        text = buf.replace("\r\n", "\n").replace("\r", "\n")
        idx = text.find("->")
        return idx if idx >= 0 else None

    @classmethod
    def _extract_reply(cls, buf: str) -> str | None:
        text = buf.replace("\r\n", "\n").replace("\r", "\n")
        idx = text.find("->")
        if idx < 0:
            return None
        return text[idx + 2 :].strip()


def probe_repeater(port: str, *, baud: int, timeout: float, verbose: bool) -> str | None:
    try:
        with RepeaterSerial(port, baud=baud, timeout=timeout, verbose=verbose) as cli:
            reply = cli.cmd("ver")
    except (CliError, serial.SerialException, OSError):
        return None
    if not reply or reply_failed(reply):
        return None
    return reply


def pick_port(arg: str | None, *, baud: int, timeout: float, verbose: bool) -> str:
    if arg:
        port = resolve_port_arg(arg)
        if not port:
            raise SystemExit(f"USB port not found: {arg}")
        return port
    ports = serial_port_candidates()
    if not ports:
        raise SystemExit("No USB modem ports found (cu.usbmodem* / ttyACM* / ttyUSB*).")
    if len(ports) == 1:
        return ports[0]
    print(f"Probing {len(ports)} USB port(s) for repeater CLI …")
    hits: list[tuple[str, str]] = []
    for port in ports:
        ver = probe_repeater(port, baud=baud, timeout=min(timeout, 2.5), verbose=verbose)
        if ver:
            print(f"  {port}: {ver}")
            hits.append((port, ver))
        elif verbose:
            print(f"  {port}: skip")
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        raise SystemExit(
            "Multiple USB ports, none answered repeater CLI (`ver`).\n"
            "Pass the path: ./envybot onboard /dev/cu.usbmodem…"
        )
    listed = "\n".join(f"  {p}  ({v})" for p, v in hits)
    raise SystemExit(f"Multiple repeaters on USB. Pass one path:\n{listed}")


def require_ok(step: str, reply: str) -> str:
    if reply_failed(reply):
        raise CliError(f"{step}: {reply}")
    return reply


def parse_neighbors_cli(text: str) -> list[dict[str, Any]]:
    """CLI ``neighbors``: ``aabbccdd:secs:snr4`` lines, or ``-none-``. SNR is firmware×4."""
    body = text.strip()
    if not body or body.startswith("-none-"):
        return []
    out: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("->"):
            line = line.split("->", 1)[1].strip()
        if not line or line.startswith("-none-"):
            continue
        m = NEIGH_RE.match(line)
        if not m:
            continue
        out.append(
            {
                "pubkey": m.group(1).lower(),
                "secs_ago": int(m.group(2)),
                "snr": int(m.group(3)) / 4.0,
            }
        )
    return out


def format_neighbors(neighbors: list[dict[str, Any]]) -> str:
    if not neighbors:
        return "none"
    parts = [f"{n['pubkey']} {n['snr']:g}dB {n['secs_ago']}s" for n in neighbors]
    return f"{len(neighbors)}: " + ", ".join(parts)


def discover_baseline(
    cli: RepeaterSerial,
    *,
    rounds: int,
    wait_s: float,
) -> list[dict[str, Any]]:
    latest: list[dict[str, Any]] = []
    for i in range(1, rounds + 1):
        print(f"   round {i}/{rounds} discover.neighbors …")
        require_ok("discover.neighbors", cli.cmd("discover.neighbors"))
        time.sleep(wait_s)
        raw = cli.cmd("neighbors")
        if reply_failed(raw):
            raise CliError(f"neighbors: {raw}")
        latest = parse_neighbors_cli(raw)
        print(f"   {format_neighbors(latest)}")
    return latest


def open_repeater(port: str, *, baud: int, timeout: float, verbose: bool) -> RepeaterSerial:
    cli = RepeaterSerial(port, baud=baud, timeout=timeout, verbose=verbose)
    cli.__enter__()
    return cli


def reconnect_after_reboot(
    preferred: str,
    *,
    baud: int,
    timeout: float,
    verbose: bool,
) -> tuple[RepeaterSerial, str]:
    print(f"   waiting for USB …")
    time.sleep(REBOOT_SETTLE_S)
    deadline = time.monotonic() + RECONNECT_TIMEOUT_S
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        ports = []
        resolved = resolve_port_arg(preferred)
        if resolved:
            ports.append(resolved)
        for cand in serial_port_candidates():
            if cand not in ports:
                ports.append(cand)
        for port in ports:
            try:
                cli = open_repeater(port, baud=baud, timeout=timeout, verbose=verbose)
                try:
                    ver = cli.cmd("ver")
                except Exception:
                    cli.__exit__(None, None, None)
                    raise
                if ver and not reply_failed(ver):
                    print(f"   {port}: {ver}")
                    return cli, port
                cli.__exit__(None, None, None)
            except (CliError, serial.SerialException, OSError) as exc:
                last_err = exc
        time.sleep(1.0)
    detail = f" ({last_err})" if last_err else ""
    raise CliError(f"USB did not return after reboot{detail}")


def load_registry(nodes_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not nodes_path.is_file():
        raise SystemExit(f"nodes.yaml not found: {nodes_path}")
    doc = load_nodes_doc(nodes_path)
    nodes: dict[str, Any] = doc.setdefault("nodes", {})
    return doc, nodes


def resolve_unit(
    nodes: dict[str, Any],
    pubkey: str,
    *,
    unit: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    existing = find_unit_by_pubkey(nodes, pubkey)
    if unit:
        key = unit.lower()
        if existing and existing != key:
            raise SystemExit(f"pubkey already registered as {existing}; not using --unit {key}")
        node = nodes.get(key)
        have = str((node or {}).get("identity_pubkey") or "").strip().lower()
        if have and have != pubkey.strip().lower():
            raise SystemExit(f"--unit {key} already has a different pubkey")
        return key, node
    if existing:
        return existing, nodes[existing]
    return None, None


def resolve_passwords(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    admin_pw: str | None,
    guest_pw: str | None,
) -> tuple[str, str, str, str]:
    """Return (admin, guest, admin_source, guest_source). Sources: cli / yaml / device / new."""
    stored_admin = stored_admin_password(node)
    if admin_pw is not None:
        admin, admin_src = admin_pw, "cli"
    elif stored_admin:
        admin, admin_src = stored_admin, "yaml"
    else:
        admin, admin_src = gen_password(), "new"
    if not admin:
        raise CliError("admin password cannot be empty")

    stored_guest = stored_guest_password(node)
    if guest_pw is not None:
        guest, guest_src = guest_pw, "cli"
    elif stored_guest is not None:
        guest, guest_src = stored_guest, "yaml"
    else:
        device_guest = parse_get_value(cli.cmd("get guest.password"))
        if device_guest:
            guest, guest_src = device_guest, "device"
        else:
            guest, guest_src = gen_password(), "new"
    return admin, guest, admin_src, guest_src


def apply_if_needed(
    cli: RepeaterSerial,
    *,
    step: str,
    already: bool,
    setter: str,
    verify,
    ok_label: str,
    force: bool,
) -> bool:
    """SET only when needed. Returns True if a SET was sent."""
    if already and not force:
        print(f"   {ok_label} (already)")
        return False
    require_ok(step, cli.cmd(setter))
    if not verify():
        raise CliError(f"{step} verify failed")
    print(f"   {ok_label}")
    return True


def apply_dutycycle_policy(cli: RepeaterSerial, fw: str | None, *, force: bool) -> bool:
    """``set dutycycle 100``, or ``set af 0`` on MeshCore <1.15."""
    af = airtime_factor_for_dutycycle(DUTYCYCLE_PCT)
    use_af = firmware_has_dutycycle_cli(fw) is False
    if not use_af:
        dc = parse_dutycycle(cli.cmd("get dutycycle"))
        if dc is not None or firmware_has_dutycycle_cli(fw) is True:
            return apply_if_needed(
                cli,
                step="set dutycycle",
                already=dc is not None and abs(dc - DUTYCYCLE_PCT) <= 0.5,
                setter=f"set dutycycle {DUTYCYCLE_PCT}",
                verify=lambda: (
                    (got := parse_dutycycle(cli.cmd("get dutycycle"))) is not None
                    and abs(got - DUTYCYCLE_PCT) <= 0.5
                ),
                ok_label=f"{DUTYCYCLE_PCT:g}%",
                force=force,
            )
    got_af = parse_coord(cli.cmd("get af"))
    return apply_if_needed(
        cli,
        step="set af",
        already=got_af is not None and abs(got_af - af) < 0.05,
        setter=f"set af {af:g}",
        verify=lambda: (
            (got := parse_coord(cli.cmd("get af"))) is not None
            and abs(got - af) < 0.05
        ),
        ok_label=f"{DUTYCYCLE_PCT:g}% (af {af:g})",
        force=force,
    )


def onboard(
    cli: RepeaterSerial,
    *,
    admin_pw: str,
    guest_pw: str,
    admin_src: str,
    guest_src: str,
    force: bool,
    pubkey: str | None = None,
) -> dict[str, Any]:
    ver_raw = cli.cmd("ver")
    if reply_failed(ver_raw):
        raise CliError(f"not a repeater CLI: {ver_raw}")
    fw, platform = parse_firmware(ver_raw)
    print(f"   fw {ver_raw}")

    print("   bootloader …")
    bl, bl_raw = read_bootloader(cli)
    if bl:
        print(f"   bl {bl}")
    else:
        print(f"   bl unknown ({bl_raw})")

    if not pubkey:
        pubkey = normalize_hex(parse_get_value(cli.cmd("get public.key")) or "", PUB_HEX_LEN, "public.key")
    print(f"   pub {pubkey}")

    print("1. radio USA/Canada …")
    radio = parse_radio(cli.cmd("get radio"))
    radio_changed = apply_if_needed(
        cli,
        step="set radio",
        already=radio_matches(radio),
        setter=RADIO_CMD,
        verify=lambda: radio_matches(parse_radio(cli.cmd("get radio"))),
        ok_label=f"{RADIO_FREQ},{RADIO_BW},{RADIO_SF},{RADIO_CR}",
        force=force,
    )
    if radio_changed:
        print("   (reboot to apply)")

    print("2. dutycycle 100% …")
    apply_dutycycle_policy(cli, fw, force=force)

    print("3. adverts 0/0 …")
    adv = parse_int_get_value(cli.cmd("get advert.interval"))
    flood = parse_int_get_value(cli.cmd("get flood.advert.interval"))
    adverts_ok = adv == ADVERT_MIN and flood == FLOOD_ADVERT_H
    if adverts_ok and not force:
        print("   local 0m / flood 0h (already)")
    else:
        require_ok("set advert.interval", cli.cmd(f"set advert.interval {ADVERT_MIN}"))
        require_ok("set flood.advert.interval", cli.cmd(f"set flood.advert.interval {FLOOD_ADVERT_H}"))
        adv = parse_int_get_value(cli.cmd("get advert.interval"))
        flood = parse_int_get_value(cli.cmd("get flood.advert.interval"))
        if adv != ADVERT_MIN or flood != FLOOD_ADVERT_H:
            raise CliError(f"advert verify failed: local={adv} flood={flood}")
        print("   local 0m / flood 0h")

    print("4. admin password …")
    admin_changed = False
    if admin_src == "yaml" and not force:
        print("   kept (yaml)")
    else:
        require_ok("password", cli.cmd(f"password {admin_pw}"))
        admin_changed = admin_src != "yaml"
        print("   set" if admin_changed else "   confirmed")

    print("5. guest password …")
    device_guest = parse_get_value(cli.cmd("get guest.password"))
    if device_guest is None:
        device_guest = ""
    guest_changed = False
    if device_guest == guest_pw and not force:
        print("   kept (already)")
    else:
        require_ok("set guest.password", cli.cmd(f"set guest.password {guest_pw}"))
        got_guest = parse_get_value(cli.cmd("get guest.password")) or ""
        if got_guest != guest_pw:
            raise CliError(f"guest.password verify failed")
        guest_changed = device_guest != guest_pw
        print("   set" if guest_changed else "   confirmed")

    print(f"6. position {ONBOARD_LAT}, {ONBOARD_LON} …")
    lat = parse_coord(cli.cmd("get lat"))
    lon = parse_coord(cli.cmd("get lon"))
    pos_ok = coords_match(lat, ONBOARD_LAT) and coords_match(lon, ONBOARD_LON)
    if pos_ok and not force:
        print("   already")
    else:
        require_ok("set lat", cli.cmd(f"set lat {ONBOARD_LAT}"))
        require_ok("set lon", cli.cmd(f"set lon {ONBOARD_LON}"))
        lat = parse_coord(cli.cmd("get lat"))
        lon = parse_coord(cli.cmd("get lon"))
        if not (coords_match(lat, ONBOARD_LAT) and coords_match(lon, ONBOARD_LON)):
            raise CliError(f"position verify failed: {lat}, {lon}")
        print("   set")

    print(f"7. name {ONBOARD_NAME} …")
    name = parse_get_value(cli.cmd("get name")) or ""
    apply_if_needed(
        cli,
        step="set name",
        already=name == ONBOARD_NAME,
        setter=f"set name {ONBOARD_NAME}",
        verify=lambda: (parse_get_value(cli.cmd("get name")) or "") == ONBOARD_NAME,
        ok_label=ONBOARD_NAME,
        force=force,
    )

    print("8. secret key …")
    prv = normalize_hex(parse_get_value(cli.cmd("get prv.key")) or "", PRV_HEX_LEN, "prv.key")
    print(f"   {prv}")

    print("9. clock …")
    now = int(time.time())
    clock_reply = cli.cmd(f"time {now}")
    if "cannot go backwards" in clock_reply.lower():
        print(f"   skipped ({clock_reply})")
    else:
        require_ok("time", clock_reply)
        print(f"   {clock_reply}")

    return {
        "pubkey": pubkey,
        "secret": prv,
        "admin_password": admin_pw,
        "guest_password": guest_pw,
        "admin_changed": admin_changed,
        "guest_changed": guest_changed,
        "radio_changed": radio_changed,
        "firmware_version": fw,
        "firmware_platform": platform or "meshcore",
        "firmware_raw": ver_raw,
        "bootloader_version": bl,
        "node_clock": now,
        "neighbors": None,
    }


def register(nodes_path: Path, result: dict[str, Any], *, unit: str | None) -> tuple[str, bool]:
    doc, nodes = load_registry(nodes_path)
    pubkey = result["pubkey"]
    existing = find_unit_by_pubkey(nodes, pubkey)
    if unit:
        key = unit.lower()
        if existing and existing != key:
            raise SystemExit(
                f"pubkey already registered as {existing}; not writing --unit {key}"
            )
        created = key not in nodes
        unit_id = key.upper()
        node = nodes.get(key) or new_node(unit_id)
        have = str(node.get("identity_pubkey") or "").strip().lower()
        if have and have != pubkey.strip().lower():
            raise SystemExit(f"--unit {key} already has a different pubkey")
        node["unit_id"] = unit_id
        nodes[key] = node
        remember_unit_id(doc, unit_id)
    elif existing:
        key = existing
        created = False
        node = nodes[key]
        if not node.get("unit_id"):
            node["unit_id"] = key.upper()
    else:
        unit_id = allocate_unit_id(doc)
        key = unit_id.lower()
        created = True
        node = new_node(unit_id)
        nodes[key] = node

    now = int(time.time())
    if node.get("name") == ONBOARD_NAME:
        node.pop("name", None)
    node["firmware_platform"] = result["firmware_platform"]
    node["admin_password"] = result["admin_password"]
    node["guest_password"] = result["guest_password"]
    node["identity_pubkey"] = result["pubkey"]
    node["identity_secret"] = result["secret"]
    node.pop("lat", None)
    node.pop("lon", None)
    if result.get("admin_changed"):
        node["last_admin_roll"] = now
    if result.get("guest_changed"):
        node["last_guest_roll"] = now
    if "site" not in node:
        node["site"] = None
    write_nodes_doc(nodes_path, doc)
    conn = open_history(nodes_path.parent)
    record_onboard_heard(
        conn,
        unit=key,
        firmware_version=result.get("firmware_version"),
        bootloader_version=result.get("bootloader_version"),
        firmware_platform=result.get("firmware_platform"),
        node_clock=result.get("node_clock"),
        neighbors=result.get("neighbors"),
        ts=now,
    )
    conn.close()
    return key, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "port",
        nargs="?",
        help="USB modem path (or usbmodemNNNN). Omit to autodetect.",
    )
    parser.add_argument("--unit", metavar="me0041", help="Bind to this nodes.yaml key (create if missing)")
    parser.add_argument("--admin-pw", help="Admin password (generated if omitted; yaml reused on re-run)")
    parser.add_argument("--guest-pw", help="Guest password (generated if omitted; '' for blank)")
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    parser.add_argument("--no-write", action="store_true", help="Do not update nodes.yaml")
    parser.add_argument("--force", action="store_true", help="Re-SET even when already matching (implies radio reboot)")
    parser.add_argument("--no-reboot", action="store_true", help="Skip reboot (radio stays pending)")
    parser.add_argument("--no-discover", action="store_true", help="Skip neighbor discover/fetch rounds")
    parser.add_argument("--discover-rounds", type=int, default=DISCOVER_ROUNDS, metavar="N")
    parser.add_argument("--discover-wait", type=float, default=DISCOVER_WAIT_S, metavar="SEC")
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    port = pick_port(args.port, baud=args.baud, timeout=args.timeout, verbose=args.verbose)
    print(f"USB {port}")
    cli: RepeaterSerial | None = None
    try:
        cli = open_repeater(port, baud=args.baud, timeout=args.timeout, verbose=args.verbose)
        pub = normalize_hex(parse_get_value(cli.cmd("get public.key")) or "", PUB_HEX_LEN, "public.key")
        _doc, nodes = load_registry(args.nodes)
        unit_key, existing = resolve_unit(nodes, pub, unit=args.unit)
        if unit_key:
            print(f"unit {unit_key.upper()} (existing)")
        else:
            print("unit (new)")
        admin_pw, guest_pw, admin_src, guest_src = resolve_passwords(
            cli, existing, admin_pw=args.admin_pw, guest_pw=args.guest_pw
        )
        print(f"admin {admin_pw}  ({admin_src})")
        print(f"guest {guest_pw or '(blank)'}  ({guest_src})")

        result = onboard(
            cli,
            admin_pw=admin_pw,
            guest_pw=guest_pw,
            admin_src=admin_src,
            guest_src=guest_src,
            force=args.force,
            pubkey=pub,
        )
        if not args.no_write:
            unit_key, created = register(args.nodes, result, unit=unit_key or args.unit)
            result["admin_changed"] = False
            result["guest_changed"] = False
            print(f"nodes.yaml {'created' if created else 'updated'} {unit_key.upper()}")
        else:
            print("nodes.yaml not written (--no-write)")

        do_discover = not args.no_discover and args.discover_rounds > 0

        if args.no_reboot:
            print("reboot skipped — radio USA/Canada applies after reboot")
        elif result["radio_changed"]:
            print("reboot (radio apply) …")
            cli.cmd("reboot", expect_reply=False)
            cli.__exit__(None, None, None)
            cli = None
        else:
            print("reboot skipped — radio already applied")

        if do_discover:
            print(f"10. neighbor baseline ({args.discover_rounds}×, {args.discover_wait:g}s) …")
            if cli is None:
                cli, port = reconnect_after_reboot(
                    port, baud=args.baud, timeout=args.timeout, verbose=args.verbose
                )
            result["neighbors"] = discover_baseline(
                cli, rounds=args.discover_rounds, wait_s=args.discover_wait
            )
            if not args.no_write:
                register(args.nodes, result, unit=unit_key or args.unit)
                print(f"nodes.yaml neighbors {format_neighbors(result['neighbors'])}")
    except (CliError, serial.SerialException, OSError) as exc:
        print(f"onboard failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cli is not None:
            cli.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
