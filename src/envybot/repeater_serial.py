"""MeshCore repeater USB text CLI (CR-terminated, ``  -> `` replies)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pyserial not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot onboard"
    ) from exc

from envybot.radio import serial_port_candidates

BAUD = 115200
SETTLE_S = 2.5
DEFAULT_TIMEOUT = 5.0
REPLY_IDLE_S = 0.25
ONBOARD_CMD_WAIT_DEADLINE_S = 45.0
ONBOARD_CMD_RETRY_DELAY_S = 0.4


class CliError(RuntimeError):
    pass


def reply_failed(reply: str) -> bool:
    body = reply.strip()
    if body.startswith(">"):
        body = body[1:].strip()
    upper = body.upper()
    return upper.startswith("ERR") or upper.startswith("UNKNOWN") or upper.startswith("ERROR")


class RepeaterSerial:
    """MeshCore repeater USB text CLI (CR-terminated, ``  -> `` replies)."""

    def __init__(self, port: str, *, baud: int = BAUD, timeout: float = DEFAULT_TIMEOUT, verbose: bool = False):
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.wait_for_reply = False
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

    def send_command(
        self,
        command: str,
        *,
        should_wait: bool | None = None,
        expect_reply: bool = True,
        timeout: float | None = None,
        wait_deadline_s: float | None = None,
    ) -> str:
        """Send one CR-terminated CLI line. When ``should_wait``, retry until a reply or deadline."""
        if not expect_reply:
            self._drain()
            if self.verbose:
                print(f"    > {command}")
            self._write_raw((command + "\r").encode("ascii", errors="strict"))
            return ""

        wait = self.wait_for_reply if should_wait is None else should_wait
        deadline = (
            time.monotonic() + (wait_deadline_s or ONBOARD_CMD_WAIT_DEADLINE_S)
            if wait
            else None
        )
        delay = ONBOARD_CMD_RETRY_DELAY_S
        last_err: CliError | None = None
        while True:
            try:
                return self._cmd_once(command, expect_reply=True, timeout=timeout)
            except CliError as exc:
                if "no CLI reply" not in str(exc):
                    raise
                last_err = exc
                if not wait:
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise last_err
                if self.verbose:
                    print("    (no reply, retry …)")
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)

    def cmd(
        self,
        command: str,
        *,
        expect_reply: bool = True,
        timeout: float | None = None,
        should_wait: bool | None = None,
        wait_deadline_s: float | None = None,
        attempts: int | None = None,
    ) -> str:
        """Alias for :meth:`send_command` (legacy ``attempts`` maps to a short fixed retry cap)."""
        if attempts is not None and should_wait is None and expect_reply:
            if attempts <= 1:
                should_wait = False
            else:
                wait_deadline_s = wait_deadline_s or (
                    ONBOARD_CMD_RETRY_DELAY_S * attempts + (self.timeout * attempts)
                )
                should_wait = True
        return self.send_command(
            command,
            should_wait=should_wait,
            expect_reply=expect_reply,
            timeout=timeout,
            wait_deadline_s=wait_deadline_s,
        )

    def _cmd_once(
        self,
        command: str,
        *,
        expect_reply: bool = True,
        timeout: float | None = None,
    ) -> str:
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

    def read_for(self, seconds: float, stop: Callable[[], bool] | None = None) -> str:
        deadline = time.monotonic() + seconds
        chunks: list[str] = []
        while time.monotonic() < deadline:
            if stop is not None and stop():
                break
            chunk = self.ser.read(512)
            if chunk:
                chunks.append(chunk.decode("utf-8", errors="replace"))
            else:
                time.sleep(0.05)
        return "".join(chunks)

    def capture(self, command: str, seconds: float, stop: Callable[[], bool] | None = None) -> str:
        """Send one CLI line and keep every byte for ``seconds``, including late prints."""
        self.send_command(command, should_wait=False, expect_reply=False)
        text = self.read_for(seconds, stop)
        if self.verbose and text:
            print(text.rstrip())
        return text

    @staticmethod
    def _normalize_buf(buf: str) -> str:
        return buf.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _reply_start(buf: str) -> int | None:
        text = RepeaterSerial._normalize_buf(buf)
        idx = text.find("->")
        if idx >= 0:
            return idx
        idx = text.find("ACL:")
        return idx if idx >= 0 else None

    @classmethod
    def _extract_reply(cls, buf: str) -> str | None:
        text = cls._normalize_buf(buf)
        idx = text.rfind("->")
        if idx >= 0:
            return text[idx + 2 :].strip()
        idx = text.find("ACL:")
        if idx >= 0:
            return text[idx:].strip()
        return None


@dataclass(frozen=True)
class PortProbe:
    ver: str | None = None
    note: str | None = None


def probe_repeater(port: str, *, baud: int, timeout: float, verbose: bool) -> PortProbe:
    from envybot.flash_program import serial_port_busy

    try:
        with RepeaterSerial(port, baud=baud, timeout=timeout, verbose=verbose) as cli:
            reply = cli.cmd("ver")
    except serial.SerialException as exc:
        if serial_port_busy(exc):
            return PortProbe(note="port busy (close other serial sessions)")
        return PortProbe(note=f"open failed: {exc}")
    except (CliError, OSError):
        return PortProbe(note="no repeater CLI on `ver`")
    if not reply or reply_failed(reply):
        return PortProbe(note="no repeater CLI on `ver`")
    return PortProbe(ver=reply)
