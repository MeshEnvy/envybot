# `envybot fleet`

Localhost fleet manager. Serves the map UI, GETs telemetry into
`data/fleet/history.sqlite`, and SETs radio policy.

```
./envybot [--book DIR] fleet [flags]
```

Default: UI at `http://127.0.0.1:8787/` plus poll due plus apply due.

## Stores

| Store | Role |
|-------|------|
| `nodes.yaml` | Desired identity. Operator / onboard / UI write. Poll does not. No GPS. |
| `sites.yaml` | Places (`loc`) and the 1:1 `node:` bind. Fleet writes bind only. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

First run imports leftover `polls.jsonl` (then deletes it) and YAML
`*_pulled_at` blobs, then strips observed keys from `nodes.yaml`.

## Apply

Nodes without `public: true` get the privacy mask: name `Repeater`, lat/lon
`0,0`, adverts off, a unique strong guest password, ACL = book allowlist.
Blank, weak, or colliding guests are rolled and written back to the book.
`public: true` pushes book name + resolved GPS.

Always also SETs `path.hash.mode` (default 1 = 2-byte), `dutycycle`
(default 100), a strong book admin via `password`, and clock if unset
or behind. Password login is skipped when the companion is already on
the book's ACL for that unit. Live clock then comes from the `clock`
CLI. Drop the companion from the ACL to force login if that belief is
wrong.

Apply is due when `profile_id` (`v1:` + hash of the desired SET payload)
does not match the last successful apply stamp, or a private node is
leaking identity. Edit a hashed field in `nodes.yaml` and restart fleet.

Hashed: public/name/gps/adverts, guest + admin (tokens), identity pubkey,
path.hash, dutycycle, ACL allowlist. Not hashed / not pushed here:
identity secret (`roll`), radio preset (onboard), clock.

## Flags

Same companion flags as `cmd` (`--ble`, `--serial`, `--tcp`, `--timeout`,
`--attempts`, …).

| Flag | Meaning |
|------|---------|
| `--web-only` | Browse the book. No radio. |
| `--no-web` | Headless poll/apply |
| `--unit KEY` | One unit (repeatable) |
| `--force` | Re-GET every group; re-SET profile |
| `--live` | Periodic GET groups only |
| `--poll-only` | GET only |
| `--apply-only` | SET only |
| `--all-units` | Include bag/bench (no site `node:` bind) |

## UI

Map pins use **book** position (`sites.yaml` loc for the bound unit), never
device `0,0`. Detail shows book name + site name, a `public` toggle, drift
(`leak` vs `mismatch`), and battery history.
