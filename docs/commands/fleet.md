# `envybot fleet`

Localhost fleet manager. Serves the map UI, GETs telemetry into
`data/fleet/history.sqlite`, and SETs radio policy.

```
./envybot [--book DIR] fleet [flags]
```

Default: UI at `http://127.0.0.1:8787/` plus live GET (status/telemetry/neighbors),
inventory gaps (fw/bl), and apply when the profile hash misses. Neighbor GET
sends remote `discover.neighbors`, waits 12s, then reads the table. Polls every
pollable MeshCore unit in the book, including bag/bench (no site `node:` bind).
`firmware_platform: meshtastic` is omitted from the UI and never polled or applied.

## Stores

| Store | Role |
|-------|------|
| `nodes.yaml` | Desired identity + `trust.admin` / `trust.guest`. Poll does not write. No GPS. |
| `keys.yaml` | Person → MeshCore companion pubkeys. Apply resolves names to ACL. |
| `sites.yaml` | Places (`loc`) and the 1:1 `node:` bind. Fleet writes bind only. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

First run imports leftover `polls.jsonl` (then deletes it) and YAML
`*_pulled_at` blobs, then strips observed keys from `nodes.yaml`.

## Poll cadence

| Mode | Groups | When |
|------|--------|------|
| periodic | `status`, `telemetry`, `neighbors` | `--min-interval` (default 24h) |
| inventory | `firmware`, `bootloader` | until sqlite stamp exists |
| audit | `name`, `lat`, `lon`, `advert`, `flood_advert`, `acl` | **Pull**, `--group`, or `--force` only |

Neighbors: remote `discover.neighbors` (zero-hop CTL) then `GET_NEIGHBOURS`.
`--no-discover` skips the search. `--discover-wait SEC` changes the listen
window (default 12). The UI hides rows older than 7 days. Firmware has no
TTL, so ghosts stay in sqlite history.

Default runs never GET sticky identity fields. Leak / mismatch in the UI
follow the apply profile stamp, not a heard-identity GET.

`paused: true` on a node skips auto GET and apply. The unit stays on the
map. **Refresh**, **Pull**, and **Push** still work from the UI. Unpause
(or delete the key) returns the unit to the next fleet run. Mid-run pause
takes effect at the next job boundary. `trust` / `cmd` do not honor pause.

## Job queue

Fleet work is a **fair serial command queue** (`jobs.py` + `fleet_worker.py`):

- Each unit gets a FIFO deque: login, then due GET groups (one exchange each),
  then due SET fields. Neighbors = `discover.neighbors`, a **timer job** (default
  12s, radio idle), then `GET_NEIGHBOURS`.
- One companion send+wait at a time across the whole fleet. Timeout **parks**
  that unit (head job kept, backoff, retry later) instead of blocking everyone.
  The UI badge stays on the current job stage (Logging in, Fetching ACL, …)
  while the unit is queued or retrying. `unreachable` only after `--attempts`
  is exhausted (or a hard fail).
- Pick order: manual Refresh/Pull/Push, inventory gaps (fw/bl), then
  least-recently-served among ready units.
- `--attempts` (default 10) caps retries **per command** at the scheduler.
  Attempt 2+ still resets to flood on that destination. `--retry-delay` /
  `--round-delay` control backoff between retries.
- UI Refresh/Pull/Push enqueue onto the same scheduler (priority over auto poll).
- Sqlite updates incrementally after each successful GET group or SET field.

## Manual jobs (UI)

While the companion worker is live:

| Action | GET | SET |
|--------|-----|-----|
| **Refresh** | status, telemetry, neighbors (ignore interval) | only if profile is due |
| **Pull** | Refresh plus fw, bootloader, name, lat, lon, advert, acl | only if profile is due |
| **Push** | none | force re-SET profile (incl. guest/admin passwords) |

List cards expose **Refresh** only. Detail adds **Pull** and **Push** (Push
is visually distinct; it rewrites passwords). CLI `--force` is Pull plus Push.

## Apply

Nodes without `public: true` get the privacy mask: name `Repeater`, lat/lon
`0,0`, adverts off, a unique strong guest password. ACL is resolved from
`keys.yaml` + `trust.admin` (perm 3) / `trust.guest` (perm 1). Heard keys
that are not in that list are dropped (`setperm 0`). `admin1_*` is
Meshtastic and is not applied. Blank, weak, or colliding guests are
rolled and written back to the book. `public: true` pushes site name (or
`unit_id` when bag/bench) plus resolved GPS.

Always also SETs `path.hash.mode` (default 1 = 2-byte), `dutycycle`
(default 100), a strong book admin via `password`, and clock if unset
or behind. Password-login every unit before GET or SET (login establishes
the repeater session and refreshes mesh paths). Live clock comes from
the login timestamp or `clock` CLI afterward.

Apply is due when any SET field stamp misses the book desired value
(stored in sqlite `applies` per field: name, lat, lon, advert, flood,
guest, admin, path_hash, dutycycle, acl, identity). A legacy ok
`applies.profile` row still means fully synced. `--force` clears field
stamps and re-SETs everything. Each attempt (including retries) prints
`apply need` / `apply skip` from current stamps, and `poll need` /
`poll skip` when GET groups remain. A private node still needs a guest password
assign. Heard name/GPS/adverts do **not** trigger apply. Edit a hashed field in `nodes.yaml` (or run `trust`) and restart fleet.

When apply runs, GET ACL once to drop keys not in the book allowlist.
Each SET is one queued command. Login is the reachability check. If a SET
gets no response, apply aborts for that unit (no lat/lon/guest/…).

Hashed: public/name/gps/adverts, guest + admin (tokens), identity pubkey,
path.hash, dutycycle, resolved ACL (pubkey + perm). Not hashed / not pushed here:
identity secret (`roll`), radio preset (onboard), clock.

`trust ben` (admin) updates the book and radio ACL, then stamps the new hash
on units that were already profile-synced so fleet does not re-push.

## Flags

Same companion flags as `cmd` (`--ble`, `--serial`, `--tcp`, `--timeout`,
`--attempts`, …).

| Flag | Meaning |
|------|---------|
| `--web-only` | Browse the book. No radio. |
| `--no-web` | Headless poll/apply |
| `--unit KEY` | One unit (repeatable) |
| `--force` | Pull every GET group and Push profile |
| `--live` | Periodic GET only (status/telemetry/neighbors) |
| `--no-discover` | GET neighbor table without remote `discover.neighbors` |
| `--discover-wait SEC` | Listen after discover (default 12; timer job, radio idle) |
| `--retry-delay SEC` | Backoff after a command timeout before retry |
| `--round-delay SEC` | Pause between scheduler retry rounds |
| `--poll-only` | GET only |
| `--apply-only` | SET only |
| `--deployed-only` | Site-bound units only (skip bag/bench) |

## UI

Map pins use **book** position (`sites.yaml` loc for the bound unit), never
device `0,0`. Pin labels are site name when bound, else unit id. Detail
shows unit id and site name, a `public` toggle, and drift from the apply
stamp (`leak` if a private profile is due, `mismatch` if a public profile
is due). List cards show site name with unit id when bound, or unit id
alone for bag/bench. While the companion worker is live, **Refresh** on a
unit pulls live telemetry now; **Pull** also GETs fw/name/GPS/advert/acl;
**Push** force-SETs the book profile (overrides `--skip`, `paused`, and
up-to-date skips). Pause is a checkbox on the detail card
(`paused: true`). Sidebar rows fade and badge as paused. Rows with
`decommissioned:` or `firmware_platform: meshtastic` are omitted
entirely.
