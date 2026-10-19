"""USB mota-seeder host: serve a folder of `.mota` over serial (MS/ms framing)."""

from __future__ import annotations

import fnmatch
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from envybot.mota import (
    DESC_WIRE,
    ServedMota,
    describe_wire,
    load_served_mota,
    version_str,
)

# Mirror MotaSeederProto.h / motatool format::seeder
REQ_MAGIC = b"MS"
RSP_MAGIC = b"ms"
OP_COUNT = 0x01
OP_DESCRIBE = 0x02
OP_READ = 0x03
OP_STAT = 0x04
OP_BEGIN = 0x05
OP_WRITE = 0x06
OP_SREAD = 0x07
OP_FIN = 0x08
STATUS_OK = 0x00
STATUS_ERR = 0x01
WRITE_MAX = 512
LINK_TIMEOUT_S = 0.5
SEEDER_DUTYCYCLE_PCT = 10  # standing cap; 1–2% is a manual daytime drip only
DEFAULT_ANNOUNCE_INTERVAL_S = 60.0  # re-flood OTA beacon while seed holds the USB console
DEFAULT_STATUS_INTERVAL_S = 45.0  # poll tag `ota folder` / `ota stats` over USB (0 = off)
CLI_POLL_IDLE_S = 0.35
CLI_REPLY_IDLE_S = 0.25  # match onboard RepeaterSerial
CLI_CMD_TIMEOUT_S = 5.0


def xor_bytes(data: bytes, seed: int = 0) -> int:
    out = seed
    for b in data:
        out ^= b
    return out & 0xFF


def request_header_len(op: int) -> int | None:
    if op == OP_COUNT:
        return 0
    if op == OP_DESCRIBE:
        return 1
    if op == OP_READ:
        return 7
    if op in (OP_STAT, OP_FIN):
        return 4
    if op == OP_BEGIN:
        return 8
    if op in (OP_SREAD, OP_WRITE):
        return 10
    return None


class SeederFolder:
    """Sorted catalog of valid `.mota` files."""

    def __init__(self, motas: list[ServedMota]) -> None:
        self._motas = motas

    def count(self) -> int:
        return len(self._motas)

    def at(self, idx: int) -> ServedMota | None:
        if idx < 0 or idx >= len(self._motas):
            return None
        return self._motas[idx]

    def all(self) -> list[ServedMota]:
        return list(self._motas)


def scan_folder(
    directory: Path,
    *,
    recursive: bool = False,
    only_globs: list[str] | None = None,
    warn: Callable[[Path, str], None] | None = None,
) -> SeederFolder:
    """Scan `directory` for valid `.mota` files in deterministic order."""
    warn = warn or (lambda _p, _m: None)
    directory = directory.resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"not a directory: {directory}")

    paths: list[Path] = []
    if recursive:
        paths = sorted(directory.rglob("*.mota"))
    else:
        paths = sorted(directory.glob("*.mota"))

    motas: list[ServedMota] = []
    for path in paths:
        if not path.is_file():
            continue
        name = path.name
        if only_globs and not any(fnmatch.fnmatch(name, g) for g in only_globs):
            continue
        served = load_served_mota(path)
        if served is None:
            warn(path, "invalid or unreadable .mota")
            continue
        motas.append(served)

    motas.sort(key=lambda s: str(s.path))
    return SeederFolder(motas)


class SeederCore:
    """Transport-agnostic seeder dispatch (COUNT / DESCRIBE / READ)."""

    def __init__(self, folder: SeederFolder) -> None:
        self._folder = folder

    @property
    def folder(self) -> SeederFolder:
        return self._folder

    def handle(self, op: int, args: bytes) -> tuple[int, bytes] | None:
        if op == OP_COUNT:
            n = min(self._folder.count(), 255)
            return STATUS_OK, bytes([n])

        if op == OP_DESCRIBE:
            if len(args) < 1:
                return STATUS_ERR, b""
            idx = args[0]
            served = self._folder.at(idx)
            if served is None:
                return STATUS_ERR, b""
            return STATUS_OK, describe_wire(served)

        if op == OP_READ:
            if len(args) < 7:
                return STATUS_ERR, b""
            idx = args[0]
            off = int.from_bytes(args[1:5], "little")
            length = int.from_bytes(args[5:7], "little")
            served = self._folder.at(idx)
            if served is None or off + length > len(served.bytes):
                return STATUS_ERR, b""
            return STATUS_OK, served.bytes[off : off + length]

        if op in (OP_STAT, OP_BEGIN, OP_WRITE, OP_SREAD, OP_FIN):
            return STATUS_ERR, b""

        return None


def read_request_body(ser: Any, op: int, args_hdr: bytes) -> bytes | None:
    """Read optional WRITE payload after fixed header."""
    if op != OP_WRITE:
        return args_hdr
    if len(args_hdr) < 10:
        return None
    dlen = int.from_bytes(args_hdr[8:10], "little")
    if dlen > WRITE_MAX:
        return None
    if dlen == 0:
        return args_hdr
    extra = _read_exact(ser, dlen)
    if extra is None:
        return None
    return args_hdr + extra


def _read_exact(ser: Any, n: int) -> bytes | None:
    buf = bytearray()
    deadline = time.monotonic() + LINK_TIMEOUT_S
    while len(buf) < n:
        if time.monotonic() > deadline:
            return None
        chunk = _serial_read(ser, n - len(buf))
        if not chunk:
            time.sleep(0.01)
            continue
        buf.extend(chunk)
    return bytes(buf)


def _serial_read(ser: Any, n: int) -> bytes:
    """Read up to n bytes; empty on timeout or transient serial glitch."""
    try:
        return ser.read(n)
    except OSError:
        return b""


def read_byte(ser: Any) -> int | None:
    """Read one byte; None on timeout."""
    deadline = time.monotonic() + LINK_TIMEOUT_S
    while time.monotonic() <= deadline:
        b = _serial_read(ser, 1)
        if b:
            return b[0]
        time.sleep(0.01)
    return None


def read_request(ser: Any) -> tuple[int, bytes] | None:
    """Read one MS frame after magic; None if corrupt or timeout."""
    op_b = _read_exact(ser, 1)
    if op_b is None:
        return None
    op = op_b[0]
    hdr_len = request_header_len(op)
    if hdr_len is None:
        return None
    args = _read_exact(ser, hdr_len) if hdr_len else b""
    if args is None:
        return None
    args = read_request_body(ser, op, args)
    if args is None:
        return None
    xsum_b = _read_exact(ser, 1)
    if xsum_b is None:
        return None
    if xor_bytes(args, op) != xsum_b[0]:
        return None
    return op, args


def send_response(ser: Any, op: int, status: int, payload: bytes) -> None:
    frame = bytearray()
    frame.extend(RSP_MAGIC)
    frame.append(op)
    frame.append(status)
    frame.extend(payload)
    frame.append(xor_bytes(frame))
    ser.write(frame)
    ser.flush()


def format_tag_line(line: str) -> str:
    """Highlight serial lines that often correlate with mesh OTA activity."""
    low = line.lower()
    if any(
        k in low
        for k in (
            "ota",
            "lora",
            "mesh",
            "query",
            "have",
            "advert",
            "mota",
            "folder",
            "fetch",
            "seeder",
        )
    ):
        return f"[tag|ota] {line}"
    return f"[tag] {line}"


def _drain_serial(ser: Any, max_s: float = 0.12) -> None:
    deadline = time.monotonic() + max_s
    old_timeout = getattr(ser, "timeout", None)
    try:
        ser.timeout = 0.02
        while time.monotonic() < deadline:
            if not ser.read(256):
                time.sleep(0.02)
    finally:
        if old_timeout is not None:
            ser.timeout = old_timeout


def cli_cmd_reply(ser: Any, command: str, *, timeout: float = CLI_CMD_TIMEOUT_S) -> str | None:
    """One MeshCore CLI command; return text after ``->`` (same rules as onboard USB)."""
    from envybot.commands.onboard import RepeaterSerial

    _drain_serial(ser)
    ser.write((command.strip() + "\r").encode("ascii", errors="replace"))
    ser.flush()
    deadline = time.monotonic() + timeout
    ser.timeout = 0.05
    buf = ""
    saw = False
    last = time.monotonic()
    while time.monotonic() < deadline:
        chunk = ser.read(512)
        now = time.monotonic()
        if chunk:
            buf += chunk.decode("utf-8", errors="replace")
            last = now
            if RepeaterSerial._reply_start(buf) is not None:
                saw = True
        elif saw and now - last >= CLI_REPLY_IDLE_S:
            break
        else:
            time.sleep(0.02)
    return RepeaterSerial._extract_reply(buf)


def poll_tag_cli(ser: Any, command: str, *, timeout: float = 2.5) -> list[str]:
    """Send one CLI line on the tag console; collect reply lines until idle."""
    reply = cli_cmd_reply(ser, command, timeout=timeout)
    if not reply:
        return []
    return [ln.strip() for ln in reply.splitlines() if ln.strip()]


def log_request(
    op: int,
    args: bytes,
    status: int,
    payload: bytes,
) -> str:
    ok = "OK" if status == STATUS_OK else "ERR"
    if op == OP_COUNT:
        n = payload[0] if payload else 0
        return f"COUNT -> {n} mota(s) in folder"
    if op == OP_DESCRIBE:
        idx = args[0] if args else 0
        if status == STATUS_OK and len(payload) >= DESC_WIRE:
            mid = payload[0:4].hex().upper()
            target = int.from_bytes(payload[4:8], "little")
            fw = int.from_bytes(payload[8:12], "little")
            codec = payload[12]
            from envybot.mota import CODEC_LABELS

            cl = CODEC_LABELS.get(codec, "?")
            return (
                f"DESCRIBE idx={idx} {ok} mid={mid} target={target:08X} "
                f"v{version_str(fw)} {cl}"
            )
        return f"DESCRIBE idx={idx} {ok}"
    if op == OP_READ:
        idx = args[0] if args else 0
        off = int.from_bytes(args[1:5], "little") if len(args) >= 5 else 0
        length = int.from_bytes(args[5:7], "little") if len(args) >= 7 else 0
        return f"READ idx={idx} @{off} len={length} {ok}"
    return f"op={op:#04x} {ok}"


def _emit_identity_from_replies(
    replies: dict[str, str | None],
    *,
    on_host: Callable[[str], None],
) -> None:
    on_host("seeder tag:")
    for label in (
        "name",
        "public.key",
        "radio",
        "tx",
        "dutycycle",
        "radio.rxgain",
        "radio.fem.rxgain",
        "radio.fem.txgain",
    ):
        raw = replies.get(label)
        if raw is None:
            on_host(f"  {label}: (no reply)")
        else:
            on_host(f"  {label}: {_cli_setting(raw)}")


def setup_seeder_tag_cli(
    cli: Any,
    *,
    dutycycle_pct: int,
    on_host: Callable[[str], None],
) -> dict[str, str | None]:
    """MeshCore USB setup via RepeaterSerial (``\\r`` lines, ``->`` replies)."""
    from envybot.commands.onboard import CliError

    def run(cmd: str) -> str | None:
        try:
            reply = cli.cmd(cmd)
            on_host(f"cli `{cmd}` ok")
            return reply
        except CliError as exc:
            on_host(f"cli `{cmd}` failed: {exc}")
            return None

    run("doctor gc")
    run(f"set dutycycle {dutycycle_pct}")
    run("log tail on")

    identity_cmds = {
        "name": "get name",
        "public.key": "get public.key",
        "radio": "get radio",
        "tx": "get tx",
        "dutycycle": "get dutycycle",
        "radio.rxgain": "get radio.rxgain",
        "radio.fem.rxgain": "get radio.fem.rxgain",
        "radio.fem.txgain": "get radio.fem.txgain",
    }
    replies: dict[str, str | None] = {}
    for label, cmd in identity_cmds.items():
        try:
            replies[label] = cli.cmd(cmd)
        except CliError:
            replies[label] = None
    _emit_identity_from_replies(replies, on_host=on_host)
    return replies


def tag_cmd(ser: Any, command: str, *, timeout: float = CLI_CMD_TIMEOUT_S) -> str | None:
    return cli_cmd_reply(ser, command, timeout=timeout)


def _emit_status_poll(
    ser: Any,
    *,
    on_host: Callable[[str], None],
    ms_since_poll: dict[int, int],
) -> None:
    for cmd in ("ota folder", "ota stats"):
        for line in poll_tag_cli(ser, cmd):
            on_host(f"poll `{cmd}`: {line}")
    parts = []
    for op, label in (
        (OP_COUNT, "COUNT"),
        (OP_DESCRIBE, "DESCRIBE"),
        (OP_READ, "READ"),
    ):
        n = ms_since_poll.get(op, 0)
        if n:
            parts.append(f"{label}={n}")
    if parts:
        on_host(
            "usb-mota since last poll: "
            + ", ".join(parts)
            + " (mesh fetch pulls blocks over USB READ)"
        )
    else:
        on_host(
            "usb-mota since last poll: none — DUT `ota ls` is LoRa-only; "
            "READ appears when a peer downloads from this folder"
        )
    ms_since_poll.clear()


def serve_loop(
    ser: Any,
    core: SeederCore,
    *,
    verbose: bool = False,
    stop: threading.Event | None = None,
    on_tag_line: Callable[[str], None] | None = None,
    on_host_line: Callable[[str], None] | None = None,
    enable_folder: bool = False,
    announce_interval_s: float = DEFAULT_ANNOUNCE_INTERVAL_S,
    status_interval_s: float = DEFAULT_STATUS_INTERVAL_S,
    announced: bool = False,
    last_status_at: float = 0.0,
) -> None:
    """Run until `stop` is set or serial closes."""
    stop = stop or threading.Event()
    on_tag = on_tag_line or (lambda _s: None)
    on_host = on_host_line or (lambda _s: None)
    attach_folder = enable_folder
    last_announce_at = time.monotonic() if announced else 0.0
    last_ms_at = 0.0
    ms_since_poll: dict[int, int] = {}
    prev: int | None = None
    line = ""

    def mesh_announce(label: str) -> None:
        nonlocal announced, last_announce_at
        tag_cmd(ser, "ota announce")
        announced = True
        last_announce_at = time.monotonic()
        on_host(f"cli `ota announce` ({label})")

    while not stop.is_set():
        if attach_folder:
            attach_folder = False
            tag_cmd(ser, "ota folder off")
            time.sleep(0.2)
            tag_cmd(ser, "ota folder on")
            on_host("cli `ota folder on`")
            time.sleep(0.4)
            mesh_announce("after folder on")
            if status_interval_s > 0 and last_status_at <= 0:
                time.sleep(0.3)
                _emit_status_poll(ser, on_host=on_host, ms_since_poll=ms_since_poll)
                last_status_at = time.monotonic()
            continue

        b = read_byte(ser)
        if b is None:
            now = time.monotonic()
            if (
                enable_folder
                and announce_interval_s > 0
                and last_announce_at > 0
                and now - last_announce_at >= announce_interval_s
            ):
                mesh_announce("periodic")
            if (
                enable_folder
                and status_interval_s > 0
                and last_status_at > 0
                and now - last_status_at >= status_interval_s
                and now - last_ms_at >= 3.0
            ):
                prev = None
                line = ""
                _emit_status_poll(ser, on_host=on_host, ms_since_poll=ms_since_poll)
                last_status_at = now
            continue

        if prev == ord("M") and b == ord("S"):
            prev = None
            req = read_request(ser)
            if req is not None:
                op, args = req
                handled = core.handle(op, args)
                if handled is not None:
                    status, payload = handled
                    send_response(ser, op, status, payload)
                    ms_since_poll[op] = ms_since_poll.get(op, 0) + 1
                    last_ms_at = time.monotonic()
                    msg = log_request(op, args, status, payload)
                    on_host(f"usb-mota {msg}")
                    if verbose:
                        on_host(f"(verbose) op={op:#04x} args={args.hex()}")
                    if enable_folder and not announced and op == OP_COUNT and status == STATUS_OK:
                        time.sleep(0.2)
                        mesh_announce("after folder COUNT")
            continue

        if prev is not None:
            line += chr(prev)
            if prev == ord("\n") or len(line) > 512:
                trimmed = line.rstrip("\r\n")
                if trimmed:
                    on_tag(format_tag_line(trimmed))
                line = ""
        prev = b


def folder_on(ser: Any) -> None:
    ser.write(b"ota folder on\r\n")
    ser.flush()


def folder_off(ser: Any) -> None:
    ser.write(b"ota folder off\r\n")
    ser.flush()


def announce(ser: Any) -> None:
    ser.write(b"ota announce\r\n")
    ser.flush()


def log_tail_on(ser: Any) -> None:
    ser.write(b"log tail on\r\n")
    ser.flush()


def set_dutycycle(ser: Any, pct: int) -> None:
    ser.write(f"set dutycycle {pct}\r\n".encode())
    ser.flush()


def doctor_gc(ser: Any) -> None:
    ser.write(b"doctor gc\r\n")
    ser.flush()


def print_catalog(folder: SeederFolder) -> None:
    print("#  file  version  codec  target  mid  base_hash")
    for i, s in enumerate(folder.all()):
        m = s.manifest
        print(
            f"{i}  {s.path.name}  {m.fw_version_str()}  {m.codec_label()}  "
            f"{m.target_hex()}  {m.mid_hex()}  {m.base_hash_hex()}"
        )


def open_serial_port(port: str, baud: int) -> Any:
    """Open like onboard RepeaterSerial (no DTR reset, short read timeout)."""
    import serial

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.05
    ser.write_timeout = 2
    ser.dsrdtr = False
    ser.rtscts = False
    ser.open()
    return ser


def _cli_setting(raw: str) -> str:
    from envybot.commands.onboard import reply_failed
    from envybot.radio import parse_get_value

    if reply_failed(raw):
        return "n/a"
    return parse_get_value(raw) or raw.strip().replace("\n", " ")[:120]


def run_seeder(
    directory: Path,
    port: str,
    *,
    baud: int = 115200,
    recursive: bool = False,
    only_globs: list[str] | None = None,
    verbose: bool = False,
    no_enable: bool = False,
    announce_interval_s: float = DEFAULT_ANNOUNCE_INTERVAL_S,
    status_interval_s: float = DEFAULT_STATUS_INTERVAL_S,
    dutycycle_pct: int = SEEDER_DUTYCYCLE_PCT,
) -> int:
    """Scan folder, enable ota folder relay, serve until interrupt."""

    def warn(path: Path, msg: str) -> None:
        print(f"skip {path.name}: {msg}", file=sys.stderr)

    folder = scan_folder(
        directory,
        recursive=recursive,
        only_globs=only_globs,
        warn=warn,
    )
    if folder.count() == 0:
        print(f"error: no valid .mota in {directory}", file=sys.stderr)
        return 1

    print(f"[host] {folder.count()} valid .mota in {directory}", flush=True)
    print_catalog(folder)
    print(
        "[host] DUT `ota ls` / QUERY / HAVE are LoRa — not shown unless `log tail` prints them.",
        flush=True,
    )
    print(
        "[host] Mesh download shows here as `[host] usb-mota READ …` when the tag pulls from this folder.",
        flush=True,
    )
    if status_interval_s > 0:
        print(
            f"[host] Tag `ota folder` / `ota stats` polled every {status_interval_s:g}s "
            "(`--status-interval 0` to disable).",
            flush=True,
        )
    print(f"[host] USB {port} ({baud} baud)", flush=True)

    from envybot.companion_seed import companion_folder_off, companion_setup
    from envybot.seed_usb import detect_usb_role

    role = detect_usb_role(port, baud=baud, verbose=verbose)
    if role == "unknown":
        print(
            "error: port is not a MeshCore companion (appstart) or repeater text console.\n"
            "  EnvyBot seed expects companion USB (e.g. RAK_WisMesh_Tag_bot_usb / EnvyBot).",
            file=sys.stderr,
            flush=True,
        )
        return 1

    stop = threading.Event()
    core = SeederCore(folder)
    on_host_line = lambda s: print(f"[host] {s}", flush=True)

    def on_sig(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    folder_already_on = False
    repeater_cli: Any = None
    if role == "companion":
        print("[host] USB role: companion (CMD_RUN_CLI + raw mota-seeder)", flush=True)
        if announce_interval_s > 0 or status_interval_s > 0:
            print(
                "[host] Companion raw MS phase: periodic announce/status USB polls are "
                "off (ota announce sent once at start). Re-flash tag if `ota folder on` fails.",
                flush=True,
            )
        try:
            companion_setup(
                port,
                baud=baud,
                dutycycle_pct=dutycycle_pct,
                enable_folder=not no_enable,
                verbose=verbose,
                on_host=on_host_line,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return 1
        folder_already_on = not no_enable
        ser = open_serial_port(port, baud)
    else:
        print("[host] USB role: repeater text console (legacy motatool-style)", flush=True)
        from envybot.commands.onboard import DEFAULT_TIMEOUT, RepeaterSerial

        repeater_cli = RepeaterSerial(port, baud=baud, timeout=DEFAULT_TIMEOUT, verbose=verbose)
        repeater_cli.__enter__()
        setup_seeder_tag_cli(repeater_cli, dutycycle_pct=dutycycle_pct, on_host=on_host_line)
        ser = repeater_cli.ser

    try:
        companion_raw = role == "companion"
        serve_thread = threading.Thread(
            target=serve_loop,
            kwargs={
                "ser": ser,
                "core": core,
                "verbose": verbose,
                "stop": stop,
                "enable_folder": not no_enable and not folder_already_on,
                "announce_interval_s": 0.0 if companion_raw else announce_interval_s,
                "status_interval_s": 0.0 if companion_raw else status_interval_s,
                "announced": folder_already_on,
                "last_status_at": 0.0,
                "on_tag_line": lambda s: print(s, flush=True),
                "on_host_line": on_host_line,
            },
            daemon=True,
        )
        serve_thread.start()
        while not stop.is_set():
            serve_thread.join(timeout=0.5)
    finally:
        if not no_enable:
            if role == "companion":
                companion_folder_off(port, baud=baud, verbose=verbose)
            elif repeater_cli is not None:
                from envybot.commands.onboard import CliError

                try:
                    repeater_cli.cmd("ota folder off")
                except (CliError, OSError):
                    try:
                        folder_off(ser)
                    except OSError:
                        pass
        if repeater_cli is not None:
            repeater_cli.__exit__(None, None, None)
        else:
            try:
                ser.close()
            except OSError:
                pass
    return 0
