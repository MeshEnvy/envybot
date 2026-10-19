"""USB port pick and reconnect for onboard / flash."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import serial

from envybot.repeater_serial import (
    BAUD,
    CliError,
    PortProbe,
    RepeaterSerial,
    probe_repeater,
    reply_failed,
)
from envybot.radio import serial_port_candidates

REBOOT_SETTLE_S = 4.0
REBOOT_FLUSH_S = 0.4
USB_DROP_TIMEOUT_S = 8.0
RECONNECT_TIMEOUT_S = 25.0


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


def _probe_unusable(probe: PortProbe) -> bool:
    if not probe.note:
        return False
    if "port busy" in probe.note:
        return True
    return probe.note.startswith("open failed")


def _usb_port_choice_label(port: str, probe: PortProbe) -> str:
    if probe.ver:
        return f"{port}  ({probe.ver})"
    if probe.note:
        return f"{port}  ({probe.note})"
    return f"{port}  (no repeater CLI on `ver`)"


def _prompt_usb_port(candidates: list[tuple[str, PortProbe]]) -> str:
    if not candidates:
        raise SystemExit("No USB ports to pick from")
    if not sys.stdin.isatty():
        listed = "\n".join(f"  {_usb_port_choice_label(p, pr)}" for p, pr in candidates)
        raise SystemExit(
            "Pass the USB path: ./envybot onboard /dev/cu.usbmodem…\n" + listed
        )
    print("Pick USB port:", file=sys.stderr)
    for i, (port, probe) in enumerate(candidates, 1):
        print(f"  {i}. {_usb_port_choice_label(port, probe)}", file=sys.stderr)
    while True:
        try:
            raw = input(f"Pick port [1-{len(candidates)}]: ").strip()
        except EOFError:
            raise SystemExit("USB port pick cancelled") from None
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("Enter a number from the list.", file=sys.stderr)
            continue
        if 1 <= idx <= len(candidates):
            port, probe = candidates[idx - 1]
            if _probe_unusable(probe):
                print(
                    f"  {port} is not available — pick a port that opened during probe.",
                    file=sys.stderr,
                )
                continue
            return port
        print(f"Enter 1–{len(candidates)}.", file=sys.stderr)


def pick_port(arg: str | None, *, baud: int, timeout: float, verbose: bool) -> str:
    from envybot.flash_program import ensure_serial_port_available

    if arg:
        port = resolve_port_arg(arg)
        if not port:
            raise SystemExit(f"USB port not found: {arg}")
        return ensure_serial_port_available(port, baud=baud)
    ports = serial_port_candidates()
    if not ports:
        raise SystemExit("No USB modem ports found (cu.usbmodem* / ttyACM* / ttyUSB*).")
    print(f"Probing {len(ports)} USB port(s) for repeater CLI …")
    probed: list[tuple[str, PortProbe]] = []
    for port in ports:
        result = probe_repeater(port, baud=baud, timeout=min(timeout, 2.5), verbose=verbose)
        if result.ver:
            print(f"  {port}: {result.ver}")
        elif verbose:
            print(f"  {port}: {result.note or 'skip'}")
        probed.append((port, result))
        time.sleep(0.08)
    chosen = _prompt_usb_port(probed)
    return ensure_serial_port_available(chosen, baud=baud)


def wait_usb_gone(port: str, *, timeout: float = USB_DROP_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(port).exists():
            return True
        time.sleep(0.15)
    return False


def open_repeater(
    port: str,
    *,
    baud: int,
    timeout: float,
    verbose: bool,
    wait_for_reply: bool = False,
) -> RepeaterSerial:
    cli = RepeaterSerial(port, baud=baud, timeout=timeout, verbose=verbose)
    cli.wait_for_reply = wait_for_reply
    cli.__enter__()
    return cli


def send_reboot(cli: RepeaterSerial) -> bool:
    port = cli.port
    cli.send_command("reboot", should_wait=False, expect_reply=False)
    time.sleep(REBOOT_FLUSH_S)
    try:
        cli.__exit__(None, None, None)
    except (serial.SerialException, OSError):
        pass
    return wait_usb_gone(port)


def prompt_reboot_after_flash(step: str) -> None:
    if not sys.stdin.isatty():
        return
    print(
        f"\n{step} programmed. Reboot the board (unplug/replug or reset), "
        "then press Enter to continue …",
        file=sys.stderr,
    )
    try:
        input()
    except EOFError:
        raise SystemExit("Flash pause cancelled") from None


def reconnect_after_reboot(
    preferred: str,
    *,
    baud: int,
    timeout: float,
    verbose: bool,
    usb_identity: str | None = None,
    wait_for_reply: bool = False,
) -> tuple[RepeaterSerial, str]:
    from envybot.flash_program import find_port_by_usb_identity, port_usb_identity

    print(f"   waiting for USB (up to {RECONNECT_TIMEOUT_S:g}s) …")
    time.sleep(REBOOT_SETTLE_S)
    deadline = time.monotonic() + RECONNECT_TIMEOUT_S
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        ports: list[str] = []
        if usb_identity:
            found = find_port_by_usb_identity(usb_identity)
            if found:
                ports.append(found)
        resolved = resolve_port_arg(preferred)
        if resolved and resolved not in ports:
            ports.append(resolved)
        if not usb_identity:
            for cand in serial_port_candidates():
                if cand not in ports:
                    ports.append(cand)
        else:
            ports = [p for p in ports if port_usb_identity(p) == usb_identity]
        for port in ports:
            try:
                cli = open_repeater(
                    port,
                    baud=baud,
                    timeout=timeout,
                    verbose=verbose,
                    wait_for_reply=wait_for_reply,
                )
                try:
                    ver = cli.send_command("ver", should_wait=wait_for_reply)
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
