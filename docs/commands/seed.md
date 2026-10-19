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
3. Open the USB serial port (path or autodetect). Prints `[host] USB /dev/cu.… (115200 baud)`.
4. Send `doctor gc`, `set dutycycle` (default **10**; `--dutycycle` to override), `log tail on`,
   then poll tag **name**, **public.key**, and **radio** settings on the same session (no second open).
5. Send `ota folder on` (unless `--no-enable`), then `ota announce` on the tag
   (seed holds the USB console so you cannot type it yourself). Re-announces
   every `--announce-interval` seconds (default 60) so bench DUTs learn the source.
6. Answer seeder frames until Ctrl-C, then send `ota folder off`.

**Visibility:** A DUT’s `ota ls` only floods LoRa QUERY/HAVE packets. The host does
not see those unless the tag prints them (`log tail on`, or firmware built with
`OTA_DEBUG=1`). You **do** see `[host] usb-mota READ …` when a peer actually
downloads blocks from the laptop folder. Every `--status-interval` seconds (default
45) the host polls `ota folder` and `ota stats` on the tag so you can confirm
`serving` / digest / folder attach without a second serial adapter.

Storage ops (STAT / BEGIN / WRITE / SREAD / FIN) reply **ERR** in v1.

## Radio

The **USB companion** seeder (e.g. **`RAK_WisMesh_Tag_bot_usb`**, EnvyBot name
`EnvyBot`). Same port as the MeshCore app: seed uses **`CMD_RUN_CLI_COMMAND`**
(`run_cli_command` in the Python meshcore lib) for `ota folder on` / `ota announce`,
then **raw `MS`/`ms` mota-seeder** on the same serial while folder relay is active
(firmware must include companion OTA folder support — flash EnvyOS meshcore with
`ENABLE_OTA` + `OTA_FOLDER_SERIAL`).

Legacy **repeater text console** (`simple_repeater`) still works if the port does
not speak the companion handshake. The DUT for `ota ls` / install is a separate
radio (often a field repeater), not the companion used by [`onboard`](onboard.md).

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `DIR` | required | Folder of `.mota` files |
| `port` | autodetect | USB path, or `usbmodemNNNN` |
| `--only GLOB` | all | Include only filenames matching GLOB (repeatable) |
| `--recursive` | off | Scan subdirectories |
| `-v` / `--verbose` | off | Extra hex detail on usb-mota lines |
| `--baud` | `115200` | Serial baud |
| `--no-enable` | | Skip `ota folder on` / off |
| `--announce-interval SEC` | `60` | Mesh `ota announce` cadence while seeding (`0` = once after folder on) |
| `--status-interval SEC` | `45` | USB poll of tag `ota folder` / `ota stats` (`0` = off) |
| `--dutycycle PCT` | `10` | `set dutycycle` on the tag at seed start (`1`–`100`) |

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
