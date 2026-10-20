# `envybot reonboard`

USB-serial **re-onboard**: the book row is source of truth. Pushes
`identity_secret`, passwords, radio, and fleet-profile fields onto the radio
under test. Use after misconfig, reflash, or bench rework when the unit must
keep the same mesh identity (`ME####` / pubkey) as yaml.

Same transport and post-steps as [`onboard`](onboard.md) (reboot, discover,
`nodes.yaml`, ACL, profile stamp). Does not mint a new keypair.

```
./envybot [--book DIR] reonboard --unit me0057 [port] [flags]
```

## Requirements

- `--unit` is **required**.
- Row must have `identity_pubkey` and `identity_secret` in `nodes.yaml`.
- If USB pubkey already matches the book, identity restore is skipped; profile
  SETs still run.

## Flags

Shares discover and write flags with [`onboard`](onboard.md). Flash flags
exist for parity but are unusual here (reflash first, then reonboard).

| Flag | Default | Meaning |
|------|---------|---------|
| `--unit KEY` | (required) | Book row to restore |
| `--force` | off | Re-SET profile fields even when GET already matches |
| `--no-write` | off | Skip yaml, ACL, and sqlite stamp |

## Examples

```bash
./envybot reonboard --unit me0057
./envybot reonboard --unit me0057 --force --verbose
```

## When not to use this

- **First bind** of a blank or newly flashed board → [`onboard`](onboard.md)
  (mints a new fleet identity unless USB pubkey already matches the row).
- Pubkey on USB registered to a **different** row → fix the book before
  reonboard.
