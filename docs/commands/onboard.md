# `envybot onboard`

USB-serial onboard for a MeshCore **repeater under test**. Talks repeater
text CLI, not the companion protocol.

`get prv.key` is serial-only. Do not send that over the mesh.

```
./envybot [--book DIR] onboard [port] [flags]
```

## What it does

**First bind** over USB (not the same as [`reonboard`](reonboard.md), which
pushes an existing row onto the radio):

1. Open the DUT USB modem (path or autodetection).
2. Match an existing book row by pubkey, or `--unit`, or allocate the next
   `ME####` from `next_unit` (never reuse). Refuses `decommissioned` rows.
   If `--unit` points at a row whose pubkey ≠ USB, exit with “use reonboard”.
3. **Mint** a new mesh keypair on USB (`set prv.key` + reboot) when this is a
   new allocation or the row has no identity yet. Does **not** copy the chip’s
   factory key into the book. Re-plug the same radio (pubkey already in yaml)
   skips minting. After the first bind, mesh identity stays in the book until
   you change it; restore a row onto USB with [`reonboard`](reonboard.md), not
   another `onboard`.
4. GET then SET. Reuse stored admin/guest password *values* on re-run when
   they are unique and strong. Weak or colliding book/device passwords are
   replaced. Admin cannot be GET, so onboard always writes it (a yaml hit is
   the value, not a skip). A replacement chip left on factory `password`
   otherwise silently drops mesh login.
5. Set USA/Canada radio (`910.525`, BW 62.5, SF7, CR 4/5). Remaining USB
   SETs follow the same **private profile** rules as mesh Deploy
   (`applicable_field_desireds`): dutycycle, path hash, OTA autofetch, advert
   / flood, repeat, hop retry, powersaving, AGC interval, FEM rxgain when the
   board allows it, name, lat/lon, and owner info when the row is site-bound.
   Bench rows (no `site`) get the privacy mask (name `ME####`, GPS `0,0`,
   advert 0). Writes `repeat: false` on the book row when that key is absent.
6. Read public + secret keys. Set host clock. Pull `ota self` (EndF
   `base_hash`) with firmware and bootloader from USB.
7. Reboot so `set radio` takes effect (`get radio` is prefs, not the live
   radio). Waits for the USB node to drop as proof. `--no-reboot` leaves that
   pending.
8. Neighbor discover/fetch (default 3 rounds, longer per-command timeouts
   after reconnect). Prompts to attach an antenna first (Enter to continue,
   `s` to skip). `--no-discover` skips the prompt. No TTY skips discover.
9. **One** `nodes.yaml` write: identity, creds, versions, `board` when
   `--target` maps to a known token, `onboarded_at`, optional
   `onboard_neighbors`, notes stamp. Then serial ACL from `keys.yaml` +
   node `trust` (`get acl` + `setperm`). MeshCore prints `ACL:` on serial and
   does not send a `->` reply for `get acl`. Then stamp sqlite `applies` for
   the private profile so fleet is not apply-due. `public: true` rows stay
   unstamped. `--no-write` skips yaml, ACL, and stamp.

`site` stays null on bench onboard. This unit is bag/bench until you stake it.

Implementation is split across `repeater_serial`, `onboard_usb`, `onboard_book`,
`onboard_serial_apply`, `onboard_usb_session`, and `bench_flash`; `commands/onboard.py`
and `commands/reonboard.py` are thin entrypoints.

## Radio

The **DUT** on USB. Not the BLE companion used by [`fleet`](fleet.md).
Not the USB OTA seeder used by [`seed`](seed.md).

## Flash then onboard

For a blank or old-bootloader unit, flash EnvyOS bench artifacts before the
CLI sequence:

```bash
./envybot onboard --with-flash-bootloader --with-flash-firmware \
  --target xiao-nrf52-repeater-slim
```

Requires a built envyos bench tree (`./envyos build bootloader` and meshcore
for the target). See [`flash`](flash.md). Onboard always probes USB and prompts
for the port first (unless you pass `port` on the CLI). With either flash
flag, the flash-target list comes **after** that, then DFU runs. `--envyos` and
`--flash-wait` pass through to the flash step.

Bootloader-only (`--with-flash-bootloader` without firmware) exits after the
recovery zip because the app is wiped.

After firmware flash, onboard polls the **same physical USB device** (serial /
location id captured at port pick) for up to **25s** until the repeater text CLI
answers `ver`, then continues with `get public.key`. Flash steps also stay pinned
to that device when two identical boards are plugged in.

Bootloader then firmware in one run waits for USB to re-enumerate between the
two DFU steps. If nrfutil fails mid-upload, onboard exits (it no longer continues
into CLI setup). Close fleet/serial monitors and retry; unplug/replug if the port
is stuck in bootloader.

On a TTY, after **each** successful DFU (bootloader and/or firmware), onboard
prompts you to **reboot the board** and press **Enter** before the next step.
Use `--no-flash-pause` to skip (CI or scripted runs).

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `port` | autodetect | USB path, or `usbmodemNNNN`. Omitting it probes every candidate and prompts on a TTY (even when only one port or one repeater answer). |
| `--with-flash-bootloader` | off | Flash OTAFIX recovery zip first |
| `--with-flash-firmware` | off | Flash MeshCore firmware zip first |
| `--target SLUG` | prompt with flash flags | Board slug (`xiao-nrf52-repeater-slim`, `rak4631-repeater-slim`, `sensecap-p1pro-repeater-slim`) |
| `--envyos DIR` | `../envyos` | Bench artifact root |
| `--flash-wait SEC` | 120 | USB wait during flash |
| `--no-flash-pause` | off | Skip Enter prompts after each DFU (on by default on a TTY) |

During the USB onboard sequence, each CLI command goes through
``send_command`` with **wait-for-reply** enabled: on missing ``->`` it retries
with backoff for up to **45s** per command (including after post-reboot USB
reconnect).
| `--unit KEY` | allocate | Bind this book key (create if missing) |
| `--admin-pw` | generated or yaml | Admin password |
| `--guest-pw` | generated or yaml | Guest password (`''` for blank) |
| `--no-write` | | Do not update `nodes.yaml` |
| `--no-reboot` | | Skip reboot (radio change stays pending) |
| `--no-discover` | | Skip neighbor rounds |
| `--discover-rounds N` | `3` | Discover/fetch rounds |
| `--discover-wait SEC` | `12` | Wait between discover rounds (each round uses a 30s CLI timeout and up to 90s wait-for-reply) |
| `--baud` | `115200` | Serial baud |
| `--timeout SEC` | `5` | Per-CLI wait |
| `--verbose` | | Extra serial detail |

`--nodes` is injected from the book.

## Exit status

| Code | Meaning |
|------|---------|
| `0` | Onboard finished |
| `1` | Serial or CLI failure |

## Examples

```bash
./envybot onboard
./envybot onboard /dev/cu.usbmodem1444301
./envybot onboard --unit me0041
./envybot onboard --no-write
./envybot reonboard --unit me0057
```

Wrong pubkey for an existing row → [`reonboard`](reonboard.md), not onboard.
Fails when the USB pubkey is already registered on a different row (fix the
book first).

## Writes

- New or updated `nodes.yaml` row (unless `--no-write`): identity, creds,
  `firmware_version`, `bootloader_version`, `board` (when known), `onboarded_at`,
  `onboard_neighbors` (after discover), `notes` onboard stamp
- `next_unit` bump when a new `ME####` is allocated
- Sqlite `applies` field stamps for the private profile (unless
  `--no-write` or `public: true`)

Does not poll the mesh. After a successful onboard the unit is fleet
deployment ready: privacy mask on the radio, book row, ACL, and apply
stamps. Run [`fleet`](fleet.md) to poll. A sealed unit that needs a new
companion key uses [`trust ben --unit me0041`](trust.md), not this
command.
