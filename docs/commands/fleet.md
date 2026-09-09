# `envybot fleet`

Localhost fleet manager. Serves the dashboard UI, GETs telemetry into
`data/fleet/history.sqlite`, and SETs radio policy.

```
./envybot [--book DIR] fleet [flags]
```

Default: UI at `http://127.0.0.1:8787/` (`?unit=me0032` restores the open
card; `?map=1` reopens the map modal) plus live GET (status/telemetry),
inventory gaps (fw/bl/ota base hash), and apply when the profile hash misses.
OTA session (`ota status` + delayed `ota ls`) and neighbors stay on a 24h
auto cadence; Pull fetches them now. Neighbor GET
sends remote `discover.neighbors`, waits 12s, then reads the table. Heard
rows show approximate miles (book display GPS for fleet peers, companion
advert GPS for community nodes). Polls every
pollable MeshCore unit in the book, including bag/bench (no site `node:` bind).
Contact sync drops an old companion record when a unit's pubkey changes
(same `unit_id` or site name). Bare `Repeater` contacts stay.
`firmware_platform: meshtastic` is omitted from the UI and never polled or applied.

## Stores

| Store | Role |
|-------|------|
| `nodes.yaml` | Desired identity + `trust.admin` / `trust.guest`. Poll does not write. `bench_loc` + optional per-node `loc` for map/sun/history only (not radio apply). |
| `keys.yaml` | Person → MeshCore companion pubkeys. Apply resolves names to ACL. |
| `sites.yaml` | Places (`loc`) and the 1:1 `node:` bind. Fleet writes bind only. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

Observed last-seen lives in sqlite only. Do not put telemetry or
`*_pulled_at` in `nodes.yaml`. First run still ingests leftover
`polls.jsonl` (then deletes it); that path is leftover, not a yaml
migrator.

## Poll cadence

| Mode | Groups | When |
|------|--------|------|
| periodic | `status`, `telemetry` | `--min-interval` (default 1h) |
| periodic | `ota_status`, `ota_ls` | 24h (`ota status` + delayed `ota ls`). Skip after `Unknown command` until firmware changes |
| periodic | `neighbors` | 24h (`discover.neighbors` + wait + GET) |
| inventory | `firmware`, `bootloader`, `ota` | until sqlite stamp exists |
| audit | `name`, `lat`, `lon`, `advert`, `flood_advert`, `acl` | **Pull**, `--group`, or `--force` only |

Neighbors: remote `discover.neighbors` (zero-hop CTL) then `GET_NEIGHBOURS`.
`--no-discover` skips the search. `--discover-wait SEC` changes the listen
window (default 12). The UI hides rows older than 7 days. Firmware has no
TTL, so ghosts stay in sqlite history.

A long-running `fleet` (not `--once`) re-checks due groups about every 60s
while idle, and after each swim-lane batch so a missing profile can SET
while other units are still GETting. No extra radio traffic unless
status/telemetry is ≥1h stale, OTA/neighbors ≥24h, or apply is due. UI
freshness stays 24h.

Default runs never GET sticky identity fields. UI ``due`` follows the
apply profile stamp. ``leak`` is a later pull that still shows advert
or flood advert on. Leftover name or GPS is not an advert. A last-seen
interval older than the apply stamp is ignored.

`paused: true` on a node skips auto GET and apply. The unit stays on the
map modal. **Refresh**, **Pull**, and **Push** still work from the UI. Unpause
(or delete the key) returns the unit to the next fleet run. Mid-run pause
takes effect at the next job boundary. `trust` / `cmd` do not honor pause.

`routing: direct | path | flood` is mesh send policy (default **path** when
omitted). Path uses cached route (including learned zero-hop direct),
flood-logins when cache is empty, and discards stale cache after 3 timeouts.
`direct` forces zero-hop every send. `flood` always floods (danger). Mid-run
change takes effect on the next login/send. Detail **Routing policy** control;
list/detail show **live route**.

## Job queue

Fleet work is a **swim-lane round-robin dispatcher** (`jobs.py` +
`fleet_worker.py`):

- Each unit owns a FIFO deque: login, due SET fields, then due GET groups
  (one exchange each). Neighbors = `discover.neighbors`, a **timer job** (default
  12s, background sleep, radio idle), then `GET_NEIGHBOURS`. If a unit is
  already GETting and the profile stamp is due, SET jobs splice in after the
  in-flight command.
- One companion send+wait at a time across the whole fleet. After each radio
  command the lane goes to the back. Timeout **parks** that unit's head job
  (backoff, retry on its next turn) while other lanes keep sending. The UI
  badge stays on the current job stage (Logging in, Fetching ACL, …) while the
  unit is queued or retrying. `unreachable` only after `--attempts` is exhausted
  on login/GET (or a hard fail). A SET timeout is not unreachable.
- Pick order: a manual Refresh/Pull/Push stays at the front until that
  click finishes, then least-recently-served among ready lanes. No inventory
  priority. Two manuals interleave with each other.
- `--attempts` (default 10) caps retries **per command** at the scheduler.
  Logs show scheduler `N/max` (e.g. `8/10`), not inner one-shot `N/1`.
  Login uses book routing policy (default path). Later GET/CLI/binary in that
  session use the cached route when path mode has one
  (`mesh_audit.path` = hop string, `direct`, or `flood` on discover/retry).
  Path mode flood-discovers when cache is empty. Three consecutive timeouts on
  a cached path discard the cache and re-flood.
  `--retry-delay` (default 60s) parks auto poll/apply units after a timeout
  before retry; `--miss-cooldown` (default 3600s) skips re-seed after max
  attempts. Console and manual Refresh/Pull/Push are exempt; manual UI also
  clears cooldown. `--force` or `--unit` bypass cooldown on startup seed.
  `--round-delay` pauses between scheduler retry rounds. On drop: `gave up after N,
  continuing` (actual attempts, and only when that job was dropped). A
  displaced in-flight login is not a give-up. Failed login drops remaining
  console CLI (`stopping`, not `continuing`). On unit done with gaps: `partial OK`.
- Per-attempt mesh audit rows land in sqlite `mesh_audit` (unit, kind, label,
  path, wait, outcome, reply snippet). Query the book DB; no UI yet.
- UI Refresh/Pull/Push always enqueue, even while that unit is polling.
  The click replaces remaining jobs for that unit and runs next.
- Sqlite updates incrementally after each successful GET group or SET field.

## Poll history (UI)

Status, telemetry, neighbors, and ACL are stored in orthogonal sqlite tables.
`/api/polls/{unit}` returns `{ histories: { status, telemetry, polls,
neighbors, acl, sun } }`. The detail card shows a single **Polls** table
from `polls` (`poll_snapshots`: status spine with nearest/interpolated
telemetry, deltas, reboot detection). Status and telemetry arrays remain
for sparkline fallback. Samples store display GPS (bound site, else node
`loc`, else book `bench_loc`). A one-shot sqlite backfill copies the
current display loc onto older NULL rows (`sample_loc_backfill_v2`; v1
was site-only). Opening fleet runs backfill once. The UI may POST
`/api/bench` from browser geolocation (debounced) to move `bench_loc`
while the laptop travels; that does not SET radio lat/lon and does not
rewrite history. Poll rows show ☀️ or 🌙, voltage, traffic deltas,
temperature (with vs-ambient delta), Open-Meteo weather column, and gap.
Interpolated gauges are italic. Detail 10 rows, **Load more** +10. Sparklines
prefer `polls` when loaded. Default history limit is 80 (72h). SSE `unit`
events merge live status/telemetry into the polls list.
Cards show book apply prefs (power saving, FEM LNA, duty cycle, path hash,
OTA autofetch) with applied/due from sqlite stamps. Not a live radio GET.

## Manual jobs (UI)

While the companion worker is live:

| Action | GET | SET |
|--------|-----|-----|
| **Refresh** | status, telemetry (ignore interval) | only if profile is due |
| **Pull** | all GET groups (incl. OTA + neighbors) | only if profile is due |
| **Push** | none | force re-SET profile (incl. guest/admin passwords) |

List cards expose **Refresh** only. Detail adds **Pull** and **Push** (Push
is visually distinct; it rewrites passwords). CLI `--force` is Pull plus Push.

### Console (header)

The header terminal icon opens a tabbed console modal. Opening a tab does
not pause poll/apply. Hide (Esc, backdrop, icon) leaves tabs running.
Each unit row has the same icon: opens the first tab for that unit, or
creates one. Click it again while that unit's tab is showing to hide the
console. The header icon also toggles.

- **+** opens another tab. The same unit can have more than one session
  (labels `Ophir`, `Ophir 2`). Tab `×` cancels that session only.
- Typed lines are MeshCore CLI (`ota status`, `get name`, …). Login floods
  deployed units once; later lines in that session use the learned path.
  Login runs on the first send if that unit is not already authed
  (same `--attempts` cap as GET/SET).
- That command jumps to the front of the radio until heard, cancelled, or
  `--attempts` timeouts (default 10, same unit, no rotate). **Cancel** is
  per tab and starts the next staged line. After the last timeout or a
  hard fail, the line parks (queue stays): **Retry** sits next to the
  stopped error, including cancelled or timeout rows already in history.
  Retry is hidden while that line is still sending. **Continue** (only if
  the queue is nonempty) archives it and starts the next staged cmd.
  Further lines stage behind the current or parked line and can be edited
  or deleted.
- An in-flight poll/apply wait finishes, then the console head runs.
- Refresh / Pull / Push stay available. They sit behind queued console
  sends on the same unit.
- `--web-only` can open tabs; send returns 409 (no radio). A normal
  `fleet` start accepts console send during companion handshake and
  holds the line until the radio is up.
- URL: `?console=1` reopens the modal (`&ctab=` selects a tab). `?unit=`
  is only the detail card. `?map=1` reopens the map modal.
- Map sidebar row or pin: first click selects and flies; second click on the
  same unit opens detail and hides the map. Dashboard card click opens detail.
- Up/down in the input recalls commands sent on that tab. Separate from
  the transcript: **Clear history** does not wipe recall. The clipboard
  icon copies the transcript.
- A red `*` on a tab means that session's transcript changed since you
  last viewed it. The header icon shows the same `*` while the modal is
  hidden. Opening the tab (or the modal onto that tab) clears it.
- History, the staging queue, and a parked failed line survive browser
  refresh and `fleet` restart in `localStorage` (`envybot.console.v1`).
  After a process restart they reseed when this origin reconnects (a
  parked line is not auto-sent).
- Audit: `commands.source='console'` and `mesh_audit.source='console'` (same
  tables as `./envybot cmd`; redacted passwords).
- Tracked poll CLI replies (`ota status`, `ota stats`, `ota self`, `ota ls`,
  `ver`, `get bootloader.ver`, `get name` / `lat` / `lon` / advert intervals,
  `get acl`) also stamp sqlite last-seen like a poll. Binary status and
  telemetry still need Refresh. Neighbors stay on the 24h auto cadence
  (or Pull).

## Apply

Nodes without `public: true` get the privacy mask: name `Repeater`, lat/lon
`0,0`, adverts off, a unique strong guest password. ACL is resolved from
`keys.yaml` + `trust.admin` (perm 3) / `trust.guest` (perm 1). Heard keys
that are not in that list are dropped (`setperm 0`). `admin1_*` is
Meshtastic and is not applied. Blank, weak, or colliding guests are
rolled and written back to the book. `public: true` pushes site name (or
`unit_id` when bag/bench) plus resolved GPS.

Always also SETs `path.hash.mode` (default 1 = 2-byte), `dutycycle`
(default 50, stock MeshCore), `ota config autofetch` (default `off`; missing CLI stamps
done), optional `powersaving` / `fem_rxgain` / `rxgain`
when those keys are in the book (missing CLI stamps done). `rxgain`
needs `board: heltec-t096`. Temporary off is firmware `try`. Also SETs a strong book admin
via `password`, and clock if unset
or behind. Password-login every unit before GET or SET (login establishes
the repeater session and refreshes mesh paths). Live clock comes from
the login timestamp or `clock` CLI afterward.

Apply is due when any SET field stamp misses the book desired value
(stored in sqlite `applies` per field: name, lat, lon, advert, flood,
guest, admin, path_hash, dutycycle, ota_autofetch, powersaving, fem_rxgain, rxgain, acl, identity). A successful
[`onboard`](onboard.md) stamps those fields so a new private unit is not
due for a first mesh apply. A legacy ok `applies.profile` row still
means fully synced. `--force` clears field stamps and re-SETs everything. Each attempt (including retries) prints
`apply need` / `apply skip` from current stamps, and `poll need` /
`poll skip` when GET groups remain. A queued SET whose stamp already
matches prints `field: skip (synced)` and does not go on the air. A private node still needs a guest password
assign. Heard name/GPS/adverts do **not** trigger apply. Edit a hashed field in `nodes.yaml` (or run `trust`) and restart fleet.

When apply runs, GET ACL once to drop keys not in the book allowlist.
Each SET is one queued command. Login is the reachability check. If a SET
gets no response, apply aborts for that unit (no lat/lon/guest/…).

Hashed: public/name/gps/adverts, guest + admin (tokens), identity pubkey,
path.hash, dutycycle, ota_autofetch, powersaving, fem_rxgain, rxgain, resolved ACL (pubkey + perm). Not hashed / not pushed here:
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
| `--refresh-paths` | Clear companion cached hop paths for all poll targets before work (operator moved; next login floods to rediscover) |
| `--live` | Periodic GET only (status/telemetry/neighbors) |
| `--no-discover` | GET neighbor table without remote `discover.neighbors` |
| `--discover-wait SEC` | Listen after discover (default 12; timer job, radio idle) |
| `--retry-delay SEC` | Auto only: park unit after timeout before retry (default 60). Console and manual UI exempt. |
| `--miss-cooldown SEC` | Auto only: after max attempts, skip re-seed until cooldown (default 3600). `--force`, `--unit`, or manual UI bypass. |
| `--round-delay SEC` | Pause between scheduler retry rounds |
| `--poll-only` | GET only |
| `--apply-only` | SET only |
| `--deployed-only` | Site-bound units only (skip bag/bench) |

## UI

Map pins use **book** position (`sites.yaml` loc for the bound unit), never
device `0,0`. Pin color is last-heard age (green now, amber at 12h, red at
24h+; never-heard is gray). Labels are `Site (3h)` when bound, else book
alias, else unit id. Age ticks live from `last_heard`. Detail shows unit id and site name, editable **alias** and **notes**
(when bag/bench, alias becomes the list title), `public` / `pause`,
**routing policy** and **live route**, and drift
from the apply stamp (`due` when the profile is stale) or a later pull
that still shows advert on (`leak`). Leftover name or GPS is not an advert.
List cards show the same primary label with unit id
as secondary when it differs. While the companion worker is live, **Refresh** on a
unit pulls live telemetry now; **Pull** also GETs fw/name/GPS/advert/acl;
**Push** force-SETs the book profile (overrides `--skip`, `paused`, and
up-to-date skips). Pause is a checkbox on the detail card
(`paused: true`). Routing policy is Path (default) / Direct / Flood on the
detail card; list cards show live route and a danger badge when policy is flood.
Sidebar rows fade and badge as paused. Rows with
`decommissioned:` or `firmware_platform: meshtastic` are omitted
entirely.
