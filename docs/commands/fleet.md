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
| `nodes.yaml` | Desired identity. Operator / onboard / UI write. Poll does not. |
| `sites.yaml` | Places. Fleet never writes this. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

First run imports leftover `polls.jsonl` (then deletes it) and YAML
`*_pulled_at` blobs, then strips observed keys from `nodes.yaml`.

## Apply

Nodes without `public: true` get the privacy mask: name `Repeater`, lat/lon
`0,0`, adverts off, guest password set, ACL = book allowlist.
`public: true` pushes book name + resolved GPS.

Always also SETs `path.hash.mode=1`, `dutycycle=100`, and clock if unset
or behind.

Apply is due when the radio does not match that profile, not on a 24h
clock. First radio session after this ships masks every reachable private
node.

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
| `--all-units` | Include bag/bench (`site` null) |

## UI

Map pins use **book** position (node override or site loc), never device
`0,0`. Detail shows book name + site name, a `public` toggle, drift
(`leak` vs `mismatch`), and battery history.
