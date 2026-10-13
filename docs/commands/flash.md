# `envybot flash`

USB programming for EnvyOS bench artifacts on nRF52 OTAFIX boards. v1
autodetects USB by VID/PID for the Seeed XIAO nRF52840 slim repeater target
only.

Does not write `nodes.yaml`. Use [`onboard`](onboard.md) after firmware is
on the chip.

```
./envybot flash bootloader --target SLUG [port] [flags]
./envybot flash firmware --target SLUG [port] [flags]
```

## Artifacts

Discovery reads the sibling EnvyOS repo (`../envyos` or `ENVYOS_ROOT`):

| Subcommand | Default artifact |
|------------|------------------|
| `bootloader` | Newest `xiao_nrf52840_ble_bootloader-*.recovery.zip` under `build/*/bench/adafruit-nrf52-bootloader-*` |
| `firmware` | Newest `meshcore-xiao-nrf52-repeater-slim-*.zip` (and matching `.uf2`) under `build/*/bench/meshcore-*/xiao-nrf52-repeater-slim/` |

Override paths with `--bootloader`, `--firmware`, or `--uf2`.

Build artifacts in envyos first:

```bash
cd ../envyos
./envyos build bootloader
./envyos build meshcore --target xiao-nrf52-repeater-slim
```

EnvyBoot **0.9.2-ev2** keeps USB CDC up when the app slot is empty (after
recovery zip). Older bootloaders may sit in BLE DFU until you double-tap
reset; `flash` waits up to `--wait` seconds (default 120).

## USB flow

1. **App running** (`2886:8044`): open the port at 1200 baud and drop DTR
   (Adafruit 1200 touch). Wait for the app port to disappear and bootloader
   CDC (`2886:0044`) or the `XIAO-BOOT` UF2 volume to appear.
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
| `--target SLUG` | Required. e.g. `xiao-nrf52-repeater-slim` |
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
