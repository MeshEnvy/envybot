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


def serve_loop(
    ser: Any,
    core: SeederCore,
    *,
    verbose: bool = False,
    stop: threading.Event | None = None,
    on_tag_line: Callable[[str], None] | None = None,
    on_host_line: Callable[[str], None] | None = None,
    enable_folder: bool = False,
) -> None:
    """Run until `stop` is set or serial closes."""
    stop = stop or threading.Event()
    on_tag = on_tag_line or (lambda _s: None)
    on_host = on_host_line or (lambda _s: None)
    attach_folder = enable_folder
    log_tail_pending = enable_folder
    duty_pending = True
    gc_pending = True
    announced = False
    prev: int | None = None
    line = ""

    while not stop.is_set():
        if gc_pending:
            gc_pending = False
            doctor_gc(ser)
            on_host("sent `doctor gc`")
            time.sleep(0.4)
            continue

        if duty_pending:
            duty_pending = False
            set_dutycycle(ser, SEEDER_DUTYCYCLE_PCT)
            on_host(f"sent `set dutycycle {SEEDER_DUTYCYCLE_PCT}`")
            time.sleep(0.3)
            continue

        if log_tail_pending:
            log_tail_pending = False
            log_tail_on(ser)
            on_host("sent `log tail on`")
            time.sleep(0.3)
            continue

        if attach_folder:
            attach_folder = False
            folder_off(ser)
            time.sleep(0.3)
            folder_on(ser)
            on_host("sent `ota folder on`")
            continue

        b = read_byte(ser)
        if b is None:
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
                    on_host(log_request(op, args, status, payload))
                    if enable_folder and not announced and op == OP_COUNT and status == STATUS_OK:
                        announced = True
                        time.sleep(0.2)
                        announce(ser)
                        on_host("sent `ota announce`")
            continue

        if prev is not None:
            line += chr(prev)
            if prev == ord("\n") or len(line) > 512:
                trimmed = line.rstrip("\r\n")
                if trimmed:
                    on_tag(trimmed)
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
    import serial

    return serial.Serial(
        port=port,
        baudrate=baud,
        timeout=LINK_TIMEOUT_S,
        write_timeout=LINK_TIMEOUT_S,
    )


def run_seeder(
    directory: Path,
    port: str,
    *,
    baud: int = 115200,
    recursive: bool = False,
    only_globs: list[str] | None = None,
    verbose: bool = False,
    no_enable: bool = False,
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
        "[host] tag RX/TX mirrors here via `log tail on`; MS folder ops logged as [host] lines.",
        flush=True,
    )

    ser = open_serial_port(port, baud)
    stop = threading.Event()
    core = SeederCore(folder)

    def on_sig(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    serve_thread = threading.Thread(
        target=serve_loop,
        kwargs={
            "ser": ser,
            "core": core,
            "verbose": verbose,
            "stop": stop,
            "enable_folder": not no_enable,
            "on_tag_line": lambda s: print(s, flush=True),
            "on_host_line": lambda s: print(f"[host] {s}", flush=True),
        },
        daemon=True,
    )

    try:
        time.sleep(0.3)
        serve_thread.start()
        while not stop.is_set():
            serve_thread.join(timeout=0.5)
    finally:
        if not no_enable:
            try:
                folder_off(ser)
            except OSError:
                pass
        ser.close()
    return 0
