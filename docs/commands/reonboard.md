# `envybot reonboard`

USB-serial **re-onboard** for an existing MeshCore row: adopt blank book
fields from the radio, flash EnvyOS bench artifacts when bootloader or
firmware is behind, then push the book (fuzzed site GPS, passwords,
profile, ACL) and stamp sqlite so fleet is not apply-due.

Not first bind — use [`onboard`](onboard.md) for that. Not companion/mesh.

```
./envybot [--book DIR] reonboard --unit me0024 [port] [flags]
```

## What it does

1. Open the DUT USB repeater CLI.
2. **Adopt** blank yaml from USB (serial-only secrets), write `nodes.yaml`
   immediately when anything was adopted:
   - blank `identity_pubkey` / `identity_secret` from `get public.key` /
     `get prv.key`
   - missing `guest_password` key from `get guest.password` (explicit `''`
     in yaml is kept)
   - optional node `trust:` from heard ACL keys that match `keys.yaml`
     people (full override includes book admins such as `ben`)
   - never overwrites a present book pubkey, admin password, or explicit
     guest blank
3. Read `ver` and `get bootloader.ver`, compare to the newest EnvyOS bench
   zip for the board (`board:` → flash target, or `--target`).
4. **Flash** only when behind: bootloader recovery zip (then firmware, app
   wiped) or firmware zip alone when bootloader is current.
5. **Push** profile once (same SET path as onboard / fleet apply), including
   **fuzzed lat/lon** for site-bound units from `public_advert`.
6. Reboot, optional neighbor discover, register yaml + ACL + profile stamp.

Admin password cannot be read from USB; blank yaml gets a new strong password
on push (same as onboard).

## Requirements

- `--unit` is **required**.
- Row must exist, be MeshCore, and not be `decommissioned`.
- USB `get public.key` must match book `identity_pubkey` when the book
  already has one.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--unit KEY` | (required) | Book row to reconcile |
| `--force` | off | Re-SET profile fields even when GET already matches |
| `--no-flash` | off | Skip bench version check and DFU |
| `--no-write` | off | Skip yaml, ACL, and sqlite stamp |
| `--target SLUG` | from `board:` | EnvyOS flash slug when bench check runs |
| `--envyos DIR` | `../envyos` | Bench artifact root |
| `--no-discover` | off | Skip neighbor rounds |

Shares discover, reboot, and serial flags with [`onboard`](onboard.md).
**Do not** pass `--with-flash-bootloader` / `--with-flash-firmware` (onboard
only). Reonboard decides flash from version compare.

## Examples

```bash
./envybot reonboard --unit me0024
./envybot reonboard --unit me0024 --no-flash --force
```

## When not to use this

- **First bind** → [`onboard`](onboard.md).
- USB pubkey registered to a **different** row → fix the book first.

If bench zips are missing, adopt still saves; push runs; exit code is `1`
with the usual EnvyOS build hint.
