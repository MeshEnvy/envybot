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
| `nodes.yaml` | Desired identity + `trust.admin` / `trust.guest`. Poll does not write. Map pins: site GPS or per-node `loc` (mobile/bag). `bench_loc` is the ingestor pin plus sun/history backfill (not radio apply). |
| `keys.yaml` | Person → MeshCore companion pubkeys. Apply resolves names to ACL. |
| `sites.yaml` | Places (`loc`) and the 1:1 `node:` bind. Fleet writes bind only. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

Observed last-seen lives in sqlite only. Do not put telemetry or
`*_pulled_at` in `nodes.yaml`. First run still ingests leftover
`polls.jsonl` (then deletes it); that path is leftover, not a yaml
migrator.

## Book validation

Fleet, cmd, trust, and onboard load the book through `book_dal.load_book()`.
Invalid desired state exits before any radio work. Hot reload (`sync_book`)
runs the same checks.

Site-bound MeshCore units advertise GPS in the 32-byte MeshCore advert
payload. Lat/lon uses 9 bytes, so the **on-air name is max 23 characters**
(base `sites.yaml` `advert_name` + book `public_advert.name_suffix`). CLI
`set name` allows 32 characters; firmware truncates what goes on-air.
Path hop labels and neighbor names come from heard adverts, not the full CLI
name.

With default suffix ` {lora.sh}` (10 chars), keep `advert_name` at **13
characters or fewer**. Changing `name_suffix` can invalidate previously OK
sites until names are shortened.

## Poll cadence

| Mode | Groups | When |
|------|--------|------|
| periodic | `status`, `telemetry` | `--min-interval` (default 1h) |
| periodic | `ota_status`, `ota_ls` | 24h (`ota status` + delayed `ota ls`). Skip after `Unknown command` until firmware changes |
| periodic | `neighbors` | 24h (`discover.neighbors` + wait + GET) |
| inventory | `firmware`, `bootloader`, `ota` | until sqlite stamp exists |
| audit | `name`, `lat`, `lon`, `advert`, `flood_advert`, `acl` | weekly when stamped; **Pull** or `--group` forces now (3 attempts max) |

Neighbors: remote `discover.neighbors` (zero-hop CTL) then `GET_NEIGHBOURS`.
`--no-discover` skips the search. `--discover-wait SEC` changes the listen
window (default 12). The UI hides a neighbor when the hear is older than
7 days from now: radio `secs_ago` at GET, plus time since that pull.
Firmware has no TTL, so ghosts stay in sqlite history.

A long-running `fleet` (not `--once`) re-checks due groups about every 60s
while idle, and after each swim-lane batch so a missing profile can SET
while other units are still GETting. No extra radio traffic unless
status/telemetry is ≥1h stale, OTA/neighbors ≥24h, or apply is due. UI
freshness stays 24h.

Default runs never GET sticky identity fields unless the weekly audit
interval elapsed. UI ``due`` follows per-field apply stamps. Audit GET
mismatch clears that field's stamp and re-queues SET. ``leak`` is a
later pull that still shows advert or flood advert on. Leftover name or
GPS is not an advert. A last-seen interval older than the apply stamp is
ignored. Book edits to `sites.yaml`, `public_advert`, or `keys.yaml`
reload live during a long fleet run; adding a new unit row still needs
restart. Pre-0.1.3 units: pin `advert_interval_min: 0` and
`flood_advert_interval_h: 0` on the node row until OTA ≥0.1.3.

`paused: true` on a node skips auto GET and apply. The unit stays on the
map modal. **Refresh**, **Pull**, and **Deploy** still work from the UI. Unpause
(or delete the key) returns the unit to the next fleet run. Mid-run pause
takes effect at the next job boundary. `trust` / `cmd` do not honor pause.

`routing: auto | path | direct | flood` is mesh send policy (default **auto**
when omitted). Auto uses companion hop cache, flood-logins when empty, and
discards stale cache after 3 timeouts. **path** locks hops in book `route:`.
`direct` forces zero-hop every send. `flood` always floods (danger). Mid-run
change takes effect on the next login/send. Detail **Route** dropdown;
auto shows live companion cache.

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
- Pick order: a manual Refresh/Pull/Deploy stays at the front until that
  click finishes, then least-recently-served among ready lanes. No inventory
  priority. Two manuals interleave with each other.
- `--attempts` (default 10) caps retries **per command** at the scheduler.
  Logs show scheduler `N/max` (e.g. `8/10`), not inner one-shot `N/1`.
  Login uses book routing policy (default auto). Later GET/CLI/binary in that
  session use the cached route when auto mode has one
  (`mesh_audit.path` = hop string, `direct`, or `flood` on discover/retry).
  Auto flood-discovers when cache is empty. Three consecutive timeouts on
  a cached path discard the cache and re-flood. Locked **path** re-applies
  book `route:` every send.
  `--retry-delay` (default 60s) parks auto poll/apply units after a timeout
  before retry; `--miss-cooldown` (default 3600s) skips re-seed after max
  attempts. Console and manual Refresh/Pull/Deploy are exempt; manual UI also
  clears cooldown. `--full-sync` or `--only` bypass cooldown on startup seed.
  `--round-delay` pauses between scheduler retry rounds. On drop: `gave up after N,
  continuing` (actual attempts, and only when that job was dropped). A
  displaced in-flight login is not a give-up. Failed login drops remaining
  console CLI (`stopping`, not `continuing`). On unit done with gaps: `partial OK`.
- Per-attempt mesh audit rows land in sqlite `mesh_audit` (unit, kind, label,
  path, wait, outcome, reply snippet). Detail card **Audit** table,
  `GET /api/audit/{unit}?limit=40&before_id=` (newest first, **Load older**),
  and live `audit` SSE events on each send begin/finish.
- UI Refresh/Pull/Deploy always enqueue, even while that unit is polling.
  The click replaces remaining jobs for that unit and runs next.
- Sqlite updates incrementally after each successful GET group or SET field.

## Poll history (UI)

Status, telemetry, neighbors, and ACL are stored in orthogonal sqlite tables.
`/api/polls/{unit}` returns `{ histories: { status, telemetry, polls,
neighbors, acl, sun } }`. `/api/audit/{unit}` returns `{ rows, has_more }`
from `mesh_audit` (cursor `before_id`, default limit 40). The detail card
shows **Audit** (mesh sends) and a single **Polls** table
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
Cards show book apply prefs (power saving, FEM LNA, AGC reset, duty cycle, path hash,
OTA autofetch) with applied/due from sqlite stamps. Not a live radio GET.

## Manual jobs (UI)

While the companion worker is live:

| Action | GET | SET |
|--------|-----|-----|
| **Sync** | status, telemetry | SET fields whose book desired differs from apply stamp |
| **Full sync** | all GET groups (incl. OTA, neighbors, profile audit) | SET dirty fields after reconcile |

Unit detail: **Active** (auto poll/apply), **Sync**, **Full sync** + interval dropdown (`full_sync_interval`: 24h, 7d, 30d, off). **Profile** edits radio prefs and credentials in the book; **ACL** is a read-only table (2-byte prefix, role, person) with per-row status vs last `get acl` (✓ in sync, ⚠ book-only, ! unknown on radio). Edit `nodes.yaml` trust and `keys.yaml` offline, then Sync when `acl` is dirty. CLI `--full-sync` forces GET policy on a headless run.

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
- Refresh / Pull / Deploy stay available. They sit behind queued console
  sends on the same unit.
- `--web-only` can open tabs; send returns 409 (no radio). A normal
  `fleet` start accepts console send during companion handshake and
  holds the line until the radio is up.
- URL: `?console=1` reopens the modal (`&ctab=` selects a tab). `?unit=`
  is only the detail card. `?map=1` reopens the map modal.
- Map sidebar row or pin: first click selects and flies; second click on the
  same unit opens detail and hides the map. Dashboard card click opens detail.
  Cards and detail show `board` from the book.
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
done), optional `powersaving`, `hop_retry`, and `hop_retry_ms` when set in the book.
RF sensitivity SETs run first after login: `radio.fem.rxgain` (default on),
`agc.reset.interval` (default 4), optional `radio.rxgain` when the book sets it
(`board: heltec-t096`), then optional `powersaving off/on`, `set hop.retry`, and
`set hop.retry.ms` before identity or OTA SETs. Missing CLI stamps done.
Temporary off is firmware `try`. Also SETs a strong book admin
via `password`, and clock if unset
or behind (`time` uses the same `--attempts` lane as other SETs; a clock
give-up still sends the identity advert). After apply finishes, if name or GPS changed this pass, fleet sends
`advert` (flood) so the mesh hears the new identity. Password-login every unit before GET or SET (login establishes
the repeater session and refreshes mesh paths). Live clock comes from
the login timestamp or `clock` CLI afterward.

Apply is due when any SET field stamp misses the book desired value
(stored in sqlite `applies` per field: name, lat, lon, advert, flood,
guest, admin, path_hash, dutycycle, ota_autofetch, powersaving, hop_retry,
hop_retry_ms, fem_rxgain, agc_reset_interval, rxgain, acl, identity). A successful
[`onboard`](onboard.md) stamps those fields so a new private unit is not
due for a first mesh apply. `--full-sync` or **Deploy** clears field
stamps and re-SETs everything (full-sync skips audit GETs on that pass).
Each attempt (including retries) prints
`apply need` / `apply skip` from current stamps, and `poll need` /
`poll skip` when GET groups remain. A queued SET whose stamp already
matches prints `field: skip (synced)` and does not go on the air. A private node still needs a guest password
assign. Heard name/GPS/adverts do **not** trigger apply. Edit a hashed field in `nodes.yaml` (or run `trust`) and restart fleet.

When apply runs, GET ACL once to drop keys not in the book allowlist.
Each SET is one queued command. Login is the reachability check. If a SET
gets no response, apply aborts for that unit (no lat/lon/guest/…). Clock
is the exception: retries, then continues to the identity advert.

Hashed: public/name/gps/adverts, guest + admin (tokens), identity pubkey,
path.hash, dutycycle, ota_autofetch, powersaving, hop_retry, hop_retry_ms, fem_rxgain,
rxgain, resolved ACL (pubkey + perm). Not hashed / not pushed here:
identity secret (`roll`), radio preset (onboard), clock.

`trust ben` (admin) updates the book and radio ACL, then stamps the new hash
on units that were already profile-synced so fleet does not re-push.

## Flags

Same companion flags as `cmd` (`--companion`, `--ble`, `--serial`, `--tcp`,
`--timeout`, `--attempts`, …). With several BLE devices visible, pass
`--companion HINT` to skip the interactive picker: a pubkey hex prefix
(e.g. `3355` or `3355e0fc8bc8`) or a `keys.yaml` person slug (e.g. `ben`).

On companion connect (not `--web-only`), fleet sends a zero-hop repeater
discover ping (MeshCore app Tools-style `NODE_DISCOVER_REQ`), listens 10s,
and prints nearby repeaters that answer (name, pubkey prefix, SNR). Empty
list warns that fleet may not get out. Requires companion firmware with
`CMD_SEND_CONTROL_DATA` (v8+); older companions skip silently.

| Flag | Meaning |
|------|---------|
| `--web-only` | Browse the book. No radio. |
| `--no-web` | Headless poll/apply |
| `--only SPEC` | Include only matching units (comma list or glob; repeatable). Matches book key, `unit_id`, alias, site slug, site name, or identity pubkey prefix (4+ hex chars, e.g. `3d35`). Filters the dashboard and auto poll/apply. |
| `--skip SPEC` | Exclude matching units (same matcher as `--only`; repeatable). Filters the dashboard and auto poll/apply. |
| `--full-sync` | Deploy profile plus periodic/inventory GETs (no audit GETs on same pass) |
| `--refresh-paths` | Dump each target's stale cached hops, clear companion `out_path`, then flood on next login (use when preset route is wrong) |
| `--force-path HOPS` | Pin companion `out_path` to comma-separated hop hashes (e.g. `EA6E,E9BD,C458`). Overrides flood/path discovery for this run; stale-cache discard is disabled while pinned |
| UI **Route** paste | Per-unit session pin (same as `--force-path` for one target). Paste fleet log text; 4-hex tokens become hops. **`DELETE /api/unit/{key}/path`** clears pin. Not persisted in `nodes.yaml` |
| `--live` | Periodic GET only (status/telemetry/neighbors) |
| `--no-discover` | GET neighbor table without remote `discover.neighbors` |
| `--discover-wait SEC` | Listen after discover (default 12; timer job, radio idle) |
| `--retry-delay SEC` | Auto only: park unit after timeout before retry (default 60). Console and manual UI exempt. |
| `--miss-cooldown SEC` | Auto only: after max attempts, skip re-seed until cooldown (default 3600). `--full-sync`, `--only`, or manual UI bypass. |
| `--round-delay SEC` | Pause between scheduler retry rounds |
| `--poll-only` | GET only |
| `--apply-only` | SET only |
| `--no-auto-update` | No automatic due GET/apply. UI **Refresh**, **Pull**, and **Deploy** only. |
| `--deployed-only` | Site-bound units only (skip bag/bench) |

## UI

Map pins use **book** position (`sites.yaml` loc for the bound unit), never
device `0,0`. Pin color is last-heard age (green now, amber at 12h, red at
24h+; never-heard is gray). Labels are `Site (3h)` when bound, else book
alias, else unit id. Age ticks live from `last_heard`. Detail shows unit id and site name, editable **alias** and **notes**
(when bag/bench, alias becomes the list title), `public` / `pause`,
**Route** dropdown (auto / path / direct / flood) and hop display (path mode:
paste a log dump and save to book `route:`), and drift
from the apply stamp (`due` when the profile is stale) or a later pull
that still shows advert on (`leak`). Leftover name or GPS is not an advert.
List cards show the same primary label with unit id
as secondary when it differs. While the companion worker is live, **Refresh** on a
unit pulls live telemetry now; **Pull** also GETs fw/name/GPS/advert/acl;
**Deploy** re-SETs the book profile (overrides `paused` and up-to-date skips).
CLI `--skip` hides units from the dashboard. Dashboard list filters: **Active**
is every unit that is not paused (idle or queued). **In flight** is queued or
on the companion radio now. **Active** on the detail card turns auto poll/apply
on or off (`paused: true` in the book when off). Route mode is
Auto (default) / Path / Direct / Flood on the detail card; list cards show live
route and a danger badge when mode is flood.
Sidebar rows fade and badge as paused. Rows with
`decommissioned:` or `firmware_platform: meshtastic` are omitted
entirely.
