# EnvyBot

Public fleet CLI. Book (identity, creds) is private and outside this repo.
Observed last-seen lives in the book's SQLite, not in YAML.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.1.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `fleet`, `trust`, `cmd`, `onboard`, `seed`, `weather` |
| Book | `--book` / `ENVYBOT_HOME` / cwd with `nodes.yaml` (+ `keys.yaml`, `channels.yaml`) |

## Layout

| Path | Role |
|---|---|
| `./envybot` | Root shim (reexec `.venv`) |
| `src/envybot/cli.py` | Dispatcher; `COMMANDS` registry |
| `src/envybot/book.py` | Resolve book dir; never write secrets here |
| `src/envybot/keys_doc.py` | `keys.yaml` people + `trust` role resolve |
| `src/envybot/channels_doc.py` | `channels.yaml` catalog + companion slot planner |
| `src/envybot/nodes_doc.py` | Desired `nodes.yaml` load/write |
| `src/envybot/history.py` | `data/fleet/history.sqlite` (+ `mesh_audit` per send; `list_mesh_audit`) |
| `src/envybot/health.py` | Per-node health checks (snapshot + UI grade) |
| `src/envybot/position.py` | Map pin: site → node `loc`. Sun/history: + `bench_loc` (ingestor). Apply: site only |
| `src/envybot/sun.py` | Clear-sky elev: ☀️/🌙 + 72h elevation sparkline |
| `src/envybot/weather.py` | Open-Meteo cache + attach on poll history |
| `src/envybot/radio.py` | Companion session, login, CLI/binary |
| `src/envybot/poll.py` | GET cadence (live / inventory / audit) → sqlite |
| `src/envybot/apply.py` | SET mask unless `public: true`; `v1:` profile hash |
| `src/envybot/passwords.py` | Password strength + uniqueness (no shared defaults) |
| `src/envybot/jobs.py` | Swim-lane job queue (one radio command per unit per turn) |
| `src/envybot/fleet_worker.py` | Per-command job build + execute |
| `src/envybot/commands/fleet.py` | Localhost manager |
| `src/envybot/commands/trust.py` | Contacts + channels + keys.yaml / ACL login |
| `src/envybot/commands/cmd.py` | Remote MeshCore CLI |
| `src/envybot/commands/onboard.py` | USB repeater text CLI onboard (`path.hash.mode` 1, `get acl` is `ACL:` dump). Stamps private `applies` so fleet is ready. |
| `src/envybot/mota.py` | Light `.mota` parse for USB seeder catalog |
| `src/envybot/seeder.py` | mota-seeder host (COUNT / DESCRIBE / READ); sets `dutycycle 10` on launch |
| `src/envybot/commands/seed.py` | USB OTA folder relay (`envybot seed`) |
| `src/envybot/commands/weather.py` | Open-Meteo cache backfill (`envybot weather backfill`) |
| `src/envybot/web/` | Fleet UI (`:8787`); header **Console** (tabbed priority CLI) |

## Contract

- Greenfield (unshipped): `.cursor/rules/greenfield.mdc`. No builtin
  yaml/sqlite migrators, dual-read, or leftover book keys. One-shot
  book cleanup is throwaway Python, not product code. No `monitor`
  alias. No last-seen in YAML.
- `nodes.yaml` is SoT for **desired** identity. Cite sqlite `last_seen` for
  reachability / fw / battery / uptime / temperature.
- GPS for **apply** lives on `sites.yaml` (`loc` + `node:` bind). Apply
  SETs `0,0` unless `public: true`. Never GET device coords into YAML.
- **Map** pins: bound site → optional node `loc` (mobile/bag). No `bench_loc`
  fallback on units. **Sun/history** still use `bench_loc` after node `loc`.
  Fleet UI POSTs `/api/bench` for the ingestor pin.
  GPS (debounced); that does not SET radios. One-shot sqlite backfill v2
  stamps NULL history rows once.
- `fleet` / `trust` / `cmd` skip `firmware_platform: meshtastic` even
  when leftover MeshCore pubkey/admin exist. No UI, poll, apply, or
  edits. Blank platform = meshcore.
- `paused: true` stays in the UI. Fleet skips auto poll/apply. Sync and
  Full sync still hit the radio. Trust/cmd ignore the flag.
- `routing: auto | path | direct | flood` is mesh send policy (default **auto**
  when omitted). Auto uses companion hop cache, flood-logins when empty, and
  discards stale cache after 3 timeouts. **path** locks hops in book `route:`.
  `direct` forces zero-hop every send. `flood` always floods (danger).
  **`--refresh-paths`:** startup logs stale cached hops per target, clears
  companion hop cache, then next login floods to rediscover. Detail **Route**
  dropdown (auto/path/direct/flood); auto shows live companion cache.
  **Next (ops 09-19, not built):** favorite-route graph (official + community
  guest hops). Launch neighbor-ask. Known path prefix. Traceroute. Do not
  flood-discover when infra is mapped. — `ops/initiatives/envybot-radio-daemon.md`.
- `decommissioned:` (epoch) rows stay in `nodes.yaml` for the number
  but envybot ignores them: no UI, poll, apply, trust, cmd, or onboard.
- `fleet` and `trust` poll every pollable MeshCore unit, including
  bag/bench (no site bind). `--deployed-only` narrows fleet to
  site-bound units only. **`--no-auto-update`:** no due seeding; manual UI
  jobs only. Contact name is the site `name` (e.g. Ophir),
  else `unit_id`. Stale advert names on the same key are removed and
  re-added. A replaced chip (same unit/site name, new pubkey) drops the
  old companion contact; bare `Repeater` names stay. `trust ben`
  records the live tag in `keys.yaml` and password-auths onto MeshCore
  units (`--unit` scopes that pass; contacts still import the full book).
  On units already profile-synced, trust stamps the new hash so fleet
  does not re-push. Guest grants do not update remotes; fleet `setperm`s.
  `admin1_*` is Meshtastic, not MC ACL.
- `channels.yaml` (channel-first) lists group name + 16-byte PSK + who
  gets it (`everyone` or person slugs). `trust` add/updates granted
  channels on the companion; extras on the tag are left alone. `public`
  and `MeshEnvy` are never applied. Missing file skips channel work.
- On-air name: site public advert when bound, else `unit_id` (bench/bag).
  Display name is site `name` when bound, else book `alias`, else `unit_id`.
  Alias is UI/selector only (not pushed to radio).
- Site-bound apply SETs public advert profile (fuzzed GPS, owner info, site
  advert name) and defaults `advert.interval 0` + `flood.advert.interval 12`
  unless the row overrides. Bench/unbound SETs adverts off. Pre-0.1.3 units:
  pin `advert_interval_min: 0` + `flood_advert_interval_h: 0` on the node row
  until OTA ≥0.1.3 (book-only; no firmware gate in apply).
- Passwords are unique and strong per unit. Apply/onboard roll blank, weak
  (`m35h3nvy`, placeholders, short), or colliding guests. Admin is never
  invented by apply. Apply SETs book admin (`password`) after ACL/login.
- Apply due = per-field sqlite `applies` (name, lat, lon, …, ota_autofetch)
  vs book desired, or weak guest assign. Audit GET mismatch clears that
  field's stamp and re-queues SET. Guest password rolls persist to disk at
  assign time. `sync_book` reloads `nodes.yaml` / `sites.yaml` / `keys.yaml`
  in place during a long fleet run (new units still need restart).
  `ota config autofetch` `Unknown command` stamps the field done (no CLI).
  A SET timeout still retries. Onboard stamps those after USB SET + ACL
  (private only). `--full-sync` is Deploy plus periodic/inventory GETs
  (no audit GETs on the same pass).
  UI ``due`` is that stamp (apply needed). ``leak`` is a later pull that
  still shows advert or flood advert on. Name or GPS is not an advert.
  A last-seen interval older than the apply stamp is ignored. Poll default: status/telemetry every 1h
  (`--min-interval`); neighbors and OTA (`ota status` + delayed `ota ls`)
  stay 24h (`discover.neighbors` + wait + GET; UI drops neighbor rows older
  than 7d). `Unknown command` on any OTA CLI pins `ota` / `ota_status` /
  `ota_ls` to that `firmware_version` (no refresh until `ver` changes).
  Long-running fleet re-checks due groups about every 60s
  while idle, and after each swim-lane batch (so apply-due units
  do not wait for the whole fleet to go quiet). fw/bl/ota once; name/gps/advert/acl
  audit GET weekly (cap 3 attempts), on Pull / `--group`, or when never stamped.
  Status/telemetry samples log bound-site GPS.
  One-shot `sample_loc_backfill` stamps current site loc onto older
  rows that lack it. Bench/unmapped stay blank. Voltage is status
  `battery_mv` only (telemetry voltage stored, unused). Voltage/temp When
  cells show ☀️/🌙 (sun above horizon = charging expected).
  **Fleet poll uses swim-lane round-robin:** each unit owns a FIFO deque
  (login, SET if profile due, then GET groups). Apply is spliced onto a
  busy GET lane when the stamp is out of sync. One dispatcher sends one radio command per
  unit per turn, then rotates to the least-recently-served ready lane. Timeout
  parks that unit's head job; other lanes keep sending. A `console:*` head
  stays on the radio until heard, exhausted, or Cancel (does not rotate).
  Tabs (`tab_id`) FIFO among `console:*`; cancel is per tab.
  `--attempts` is the
  per-command retry cap (scheduler-owned); logs show `N/max` not `N/1`.
  Auto poll/apply only: `--retry-delay` (default 60s) parks a unit after timeout
  before retry; `--miss-cooldown` (default 3600s) skips cadence re-seed after
  max attempts. Console and manual Refresh/Pull/Deploy are exempt; manual UI
  clears backoff and cooldown. `--full-sync` or `--only` bypass cooldown on startup.
  `--only` / `--skip` are the earliest node filter (dashboard + poll/apply).
  Successful GET_STATUS / GET_TELEMETRY / CLI log a one-line result as soon
  as they land (same beat as `login OK`). An apply field whose stamp
  already matches logs `field: skip (synced)` (no radio).
  Every poll session logins use book routing policy (default auto). GET/CLI/binary
  ride the learned path (`mesh_audit.path` = hop hashes, `direct`, or `flood`).
  Auto flood-discovers when cache is empty; timeout on cached path (3x)
  discards cache and re-floods. Locked path re-applies book `route:`.
  Neighbor discover wait (default 12s) is a background timer, not radio hold.
  Manual Refresh/Pull/Deploy replace that unit's remaining jobs except a
  queued console send (stays in front). A click mid-GET supersedes the
  in-flight result (does not pop the new head). Sqlite stamps incrementally per successful GET/SET. Apply GETs ACL when
  due to drop extras. A SET timeout parks and retries (including clock
  `time`). A SET CLI error or exhausted `--attempts` abort remaining SET
  jobs this pass (GET stays). Clock give-up still runs `push_advert`.
  Poll and apply password-login every MeshCore unit. Live RTC from login
  timestamp or ``clock`` CLI.
- Duty-cycle default is 50% (stock MeshCore). `nodes.yaml` `dutycycle`
  overrides. Fleet apply runs RF first after login: `fem_rxgain` on (default),
  `agc_reset_interval` 4, then book `rxgain` when set. Optional `powersaving`
  applies only when the book sets it. `fem_vfem` is
  gone (`radio.fem.vfem` CLI reverted 09-05).
  `rxgain` also
  needs `board: heltec-t096` (or `t096`). `board` is the only radio-family
  book key. Temporary off is firmware
  `try`, not apply. Missing CLI
  (`unsupported` / `unknown config`) stamps done. Seeder launch is 10%. `set dutycycle` needs MeshCore 1.15+;
  older 1.x uses `set af` (50% = af 1.0).
  After apply, if name/lat/lon SET this pass, fleet sends `advert` (flood).
  Onboard also SETs `path.hash.mode` 1 (2-byte), same as fleet apply,
  and always SETs admin (write-only; yaml is the value, not a skip),
  then stamps the private profile. A successful onboard is fleet-ready
  (no first mesh apply). Onboard always reboots (`set radio` is
  prefs-only until reboot) unless `--no-reboot`. `get radio` matching is
  not evidence the radio is live.
- Do not copy passwords or keypairs into this repo.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later.
- **EnvyOS package (native):** pinned `0.1.0` in `releases.next` (unpublished).

## Desk radios

BLE companion for `fleet` / `trust` / `cmd`. Multiple BLE tags: brief probe for
pubkey prefix, TTY `Pick companion [1-N]:`; headless needs `--ble ADDRESS`.
USB DUT for `onboard`.
Separate USB OTA repeater for `envybot seed` (see `docs/commands/seed.md`).
On connect (`post_connect`): zero-hop `NODE_DISCOVER_REQ` ping (10s listen);
lists nearby repeaters that answer (name from contacts, pubkey prefix, SNR).
Warns when none. Companion firmware v8+ (`CMD_SEND_CONTROL_DATA`); older
companions skip. Not re-run on BLE recover.

- **Fleet UI:** `./envybot fleet` serves `127.0.0.1:8787` by default.
  Main view is a **card dashboard** (batt / temp / traffic / err sparklines,
  fetch stage, health, book apply prefs). Header **Map** opens a modal with the pin map + compact
  sidebar list (first click fly/select, second opens detail). Open card is
  `?unit=<key>` (`replaceState`; reload restores). `?map=1` reopens the map.
  `--web-only` browses the book without a radio. Never expose secrets.
  **Sync** and **Full sync** always enqueue (even while that unit
  is polling) and run ahead of auto work until the click is done. Overrides
  `--skip` and `paused`. Sync is live GET (status/telemetry) then SET dirty
  fields. Full sync GETs all groups (audit reconciles), then SET dirty only.
  Per-node `full_sync_interval` (default 7d) schedules auto Full sync.
  **Console** (header or unit-row icon): tabbed modal, optional extra sessions to the
  same unit. Row icon focuses the first tab for that unit or opens one.
  Open is radio-free. CLI jumps that unit (login on first send if not
  authed, or if a login job is already queued). Login timeout clears
  cached auth. Failed login drops remaining console CLI. Timeout retries
  `--attempts` (default 10) on that unit, then parks
  (Retry beside the error; Continue if queued). Per-tab Cancel while
  sending (Retry only after the line has stopped, not during attempts).
  Busy send stages the next line. Hide
  keeps running. Dashboard card click opens detail. Map pin opens detail
  and flies. Sidebar: first click selects + flies; second opens detail over
  the map (map stays open; Esc / backdrop closes the card first). Per-tab up/down command recall (survives Clear history).
  Clipboard copies the transcript.
  Unread `*` on a tab (header icon too when hidden).
  `localStorage` + hello survive refresh / fleet restart.
  Audit `source=console`. Tracked CLI replies stamp last-seen.
  Deploy re-SETs profile (including passwords). CLI `--full-sync` is Pull plus
  Deploy. Auto-apply queues when `apply_is_due` even if the fleet is mid-sync.
  **Pause** (detail checkbox) writes `paused: true` and drops the unit from
  auto poll/apply on the next job boundary (in-flight exchange finishes).
  Map sidebar and dashboard cards fade paused rows. Dashboard **on-air**
  badge (pulsing radio icon + teal border) marks the unit holding the
  companion radio (`poll.unit` via session SSE); queued units stay busy
  purple only.
  **Route** (detail dropdown) writes `routing: auto|path|direct|flood`. Locked
  **path** saves hops in book `route:`. Auto shows companion cache (direct,
  hops, or flood). Flood shows a danger badge on list cards.
  Units carry `health` (worst-of component checks) and interval traffic
  deltas. Hello snapshot includes compact 72h `sparks` (battery V, temp,
  in/h, unreadable %) per unit; SSE status/telemetry samples extend them
  live.   Detail sparklines prefer merged `polls` from `/api/polls/{unit}`
  (`poll_snapshots`); ACL poll log only (no neighbor count history).
  **Audit** table from `/api/audit/{unit}` (`mesh_audit`, newest first,
  **Load older** via `before_id`; live `audit` SSE rows while card open).
  **Heard**
  list is live snapshot from latest neighbors GET (book + off-book by pubkey).
  Rows show approximate miles from the unit: book display loc for fleet
  peers, companion advert GPS (`adv_lat`/`adv_lon`) for community nodes.
  Each GET persists only that
  group (no replay of earlier status). SSE `unit` events carry
  `{ source, sample }` so the open card updates live via `state.js`.
  List meta line shows a headline mark: Paused, Healthy, Unreachable,
  Needs attention. Status/telemetry history times show ☀️ or 🌙
  (clear-sky sun up = charging expected). Voltage sparkline and health
  Power use status `battery_mv`; telemetry table is temp only. Poll **When**
  cells: ☀️/🌙 + hover Open-Meteo ambient. `./envybot weather backfill` once.
  sparklines share a 72h wall-clock axis; Sun is elevation vs horizon.
  Detail card edits book `alias` and `notes` (blur saves).
  Map pins color by last-heard age (green→red over 24h); labels include
  `(3h)` and tick from `last_heard` + store clock.
  In-flight cards show the current job stage (Logging in, Fetching ACL, …).
  **OTA (stage-then-roll):** auto poll (24h) and Pull store `ota status` +
  delayed `ota ls` into sqlite `ota_state`. Detail **Firmware** is identity (version, hw,
  target, full body hash + image K, bootloader). **OTA** is session
  (serving, keys, local, heard `[yours]`). List badges: `staged`,
  `sees update`, `downloading`. **Stage** per heard row (`ota pull <#> flash`);
  **Install** when local is ready (confirm; reboot). Ops:
  `initiatives/envybot-monitor-ota.md`.
  `unreachable` is only after login/GET give up (`--attempts`) or a hard
  fail. A login or SET timeout parks the unit and keeps the stage.
- Long BLE apply can drop the companion link; fleet reconnects transport,
  re-syncs clock/contacts, and clears cached logins before retrying.

Last updated: 2026-09-19 (favorite-route next; still path/flood/direct today)
