"""USB flash helpers for EnvyOS bench artifacts (nRF52 OTAFIX). Testable; no book I/O."""

from __future__ import annotations

import os
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
}


def resolve_target(slug: str) -> FlashTarget:
    key = slug.strip().lower()
    spec = TARGETS.get(key)
    if spec is None:
        known = ", ".join(sorted(TARGETS))
        raise SystemExit(f"Unknown --target {slug!r}. v1 USB flash supports: {known}")
    return spec


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


def default_nrfutil_runner(cmd: list[str]) -> None:
    print("   $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _port_vid_pid(port: str) -> tuple[int | None, int | None]:
    if list_ports is None:
        return None, None
    for info in list_ports.comports():
        if info.device == port:
            return info.vid, info.pid
    return None, None


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


def classify_usb(spec: FlashTarget, *, hint_port: str | None = None) -> tuple[UsbPhase, str | None]:
    # CDC and the UF2 disk enumerate together. The recovery zip needs the serial port.
    bl_ports = _ports_matching(spec.bl_vid, spec.bl_pids)
    if bl_ports:
        if hint_port and hint_port in bl_ports:
            return UsbPhase.BOOTLOADER, hint_port
        return UsbPhase.BOOTLOADER, bl_ports[0]

    if uf2_mount_path(spec.uf2_volume):
        return UsbPhase.UF2_VOLUME, None

    app_ports = _ports_matching(spec.app_vid, (spec.app_pid,))
    if app_ports:
        if hint_port and hint_port in app_ports:
            return UsbPhase.APP, hint_port
        return UsbPhase.APP, app_ports[0]

    if hint_port and Path(hint_port).exists():
        vid, pid = _port_vid_pid(hint_port)
        if vid == spec.bl_vid and pid in spec.bl_pids:
            return UsbPhase.BOOTLOADER, hint_port
        if vid == spec.app_vid and pid == spec.app_pid:
            return UsbPhase.APP, hint_port
        return UsbPhase.NONE, hint_port

    return UsbPhase.NONE, None


def touch_1200(port: str) -> None:
    if serial is None:  # pragma: no cover
        raise SystemExit("pyserial required for 1200 touch")
    ser = serial.Serial(port, TOUCH_BAUD, timeout=0.5)
    try:
        ser.dtr = True
        time.sleep(0.05)
        ser.dtr = False
    finally:
        ser.close()


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


def run_dfu_serial(
    port: str,
    package: Path,
    *,
    runner: NrfutilRunner | None = None,
) -> None:
    runner = runner or default_nrfutil_runner
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
) -> str:
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
        print(f"   1200 touch on {found}")
        touch_1200(found)
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
            print(f"   1200 touch on {found}")
            touch_1200(found)
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
    bl_port = ensure_bootloader_port(
        spec, port=port, wait_s=wait_s, poll=poll, verbose=verbose
    )
    print(f"   dfu serial on {bl_port}")
    run_dfu_serial(bl_port, package, runner=runner)
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
    phase, found = _usb_phase(spec, port=port, poll=poll)
    if verbose:
        print(f"   USB phase: {phase.value}" + (f" ({found})" if found else ""))

    if phase == UsbPhase.UF2_VOLUME and uf2 is not None:
        copy_firmware_uf2(uf2, spec)
        print("firmware UF2 copy done (device will reboot)")
        return None

    bl_port = ensure_bootloader_port(
        spec, port=port or found, wait_s=wait_s, poll=poll, verbose=verbose
    )
    print(f"   dfu serial on {bl_port}")
    run_dfu_serial(bl_port, package, runner=runner)
    print("firmware flash done")
    return bl_port
