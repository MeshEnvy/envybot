---
name: fleet
description: >-
  envybot fleet: poll/apply, UI, --only/--skip filters, routing (path/flood/direct),
  stale hop cache (--refresh-paths), --force-path, companion BLE, Fleet idle vs queued
  work, Deploy vs apply-only vs full-sync. Read this before grepping fleet.py for basics.
---

# envybot fleet

Operator CLI + localhost dashboard for MeshCore repeaters over a **companion** radio (BLE/serial/TCP).

## Read first

1. [MEMORY.md](../../../MEMORY.md) — repo layout, book contract, routing one-liner
2. [docs/commands/fleet.md](../../../docs/commands/fleet.md) — full manual (cadence, UI, flags)
3. [docs/README.md](../../../docs/README.md) — docs index + quick reference

Book path: `--book DIR` or `ENVYBOT_HOME`. Example book: private `peaky-nevada/` (not in envybot repo). Never copy secrets into envybot.

## Quick start

```bash
uv sync
export ENVYBOT_HOME=/path/to/book
cd /path/to/envybot
./envybot fleet                              # UI http://127.0.0.1:8787/ + auto work
./envybot fleet --web-only                   # book browser, no radio
./envybot fleet --no-web --only me0048 --full-sync --companion 3355
```

## "Nothing is happening" / Fleet idle

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Fleet idle` right after start | No auto jobs queued (all poll/apply up to date) | **Deploy** in UI, or `--full-sync`, or edit book / clear stamps |
| `--apply-only` + synced profile | Apply not due; login never queued | **Deploy** / `--full-sync` to force SETs |
| Companion connects, neighbor ping only | Idle loop waiting for manual/scheduled work | Expected when idle |
| `--only` matched but no login lines | Filter worked; unit not due for queued phase | Same as idle |

Auto seed only queues units with **due** poll groups or **due** apply fields. `--full-sync` and UI **Deploy** bypass apply skip. `--only` bypasses miss-cooldown on startup seed.

Progress logs require default progress (do not pass `-q` / `--quiet`).

## Unit filters: `--only` / `--skip`

- Comma list and/or globs (`me00[45]*`). Repeatable flags.
- Matches (case-insensitive): book key, `unit_id`, alias, site slug, site name, **pubkey prefix** (4+ hex chars, e.g. `3d35` → me0048).
- Applied **early**: hidden from dashboard and auto poll/apply.
- Does **not** change whether apply is due.

## Apply: due vs synced

Apply due = any SET field stamp in sqlite `applies` differs from book desired (name, lat, lon, advert, flood, guest, admin, acl, identity, path_hash, dutycycle, ota_autofetch, powersaving, hop_retry, fem_rxgain, rxgain, …).

Before SETs, fleet prints:

```
apply need: fem_rxgain, name, … (v1:hash)
apply skip: none
```

If all fields synced: `apply skip: all` and `--apply-only` queues nothing.

**Deploy** (UI) or **`--full-sync`** (CLI) forces profile SETs regardless of stamps.

## Routing and companion paths

Mesh send policy is per-node in `nodes.yaml`: `routing: path | direct | flood`. Default when omitted: **path**.

| Policy | Sends use |
|--------|-----------|
| **path** | Companion cached `out_path` if present; otherwise flood-login to learn route; after 3 timeouts on cached path, cache discarded and re-flood |
| **direct** | Zero-hop every send |
| **flood** | Flood every send (danger — high airtime) |

### Log lines

On **login** (and CLI sends), progress shows the route about to be used:

```
  login
    path:
      GNGR1 (ea6e)
      → NextHop (e9bd)
      …
```

Or `path: flood` / `path: direct`.

Login **retries** (`login 2`, …) use compact logging: path reprints only if the route changed (e.g. after stale discard).

### Stale cached hops

Symptom: long wrong hop chain, logins timeout, operator moved or mesh changed.

**Do not** use `--force-path` to fix stale cache (that pins the bad route).

**Use `--refresh-paths`:**

1. After contact sync, for each filtered target with a cached route, prints **`{unit} path was:`** and hop list
2. Clears companion `out_path` via `reset_path`
3. Next login prints **`path: flood`** and flood-discovers a new route

```bash
./envybot fleet --only 3d35 --refresh-paths --full-sync --no-web --companion 3355
```

Book alternative: set `routing: flood` on the node (persistent always-flood — use sparingly).

### Pin a known good path

`--force-path EA6E,E9BD,C458,D709,04E2,1FD6` — comma-separated 2-byte hop hashes (hash_mode 1). Disables stale-cache discard for that run. Copy hops from a successful login dump (hashes in parentheses) or from sqlite audit:

```bash
sqlite3 "$ENVYBOT_HOME/data/fleet/history.sqlite" \
  "SELECT path FROM mesh_audit WHERE unit='ME0048' ORDER BY ts_sent DESC LIMIT 1;"
```

### Light path probe without full fleet

```bash
./envybot cmd 3d35 ver --companion 3355   # login + path on stderr, then one CLI
```

## Flag cheat sheet

| Flag | Meaning |
|------|---------|
| `--web-only` | Dashboard only, no radio |
| `--no-web` | Headless poll/apply |
| `--only SPEC` / `--skip SPEC` | Filter units (see above) |
| `--full-sync` | Deploy profile + periodic/inventory GETs (not weekly audit GETs) |
| `--apply-only` | SET only, skip GET cadence |
| `--poll-only` | GET only |
| `--refresh-paths` | Dump stale hops, clear cache, flood on next login |
| `--force-path HOPS` | Pin hops for this run |
| `--companion HINT` | Pubkey prefix or `keys.yaml` person slug (skip BLE picker) |
| `--attempts N` | Max retries per command (0 = unlimited) |
| `--retry-delay SEC` | Auto backoff after timeout (default 60) |
| `--deployed-only` | Site-bound units only |

Companion flags match `cmd`: `--ble`, `--serial`, `--tcp`, `--timeout`, `--login-timeout`, `-v`.

## UI manual jobs (companion worker live)

| Action | GET | SET |
|--------|-----|-----|
| **Refresh** | status, telemetry now | only if profile due |
| **Pull** | all GET groups | only if profile due |
| **Deploy** | none | full profile re-SET (always) |

`paused: true` skips auto poll/apply; manual three still work.

## Stores (do not confuse)

| Store | Holds |
|-------|--------|
| `nodes.yaml` | Desired config; not last-seen |
| `sites.yaml` | Site GPS + `node:` bind |
| `history.sqlite` | Observed telemetry, apply stamps, `mesh_audit` |

Reachability: cite sqlite last-seen, not YAML.

## Book validation (load gate)

All fleet commands load the book through `book_dal.load_book()` (not raw
`load_nodes_doc`). Invalid YAML **exits before radio work**.

### On-air advert name limit (not CLI 32)

MeshCore advert `app_data` is **32 bytes total** (`MAX_ADVERT_DATA_SIZE`).
Site-bound units that advertise GPS consume 9 bytes before the name (flags +
lat/lon), so the **on-air name is max 23 characters**.

| Piece | Budget |
|-------|--------|
| Full on-air name with GPS | **23 chars** |
| Default suffix ` {lora.sh}` | 10 chars |
| Max base `advert_name` with GPS + default suffix | **13 chars** |
| CLI `set name` / prefs | 32 chars (can be longer; advert truncates) |

Companion path labels and neighbor names use **heard advert names**, not the
full CLI name. Clipped names in `path:` lines (e.g. `Bare Mountain E {lora.s`)
mean the book name exceeds the 23-byte advert budget.

Validation checks every **site-bound MeshCore** unit:
`advert_name` + `public_advert.name_suffix` (or per-site `advert_suffix`).
Change the suffix and a formerly OK site can fail validation until you shorten
names.

```text
book validation failed:
  ME0048 / bare-mountain-east: on-air name 'Bare Mountain E {lora.sh}' is 25 chars; max 23 with GPS in advert (32-byte payload). ...
fix nodes.yaml / sites.yaml before running fleet
```

Hot reload (`sync_book`) runs the same checks. Fix YAML before fleet continues.

## Code pointers (when skill is not enough)

| Topic | File |
|-------|------|
| Book load + validation gate | `src/envybot/book_dal.py`, `public_advert.py` |
| CLI flags, startup, refresh-paths | `src/envybot/commands/fleet.py` |
| Job execution, login compact | `src/envybot/fleet_worker.py` |
| Path log, reset_to_flood, login | `src/envybot/radio.py` |
| Routing policy helpers | `src/envybot/routing.py` |
| `--only` matcher | `src/envybot/unit_filter.py` |
| Apply due / stamps | `src/envybot/apply.py` |
| Dashboard snapshot | `src/envybot/web/snapshot.py` |

## Maintenance

When changing fleet behavior, flags, routing, or idle/queue semantics: update **this skill**, [docs/commands/fleet.md](../../../docs/commands/fleet.md), and [MEMORY.md](../../../MEMORY.md) in the same change set.
