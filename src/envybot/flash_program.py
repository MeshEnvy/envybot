"""USB flash helpers for EnvyOS bench artifacts (nRF52 OTAFIX). Testable; no book I/O."""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]

NrfutilRunner = Callable[[list[str]], None]

DEFAULT_WAIT_S = 120.0
DFU_POST_SETTLE_S = 2.0
TOUCH_BAUD = 1200
DFU_BAUD = 115200


class UsbPhase(str, Enum):
    APP = "app"
    BOOTLOADER = "bootloader"
    UF2_VOLUME = "uf2"
    NONE = "none"


@dataclass(frozen=True)
class FlashTarget:
    slug: str
    bootloader_glob: str
    firmware_prefix: str
    app_vid: int
    app_pid: int
    bl_vid: int
    bl_pids: tuple[int, ...]
    uf2_volume: str


TARGETS: dict[str, FlashTarget] = {
    "rak4631-repeater-slim": FlashTarget(
        slug="rak4631-repeater-slim",
        bootloader_glob="rak4631_bootloader-*.recovery.zip",
        firmware_prefix="meshcore-rak4631-repeater-slim-",
        app_vid=0x239A,
        app_pid=0x8029,
        bl_vid=0x239A,
        # 0x0029 is UF2+CDC. A 1200 touch uses serial-only DFU, which is 0x002A.
        bl_pids=(0x0029, 0x002A),
        uf2_volume="RAK4631",
    ),
    "xiao-nrf52-repeater-slim": FlashTarget(
        slug="xiao-nrf52-repeater-slim",
        bootloader_glob="xiao_nrf52840_ble_bootloader-*.recovery.zip",
        firmware_prefix="meshcore-xiao-nrf52-repeater-slim-",
        app_vid=0x2886,
        app_pid=0x8044,
        bl_vid=0x2886,
        bl_pids=(0x0044,),
        uf2_volume="XIAO-BOOT",
    ),
    "sensecap-p1pro-repeater-slim": FlashTarget(
        slug="sensecap-p1pro-repeater-slim",
        bootloader_glob="sensecap_solar_p1_bootloader-*.recovery.zip",
        firmware_prefix="meshcore-sensecap-p1pro-repeater-slim-",
        app_vid=0x2886,
        app_pid=0x0059,
        bl_vid=0x2886,
        bl_pids=(0x0044,),
        uf2_volume="SENSECAP",
    ),
}

TARGET_ALIASES: dict[str, str] = {
    "sensecap-solar-p1-repeater-slim": "sensecap-p1pro-repeater-slim",
}


def flash_target_slugs() -> list[str]:
    return sorted(TARGETS)


def resolve_target(slug: str) -> FlashTarget:
    key = slug.strip().lower()
    key = TARGET_ALIASES.get(key, key)
    spec = TARGETS.get(key)
    if spec is None:
        known = ", ".join(flash_target_slugs())
        raise SystemExit(f"Unknown --target {slug!r}. v1 USB flash supports: {known}")
    return spec


def pick_flash_target(slug: str | None) -> FlashTarget:
    """Resolve --target, or list slugs and prompt on a TTY when omitted."""
    if slug and slug.strip():
        return resolve_target(slug)
    slugs = flash_target_slugs()
    if not sys.stdin.isatty():
        listed = "\n".join(f"  {s}" for s in slugs)
        raise SystemExit(f"Pass --target SLUG. v1 USB flash supports:\n{listed}")
    print("Pick flash target:", file=sys.stderr)
    for i, name in enumerate(slugs, 1):
        print(f"  {i}. {name}", file=sys.stderr)
    while True:
        try:
            raw = input(f"Pick target [1-{len(slugs)}]: ").strip()
        except EOFError:
            raise SystemExit("Target pick cancelled") from None
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("Enter a number from the list.", file=sys.stderr)
            continue
        if 1 <= idx <= len(slugs):
            return resolve_target(slugs[idx - 1])
        print(f"Enter 1–{len(slugs)}.", file=sys.stderr)


def default_envyos_root() -> Path:
    env = os.environ.get("ENVYOS_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    sibling = here.parents[3] / "envyos"
    if sibling.is_dir():
        return sibling.resolve()
    return (Path.cwd().parent / "envyos").resolve()


def _newest_file(root: Path, pattern: str) -> Path | None:
    hits = [p for p in root.glob(pattern) if p.is_file()]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def _newest_bench_dir(envyos: Path, bench_glob: str) -> Path | None:
    build = envyos / "build"
    if not build.is_dir():
        return None
    dirs = [p for p in build.glob(f"*/bench/{bench_glob}") if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def resolve_bootloader_zip(
    envyos: Path,
    spec: FlashTarget,
    *,
    override: Path | None = None,
) -> Path:
    if override is not None:
        path = override.expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Bootloader package not found: {path}")
        return path
    bench = _newest_bench_dir(envyos, "adafruit-nrf52-bootloader-*")
    if bench is None:
        raise SystemExit(
            f"No adafruit-nrf52-bootloader bench tree under {envyos / 'build'}. "
            "Run ./envyos build bootloader in envyos."
        )
    found = _newest_file(bench, spec.bootloader_glob)
    if found is None:
        raise SystemExit(
            f"No {spec.bootloader_glob} under {bench}. Rebuild bootloader for this board."
        )
    return found


def resolve_firmware_artifacts(
    envyos: Path,
    spec: FlashTarget,
    *,
    zip_override: Path | None = None,
    uf2_override: Path | None = None,
) -> tuple[Path, Path | None]:
    if zip_override is not None:
        zpath = zip_override.expanduser().resolve()
        if not zpath.is_file():
            raise SystemExit(f"Firmware package not found: {zpath}")
        uf2: Path | None = None
        if uf2_override is not None:
            up = uf2_override.expanduser().resolve()
            if up.is_file():
                uf2 = up
        return zpath, uf2

    bench = _newest_bench_dir(envyos, "meshcore-*")
    if bench is None:
        raise SystemExit(
            f"No meshcore bench tree under {envyos / 'build'}. "
            "Run ./envyos build meshcore in envyos."
        )
    slug_dir = bench / spec.slug
    if not slug_dir.is_dir():
        raise SystemExit(f"No firmware folder {slug_dir}. Build target {spec.slug} first.")
    zpath = _newest_file(slug_dir, f"{spec.firmware_prefix}*.zip")
    if zpath is None:
        raise SystemExit(f"No {spec.firmware_prefix}*.zip under {slug_dir}.")
    uf2 = _newest_file(slug_dir, f"{spec.firmware_prefix}*.uf2")
    if uf2_override is not None:
        up = uf2_override.expanduser().resolve()
        if up.is_file():
            uf2 = up
    return zpath, uf2


def resolve_nrfutil() -> list[str]:
    env = os.environ.get("ENVYBOT_NRFUTIL", "").strip()
    if env:
        return [env]
    if shutil.which("adafruit-nrfutil"):
        return ["adafruit-nrfutil"]
    py = Path.home() / ".platformio/packages/tool-adafruit-nrfutil/adafruit-nrfutil.py"
    if py.is_file():
        return [sys.executable, str(py)]
    raise SystemExit(
        "adafruit-nrfutil not found. Install PlatformIO tool-adafruit-nrfutil or set ENVYBOT_NRFUTIL."
    )


def _nrfutil_unbuffered_cmd(cmd: list[str]) -> list[str]:
    if len(cmd) >= 2 and str(cmd[1]).endswith(".py"):
        return [cmd[0], "-u", *cmd[1:]]
    return cmd


def _stream_nrfutil_pipe(cmd: list[str]) -> tuple[int, str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    chunks: list[bytes] = []
    assert proc.stdout is not None
    while True:
        block = proc.stdout.read(256)
        if not block:
            break
        chunks.append(block)
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
    return proc.wait(), b"".join(chunks).decode("utf-8", errors="replace")


def _stream_nrfutil_pty(cmd: list[str]) -> tuple[int, str]:
    """Run nrfutil on a pseudo-TTY so click progress `#` chars flush (pipe = block chunks)."""
    master, slave = pty.openpty()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=slave,
            stderr=slave,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    finally:
        os.close(slave)
    chunks: list[bytes] = []
    try:
        while True:
            if proc.poll() is not None:
                while True:
                    try:
                        block = os.read(master, 4096)
                    except OSError:
                        break
                    if not block:
                        break
                    chunks.append(block)
                    sys.stdout.buffer.write(block)
                    sys.stdout.buffer.flush()
                break
            ready, _, _ = select.select([master], [], [], 0.05)
            if master not in ready:
                continue
            try:
                block = os.read(master, 4096)
            except OSError:
                break
            if not block:
                break
            chunks.append(block)
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
    finally:
        os.close(master)
    return proc.wait(), b"".join(chunks).decode("utf-8", errors="replace")


def _stream_nrfutil_output(cmd: list[str]) -> tuple[int, str]:
    if sys.platform == "win32":
        return _stream_nrfutil_pipe(cmd)
    return _stream_nrfutil_pty(cmd)


def default_nrfutil_runner(cmd: list[str]) -> None:
    print("   $", " ".join(cmd))
    cmd = _nrfutil_unbuffered_cmd(cmd)
    # adafruit-nrfutil often exits 0 even when DFU fails; parse banner lines after stream ends.
    code, combined = _stream_nrfutil_output(cmd)
    if code != 0 or "Failed to upgrade target" in combined:
        raise SystemExit(
            "DFU failed (no ack from the bootloader — often instant if the port is "
            "busy or the chip is not in serial DFU). Close fleet/seed/monitor, "
            "unplug/replug, and retry. Skip --with-flash-bootloader if EnvyBoot is "
            "already flashed and use --with-flash-firmware only."
        )
    if "Device programmed." not in combined:
        raise SystemExit("DFU finished without a success banner from nrfutil.")


def _port_vid_pid(port: str) -> tuple[int | None, int | None]:
    if list_ports is None:
        return None, None
    for info in list_ports.comports():
        if info.device == port:
            return info.vid, info.pid
    return None, None


def port_usb_identity(port: str) -> str | None:
    """Stable id for the same physical USB device across cu.* re-enumeration."""
    if list_ports is None:
        return None
    for info in list_ports.comports():
        if info.device != port:
            continue
        for attr in ("serial_number", "location", "hwid"):
            val = getattr(info, attr, None)
            if val and str(val).strip():
                return str(val).strip()
    return None


def find_port_by_usb_identity(identity: str) -> str | None:
    if list_ports is None or not identity:
        return None
    for info in list_ports.comports():
        if not info.device:
            continue
        if port_usb_identity(info.device) == identity:
            return info.device
    return None


def _ports_for_identity(ports: list[str], usb_identity: str | None) -> list[str]:
    if not usb_identity:
        return ports
    matched = [p for p in ports if port_usb_identity(p) == usb_identity]
    # Do not fall back to other boards when the operator pinned a physical device.
    return matched


def _choose_one_port(
    ports: list[str],
    *,
    hint_port: str | None,
    kind: str,
) -> str:
    if not ports:
        raise SystemExit(f"No {kind} USB port for this target")
    if hint_port and hint_port in ports:
        return hint_port
    if len(ports) == 1:
        return ports[0]
    listed = "\n".join(f"  {p}" for p in ports)
    raise SystemExit(
        f"Multiple {kind} USB devices for this target — pass the port:\n{listed}"
    )


def _ports_matching(vid: int, pids: tuple[int, ...]) -> list[str]:
    if list_ports is None:
        return []
    want = set(pids)
    out: list[str] = []
    for info in list_ports.comports():
        if info.vid == vid and info.pid in want and info.device:
            out.append(info.device)
    return sorted(out)


def _cdc_label(spec: FlashTarget) -> str:
    pids = ",".join(f"{spec.bl_vid:04x}:{pid:04x}" for pid in spec.bl_pids)
    return pids


def uf2_mount_path(volume: str) -> Path | None:
    mount = Path("/Volumes") / volume
    if mount.is_dir():
        return mount
    return None


def classify_usb(
    spec: FlashTarget,
    *,
    hint_port: str | None = None,
    usb_identity: str | None = None,
) -> tuple[UsbPhase, str | None]:
    if hint_port and not usb_identity:
        usb_identity = port_usb_identity(hint_port)
    # CDC and the UF2 disk enumerate together. The recovery zip needs the serial port.
    bl_ports = _ports_for_identity(
        _ports_matching(spec.bl_vid, spec.bl_pids), usb_identity
    )
    if bl_ports:
        return UsbPhase.BOOTLOADER, _choose_one_port(
            bl_ports, hint_port=hint_port, kind="bootloader"
        )

    if uf2_mount_path(spec.uf2_volume):
        return UsbPhase.UF2_VOLUME, None

    app_ports = _ports_for_identity(
        _ports_matching(spec.app_vid, (spec.app_pid,)), usb_identity
    )
    if app_ports:
        return UsbPhase.APP, _choose_one_port(app_ports, hint_port=hint_port, kind="app")

    if hint_port and Path(hint_port).exists():
        vid, pid = _port_vid_pid(hint_port)
        if vid == spec.bl_vid and pid in spec.bl_pids:
            return UsbPhase.BOOTLOADER, hint_port
        if vid == spec.app_vid and pid == spec.app_pid:
            return UsbPhase.APP, hint_port
        return UsbPhase.NONE, hint_port

    return UsbPhase.NONE, None


def flash_usb_poll(
    spec: FlashTarget,
    port: str | None,
) -> Callable[[], tuple[UsbPhase, str | None]]:
    """Pin flash autodetect to the operator-chosen port / USB identity."""
    identity = port_usb_identity(port) if port else None

    def poll() -> tuple[UsbPhase, str | None]:
        hint = port if port and Path(port).exists() else None
        if not hint and identity:
            hint = find_port_by_usb_identity(identity)
        return classify_usb(spec, hint_port=hint, usb_identity=identity)

    return poll


def serial_port_busy(exc: BaseException) -> bool:
    if serial is not None and isinstance(exc, serial.SerialException):
        if getattr(exc, "errno", None) == 16:
            return True
    text = str(exc).lower()
    return "busy" in text or "resource busy" in text


def serial_port_transient(exc: BaseException) -> bool:
    if serial_port_busy(exc):
        return True
    if isinstance(exc, OSError) and exc.errno == 2:
        return True
    if serial is not None and isinstance(exc, serial.SerialException):
        if getattr(exc, "errno", None) == 2:
            return True
    return False


def resolve_serial_port(port: str, usb_identity: str | None = None) -> str:
    if port and Path(port).exists():
        return port
    if usb_identity:
        found = find_port_by_usb_identity(usb_identity)
        if found:
            return found
    raise SystemExit(
        f"USB device went away ({port!r}). Re-plug the board and restart, "
        "or pass the new /dev/cu path on the command line."
    )


def ensure_serial_port_available(
    port: str,
    *,
    usb_identity: str | None = None,
    baud: int = DFU_BAUD,
    timeout: float = 15.0,
) -> str:
    """Open and close the port; retry busy / re-enumeration gaps. Returns current path."""
    if serial is None:  # pragma: no cover
        raise SystemExit("pyserial required for USB serial")
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        path = resolve_serial_port(port, usb_identity)
        try:
            ser = serial.Serial(path, baud, timeout=0.5)
            ser.close()
            time.sleep(0.08)
            return path
        except (serial.SerialException, OSError) as exc:
            last = exc
            if serial_port_transient(exc):
                time.sleep(0.35)
                continue
            raise SystemExit(f"Cannot open USB port {path}: {exc}") from exc
    raise SystemExit(
        f"USB port {port!r} is not available after {timeout:g}s.\n"
        "Close other serial sessions (fleet, screen, PlatformIO monitor) and retry."
    ) from last


def prepare_serial_port(
    port: str,
    usb_identity: str | None,
    *,
    baud: int = DFU_BAUD,
    timeout: float = 15.0,
) -> str:
    """Re-resolve cu.* after menus and confirm the device opens."""
    return ensure_serial_port_available(
        port, usb_identity=usb_identity, baud=baud, timeout=timeout
    )


def touch_1200(port: str, *, usb_identity: str | None = None) -> None:
    if serial is None:  # pragma: no cover
        raise SystemExit("pyserial required for 1200 touch")
    deadline = time.monotonic() + 15.0
    last: BaseException | None = None
    while time.monotonic() < deadline:
        path = resolve_serial_port(port, usb_identity)
        try:
            ser = serial.Serial(path, TOUCH_BAUD, timeout=0.5)
            try:
                ser.dtr = True
                time.sleep(0.05)
                ser.dtr = False
            finally:
                ser.close()
            return
        except (serial.SerialException, OSError) as exc:
            last = exc
            if serial_port_transient(exc):
                time.sleep(0.35)
                continue
            raise SystemExit(f"1200 touch failed on {path}: {exc}") from exc
    raise SystemExit(
        f"1200 touch failed: USB port {port!r} not available after 15s."
    ) from last


def wait_port_gone(port: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(port).exists():
            return True
        time.sleep(0.15)
    return False


def wait_for_bootloader_usb(
    spec: FlashTarget,
    *,
    after_port: str | None,
    timeout: float,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
) -> tuple[UsbPhase, str | None]:
    poll_fn = poll or (lambda: classify_usb(spec))
    deadline = time.monotonic() + timeout
    if after_port:
        wait_port_gone(after_port, timeout=min(15.0, timeout))
    while time.monotonic() < deadline:
        phase, port = poll_fn()
        if phase in (UsbPhase.BOOTLOADER, UsbPhase.UF2_VOLUME):
            return phase, port
        time.sleep(0.25)
    return UsbPhase.NONE, None


def wait_for_any_usb(
    spec: FlashTarget,
    *,
    timeout: float,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
) -> tuple[UsbPhase, str | None]:
    poll_fn = poll or (lambda: classify_usb(spec))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phase, port = poll_fn()
        if phase != UsbPhase.NONE:
            return phase, port
        time.sleep(0.25)
    return UsbPhase.NONE, None


def settle_after_dfu(
    spec: FlashTarget,
    port: str,
    *,
    usb_identity: str | None = None,
    wait_s: float = DEFAULT_WAIT_S,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
    verbose: bool = False,
) -> str:
    """After nrfutil finishes the device resets; wait for bootloader CDC again."""
    print(f"   waiting for USB after DFU ({DFU_POST_SETTLE_S:g}s settle) …")
    if wait_port_gone(port, timeout=min(8.0, wait_s)):
        time.sleep(DFU_POST_SETTLE_S)
    else:
        print(f"   note: {port} still present (DFU may have failed or USB did not reset)")
        time.sleep(DFU_POST_SETTLE_S)
    poll = poll or flash_usb_poll(spec, port)
    bl_port = ensure_bootloader_port(
        spec,
        port=port,
        wait_s=wait_s,
        poll=poll,
        verbose=verbose,
        usb_identity=usb_identity,
    )
    return ensure_serial_port_available(bl_port, usb_identity=usb_identity)


def run_dfu_serial(
    port: str,
    package: Path,
    *,
    runner: NrfutilRunner | None = None,
    usb_identity: str | None = None,
) -> None:
    runner = runner or default_nrfutil_runner
    # Do not open the port here — nrfutil needs an exclusive handle for the DFU handshake.
    port = resolve_serial_port(port, usb_identity)
    if not Path(port).exists():
        raise SystemExit(f"USB port {port!r} is not present.")
    cmd = [
        *resolve_nrfutil(),
        "dfu",
        "serial",
        "--singlebank",
        "-b",
        str(DFU_BAUD),
        "-p",
        port,
        "--package",
        str(package),
    ]
    runner(cmd)


def copy_firmware_uf2(uf2: Path, spec: FlashTarget) -> None:
    mount = uf2_mount_path(spec.uf2_volume)
    if mount is None:
        raise SystemExit(
            f"UF2 volume {spec.uf2_volume!r} not mounted. Wait for bootloader USB or pass a CDC port."
        )
    dest = mount / uf2.name
    print(f"   copy {uf2.name} → {mount}")
    shutil.copy2(uf2, dest)


def _usb_phase(
    spec: FlashTarget,
    *,
    port: str | None,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None,
) -> tuple[UsbPhase, str | None]:
    if poll is not None:
        return poll()
    return classify_usb(spec, hint_port=port)


def ensure_bootloader_port(
    spec: FlashTarget,
    *,
    port: str | None,
    wait_s: float,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
    verbose: bool = False,
    usb_identity: str | None = None,
) -> str:
    stick = usb_identity or (port_usb_identity(port) if port else None)

    def touch(port_path: str) -> None:
        print(f"   1200 touch on {port_path}")
        touch_1200(port_path, usb_identity=stick)

    phase, found = _usb_phase(spec, port=port, poll=poll)
    if verbose:
        print(f"   USB phase: {phase.value}" + (f" ({found})" if found else ""))

    if phase == UsbPhase.BOOTLOADER and found:
        return found

    if phase == UsbPhase.UF2_VOLUME:
        print(
            f"   {spec.uf2_volume} is mounted. Waiting up to {wait_s:g}s for bootloader CDC "
            f"({_cdc_label(spec)})."
        )
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            phase, found = _usb_phase(spec, port=port, poll=poll)
            if phase == UsbPhase.BOOTLOADER and found:
                return found
        raise SystemExit(
            f"Timed out after {wait_s:g}s waiting for bootloader USB "
            f"(CDC {_cdc_label(spec)}). {spec.uf2_volume} is still the only USB device."
        )

    if phase == UsbPhase.APP and found:
        touch(found)
        phase, found = wait_for_bootloader_usb(
            spec, after_port=found, timeout=wait_s, poll=poll
        )
        if phase == UsbPhase.BOOTLOADER and found:
            return found
        if phase == UsbPhase.UF2_VOLUME:
            raise SystemExit(
                "Bootloader UF2 volume appeared; use flash firmware with .uf2 or wait for CDC."
            )

    if phase == UsbPhase.NONE:
        print(
            "No USB port yet. A red blink about 3-4 times a second means the "
            "bootloader is running and USB has not mounted. Unplug the board "
            "for a few seconds and plug it back in. Double-tap reset only if "
            "that blink is an older bootloader stuck in BLE DFU."
        )
        phase, found = wait_for_any_usb(spec, timeout=wait_s, poll=poll)
        if phase == UsbPhase.APP and found:
            touch(found)
            phase, found = wait_for_bootloader_usb(
                spec, after_port=found, timeout=wait_s, poll=poll
            )
        elif phase == UsbPhase.UF2_VOLUME:
            raise SystemExit(
                "UF2 volume only; bootloader flash needs CDC. Double-tap reset or power-cycle."
            )

    if phase == UsbPhase.BOOTLOADER and found:
        return found

    raise SystemExit(
        f"Timed out after {wait_s:g}s waiting for bootloader USB (CDC {_cdc_label(spec)})."
    )


def flash_bootloader_package(
    package: Path,
    spec: FlashTarget,
    *,
    port: str | None = None,
    wait_s: float = DEFAULT_WAIT_S,
    runner: NrfutilRunner | None = None,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
    verbose: bool = False,
) -> str:
    print(f"bootloader package: {package}")
    poll = poll or (flash_usb_poll(spec, port) if port else None)
    stick = port_usb_identity(port) if port else None
    bl_port = ensure_bootloader_port(
        spec,
        port=port,
        wait_s=wait_s,
        poll=poll,
        verbose=verbose,
        usb_identity=stick,
    )
    print(f"   dfu serial on {bl_port}")
    run_dfu_serial(bl_port, package, runner=runner, usb_identity=stick)
    print("bootloader flash done")
    return bl_port


def flash_firmware_package(
    package: Path,
    spec: FlashTarget,
    *,
    uf2: Path | None = None,
    port: str | None = None,
    wait_s: float = DEFAULT_WAIT_S,
    runner: NrfutilRunner | None = None,
    poll: Callable[[], tuple[UsbPhase, str | None]] | None = None,
    verbose: bool = False,
) -> str | None:
    print(f"firmware package: {package}")
    poll = poll or (flash_usb_poll(spec, port) if port else None)
    phase, found = _usb_phase(spec, port=port, poll=poll)
    if verbose:
        print(f"   USB phase: {phase.value}" + (f" ({found})" if found else ""))

    if phase == UsbPhase.UF2_VOLUME and uf2 is not None:
        copy_firmware_uf2(uf2, spec)
        print("firmware UF2 copy done (device will reboot)")
        return None

    hint = port or found
    stick = port_usb_identity(hint) if hint else None
    bl_port = ensure_bootloader_port(
        spec,
        port=hint,
        wait_s=wait_s,
        poll=poll,
        verbose=verbose,
        usb_identity=stick,
    )
    print(f"   dfu serial on {bl_port}")
    run_dfu_serial(bl_port, package, runner=runner, usb_identity=stick)
    print("firmware flash done")
    return bl_port
