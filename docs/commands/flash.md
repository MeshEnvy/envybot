# `envybot flash`

USB programming for EnvyOS bench artifacts on nRF52 OTAFIX boards.
Autodetects USB by VID/PID for the targets below.

Does not write `nodes.yaml`. Use [`onboard`](onboard.md) after firmware is
on the chip.

```
./envybot flash bootloader [--target SLUG] [port] [flags]
./envybot flash firmware [--target SLUG] [port] [flags]
```

Omit `port` to probe USB and pick the device on a TTY first (pass an explicit
path to skip). Omit `--target` on a TTY to pick the board slug next.
Non-interactive runs must pass both `port` and `--target`.

## Artifacts

Discovery reads the sibling EnvyOS repo (`../envyos` or `ENVYOS_ROOT`):

| `--target` | Bootloader zip | Firmware zip / UF2 | App | Bootloader CDC | Volume |
|------------|----------------|------------------|-----|----------------|--------|
| `xiao-nrf52-repeater-slim` | `xiao_nrf52840_ble_bootloader-*.recovery.zip` | `meshcore-xiao-nrf52-repeater-slim-*` | `2886:8044` | `2886:0044` | `XIAO-BOOT` |
| `rak4631-repeater-slim` | `rak4631_bootloader-*.recovery.zip` | `meshcore-rak4631-repeater-slim-*` | `239A:8029` | `239A:0029` or `239A:002A` | `RAK4631` |
| `sensecap-p1pro-repeater-slim` | `sensecap_solar_p1_bootloader-*.recovery.zip` | `meshcore-sensecap-p1pro-repeater-slim-*` | `2886:0059` | `2886:0044` | `SENSECAP` |

Bootloader zips are the newest match under `build/*/bench/adafruit-nrf52-bootloader-*`. Firmware files are the newest match under `build/*/bench/meshcore-*/<slug>/`.

Override paths with `--bootloader`, `--firmware`, or `--uf2`.

Build artifacts in envyos first:

```bash
cd ../envyos
./envyos build bootloader
./envyos build meshcore --target xiao-nrf52-repeater-slim
# SenseCAP P1-Pro solar node:
./envyos build meshcore --target sensecap-p1pro-repeater-slim
```

EnvyBoot **0.9.2-ev2** keeps USB CDC up when the app slot is empty (after
recovery zip). Older bootloaders may sit in BLE DFU until you double-tap
reset; `flash` waits up to `--wait` seconds (default 120).

## USB flow

1. **App running** (the App VID:PID in the table): open the port at 1200 baud
   and drop DTR (Adafruit 1200 touch). Wait for the app port to disappear and
   bootloader CDC or the UF2 volume to appear.
2. **Bootloader CDC already up**: run `adafruit-nrfutil dfu serial` immediately.
   Do not pass `--touch` to nrfutil.
3. **UF2 volume only** (`firmware` + `.uf2`): copy the UF2 onto the volume.
   Bootloader recovery has no drag-drop UF2 path; wait for CDC instead.
4. **Nothing visible**: print double-tap reset help and poll until USB
   appears (ev1 / stock fallback).

`ENVYBOT_NRFUTIL` overrides the flasher. Otherwise `adafruit-nrfutil` on
`PATH`, else PlatformIO `tool-adafruit-nrfutil`.

## Flags

| Flag | Meaning |
|------|---------|
| `--target SLUG` | Required. `xiao-nrf52-repeater-slim`, `rak4631-repeater-slim`, or `sensecap-p1pro-repeater-slim` |
| `port` | Optional USB path; VID/PID wins when omitted |
| `--envyos DIR` | EnvyOS root for bench discovery |
| `--wait SEC` | Bootloader wait (default 120) |
| `--verbose` | Log USB phase |

## Start-to-finish onboard

```bash
./envybot onboard --with-flash-bootloader --with-flash-firmware \
  --target xiao-nrf52-repeater-slim
```

Order: bootloader recovery zip, firmware zip, then the normal USB CLI
onboard. `--with-flash-bootloader` alone stops after the bootloader (app
wiped) and tells you to add `--with-flash-firmware`.

For an **existing** book row, [`reonboard`](reonboard.md) compares
`ver` / `get bootloader.ver` to the newest bench zip and flashes only when
behind (no `--with-flash-*` on reonboard).
