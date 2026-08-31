# `envybot onboard`

USB-serial onboard for a MeshCore **repeater under test**. Talks repeater
text CLI, not the companion protocol.

`get prv.key` is serial-only. Do not send that over the mesh.

```
./envybot [--book DIR] onboard [port] [flags]
```

## What it does

Idempotent on identity, creds, radio, name, and GPS:

1. Open the DUT USB modem (path or autodetection).
2. Match an existing book row by pubkey, or `--unit`, or allocate the next
   `ME####` from `next_unit` (never reuse).
3. GET then SET. Reuse stored admin/guest passwords on re-run when they are
   unique and strong. Weak or colliding book/device passwords are replaced.
4. Set USA/Canada radio (`910.525`, BW 62.5, SF7, CR 4/5), dutycycle 100%
   (`set af 0` on MeshCore before 1.15), `path.hash.mode` 1 (2-byte),
   advert 0 / flood advert 0.
5. Device mask: name `Repeater` and GPS `0,0`. Book `name` is not set to
   `Repeater`. No `public` key (private default).
6. Write the book row (identity, creds). Then stamp fleet ACL from
   `keys.yaml` + book/node `trust` (`get acl` + `setperm`). Skip ACL if
   there are no named people yet. MeshCore prints `ACL:` on serial and
   does not send a `->` reply for `get acl`.
7. Read public + secret keys. Set host clock.
8. Neighbor discover/fetch (default 3 rounds). Prompts to attach an
   antenna first (Enter to continue, `s` to skip). `--no-discover` skips
   the prompt. No TTY skips discover.
9. Always reboot so `set radio` takes effect (`get radio` is prefs, not
   the live radio). Waits for the USB node to drop as proof. `--no-reboot`
   leaves that pending.

`site` stays null. This unit is bag/bench until you stake it.

## Radio

The **DUT** on USB. Not the BLE companion used by [`fleet`](fleet.md).
Not the USB OTA seeder used by `motatool serve`.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `port` | autodetect | USB path, or `usbmodemNNNN` |
| `--unit KEY` | allocate | Bind this book key (create if missing) |
| `--admin-pw` | generated or yaml | Admin password |
| `--guest-pw` | generated or yaml | Guest password (`''` for blank) |
| `--no-write` | | Do not update `nodes.yaml` |
| `--force` | | Re-SET even when matching |
| `--no-reboot` | | Skip reboot (radio change stays pending) |
| `--no-discover` | | Skip neighbor rounds |
| `--discover-rounds N` | `3` | Discover/fetch rounds |
| `--discover-wait SEC` | `12` | Wait between rounds |
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
```

## Writes

- New or updated `nodes.yaml` row (unless `--no-write`)
- `next_unit` bump when a new `ME####` is allocated

Does not poll the mesh. After the unit is deployed, run
[`fleet`](fleet.md). A sealed unit that needs a new companion key uses
[`trust ben --unit me0041`](trust.md), not this command.
