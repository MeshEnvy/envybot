# `envybot monitor`

Poll deployed MeshCore repeaters over LoRa. Write last-known firmware and mesh
state into the book (`nodes.yaml`). Append a snapshot to
`data/fleet/polls.jsonl`.

Uses a **companion** radio (BLE first). This is not USB repeater text CLI.
That is [`onboard`](onboard.md).

```
./envybot [--book DIR] monitor [flags]
```

## What it does

1. Resolve the book (`--book` / `ENVYBOT_HOME` / cwd with `nodes.yaml`).
2. Discover and connect a companion (`--transport auto`: BLE NUS, then serial
   appstart).
3. For each eligible unit: admin login (always), then due queries.
4. Inventory groups (fw, bootloader, name, lat, lon, advert, flood advert,
   path hash, dutycycle) run once unless `--force` or `--group`.
5. Periodic groups (status, telemetry, neighbors, acl) re-run when older than
   `--min-interval` (default 24h).
6. Radio policy: SET `path.hash.mode 1`, `dutycycle 100`, and book `lat`/`lon`.
   Stamp only on OK. Position is never read from the radio.
7. Clock: if live login RTC is unset or behind the host, SET
   `time <host epoch>`. Does not run `clock sync`.

`nodes.yaml` is the last-known SoT for pulled state. Cite `*_pulled_at`.
GPS is book-canonical (`lat`/`lon`, else the node's `site` loc). Monitor
pushes those coords. Do not treat reachability as unknown when a successful
stamp exists.

Retries unreachable units until every target succeeds, or you hit Ctrl+C.

By default, monitor also serves a **local web UI** at `http://127.0.0.1:8787/`
with a live map and unit list (SSE updates as polls land). The UI stays up
after polling until Ctrl+C. Secrets never leave the book. Use `--no-web` for
CLI-only poll-and-exit.

## Web UI flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--no-web` | off | Poll and exit (no local UI) |
| `--web-only` | off | Serve book UI without polling radios |
| `--bind HOST` | `127.0.0.1` | Web bind address |
| `--port N` | `8787` | Web port |
| `--open` | off | Open browser on start |

`--web-only` watches `nodes.yaml` and refreshes the map when the file changes.

## Eligibility

Skips decommissioned, retired, missing pubkey, missing admin password, and
placeholder passwords. Default is deployed only (`site` set). Use
`--all-units` to include bag/bench. `--unit` filters by book key (does not
imply `--force`).

Meshtastic rows (no admin password) are not polled.

## Companion flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--transport auto\|ble\|serial\|tcp` | `auto` | Discovery order |
| `--ble [ADDRESS]` | scan | BLE companion (optional MAC) |
| `--serial PORT` | | Force USB companion (must pass appstart) |
| `--tcp HOST:PORT` | | Companion TCP |
| `--scan-timeout SEC` | `4` | BLE scan duration |
| `--baud` | `115200` | Serial baud |
| `--probe` | | List candidates and handshake. Do not poll. |

## Poll flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--timeout SEC` | `30` | Max wait per command |
| `--login-timeout SEC` | `45` | Max wait per login attempt |
| `--attempts N` | `10` | Retries per send that expects a reply (`0` = unlimited) |
| `--min-interval SEC` | `86400` | Periodic group freshness |
| `--force` | | Every group on every matching unit |
| `--live` | | Periodic groups only (plus incomplete inventory) |
| `--group NAME` | | Force one query (repeatable). `lat`/`lon` SET book coords. Names: `firmware`, `bootloader`, `name`, `lat`, `lon`, `advert`, `flood_advert`, `path_hash`, `dutycycle`, `acl`, `status`, `telemetry`, `neighbors` |
| `--unit KEY` | | Only this book key (repeatable) |
| `--skip KEY` | | Exclude this key (repeatable) |
| `--all-units` | | Include `site: null` |
| `--once` | | Single pass. No retry rounds |
| `--retry-delay SEC` | `0` | Pause after a failed unit |
| `--round-delay SEC` | `0` | Pause between retry rounds |
| `--max-rounds N` | `0` | Stop after N rounds (`0` = until done) |
| `--max-attempts N` | `0` | Give up on a unit after N tries |
| `--dry-run` | | Poll. Do not write `nodes.yaml` |
| `-q` / `--quiet` | | Less per-step progress |
| `-v` / `--verbose` | | Protocol detail |

`--nodes` and `--log-file` are injected from the book. Override only if you
need a different file.

## Exit status

| Code | Meaning |
|------|---------|
| `0` | All due units succeeded (or all already fresh) |
| `1` | No matching pollable units, or probe-only listing |
| `2` | Some units still pending |
| `130` | Interrupted. Keeps successful writes |

## Examples

```bash
./envybot monitor
./envybot monitor --web-only
./envybot monitor --no-web
./envybot monitor --probe
./envybot monitor --transport ble
./envybot monitor --unit me0016 --force
./envybot monitor --unit me0039 --group firmware
./envybot monitor --live
./envybot monitor --skip me0001 --skip me0006
```

## Writes

- `nodes.yaml` last-seen fields + `*_pulled_at` (unless `--dry-run`).
  `lat`/`lon` are book values that were SET, not device reads.
- `data/fleet/polls.jsonl` line with `event: monitor`

Does not run ad-hoc remote CLI. Use [`cmd`](cmd.md).
