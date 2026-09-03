# `envybot seed`

USB-serial OTA seeder. Serves a laptop folder of `.mota` files to a
repeater with `OTA_FOLDER_SERIAL` over mota-seeder framing (`MS`/`ms`).

Motatool still packs, verifies, and builds deltas. Envybot owns the USB
serve loop (COUNT / DESCRIBE / READ relay).

```
./envybot [--book DIR] seed DIR [port] [flags]
```

## What it does

1. Scan `DIR` for valid `.mota` files (non-recursive by default).
2. Print the catalog indices the node will see.
3. Open the USB serial port (path or autodetect).
4. Send `doctor gc` then `set dutycycle 10` (airtime cap while seeding).
5. Send `ota folder on` (unless `--no-enable`).
6. Answer seeder frames until Ctrl-C, then send `ota folder off`.

Storage ops (STAT / BEGIN / WRITE / SREAD / FIN) reply **ERR** in v1.

## Radio

The **USB OTA repeater** (WisMesh tag or similar with folder relay).
Not the BLE companion used by [`fleet`](fleet.md). Not the DUT used by
[`onboard`](onboard.md).

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `DIR` | required | Folder of `.mota` files |
| `port` | autodetect | USB path, or `usbmodemNNNN` |
| `--only GLOB` | all | Include only filenames matching GLOB (repeatable) |
| `--recursive` | off | Scan subdirectories |
| `-v` / `--verbose` | off | Log COUNT / DESCRIBE / READ |
| `--baud` | `115200` | Serial baud |
| `--no-enable` | | Skip `ota folder on` / off |

## Exit status

| Code | Meaning |
|------|---------|
| `0` | Seeder stopped (usually Ctrl-C) |
| `1` | Bad directory, no valid motas, or serial failure |

## Examples

```bash
./envybot seed /path/to/motas
./envybot seed build/otatest /dev/cu.usbmodem1444301
./envybot seed build/otatest --only 'from-0.1.0-to-0.1.1*' -v
```

Use `--only` when a large bench folder would clutter `ota ls` on the DUT.

## See also

- [`onboard`](onboard.md) — USB DUT text CLI
- [`fleet`](fleet.md) — BLE companion poll / Stage / Install
- Ops: `initiatives/envybot-seed.md`
