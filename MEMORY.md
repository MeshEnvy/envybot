# EnvyBot

Public fleet CLI. Book (identity, creds) is private and outside this repo.
Observed last-seen lives in the book's SQLite, not in YAML.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.1.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `fleet`, `trust`, `cmd`, `onboard`, `seed` |
| Book | `--book` / `ENVYBOT_HOME` / cwd with `nodes.yaml` (+ `keys.yaml`, `channels.yaml`) |

## Layout

| Path | Role |
|---|---|
| `./envybot` | Root shim (reexec `.venv`) |
| `src/envybot/cli.py` | Dispatcher; `COMMANDS` registry |
| `src/envybot/book.py` | Resolve book dir; never write secrets here |
| `src/envybot/keys_doc.py` | `keys.yaml` people + `trust` role resolve |
| `src/envybot/channels_doc.py` | `channels.yaml` catalog + companion slot planner |
| `src/envybot/nodes_doc.py` | Desired `nodes.yaml` load/write/migrate |
| `src/envybot/history.py` | `data/fleet/history.sqlite` (+ `mesh_audit` per send) |
| `src/envybot/health.py` | Per-node health checks (snapshot + UI grade) |
| `src/envybot/position.py` | Book GPS from `sites.yaml` (`node:` bind + `loc`) |
| `src/envybot/sun.py` | Clear-sky elev: ☀️/🌙 + 72h elevation sparkline |
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
| `src/envybot/web/` | Fleet UI (`:8787`); header **Console** (tabbed priority CLI) |

## Contract

- Greenfield: no `monitor` alias, no `polls.jsonl`, no last-seen in YAML.
- `nodes.yaml` is SoT for **desired** identity. Cite sqlite `last_seen` for
  reachability / fw / battery / uptime / temperature.
- GPS lives only on `sites.yaml` (`loc` + `node: me####`). Nodes have no
  `site` / `lat` / `lon`. Apply SETs `0,0` unless `public: true`. Never
  GET device coords into YAML.
- `fleet` / `trust` / `cmd` skip `firmware_platform: meshtastic` even
  when leftover MeshCore pubkey/admin exist. No UI, poll, apply, or
  edits. Blank platform = meshcore.
- `paused: true` stays in the UI. Fleet skips auto poll/apply. Refresh,
  Pull, and Push still hit the radio. Trust/cmd ignore the flag.
- `decommissioned:` (epoch) rows stay in `nodes.yaml` for the number
  but envybot ignores them: no UI, poll, apply, trust, cmd, or onboard.
- `fleet` and `trust` poll every pollable MeshCore unit, including
  bag/bench (no site bind). `--deployed-only` narrows fleet to
  site-bound units only. Contact name is the site `name` (e.g. Ophir),
  else `unit_id`. Stale advert names are removed and re-added. `trust ben`
  records the live tag in `keys.yaml` and password-auths onto MeshCore
  units (`--unit` scopes that pass; contacts still import the full book).
  On units already profile-synced, trust stamps the new hash so fleet
  does not re-push. Guest grants do not update remotes; fleet `setperm`s.
  `admin1_*` is Meshtastic, not MC ACL.
- `channels.yaml` (channel-first) lists group name + 16-byte PSK + who
  gets it (`everyone` or person slugs). `trust` add/updates granted
  channels on the companion; extras on the tag are left alone. The stock
  Public slot is never applied (`public` yaml is ignored). Missing file
  skips channel work.
- Mask name is `Repeater`. Display name is site `name` when bound, else book
  `alias`, else `unit_id`. Alias is UI/selector only (not pushed to radio).
- `public: true` SETs site name (or `unit_id` when bag/bench) + site GPS.
- Passwords are unique and strong per unit. Apply/onboard roll blank, weak
  (`m35h3nvy`, placeholders, short), or colliding guests. Admin is never
  invented by apply. Apply SETs book admin (`password`) after ACL/login.
- Apply due = `profile_id` vs last ok sqlite stamp, or weak guest assign.
  Per-field stamps in sqlite `applies` (name, lat, lon, …, ota_autofetch).
  `ota config autofetch` `Unknown command` stamps the field done (no CLI).
  A SET timeout still retries. Onboard stamps those after USB SET + ACL
  (private only). Legacy
  `profile` ok row still counts as fully synced. `--force` is Pull plus Push.
  UI ``due`` is that stamp (apply needed). ``leak`` is a later pull that
  still shows advert or flood advert on. Name or GPS is not an advert.
  A last-seen interval older than the apply stamp is ignored. Poll default: status/telemetry every 1h
  (`--min-interval`); neighbors
  stay 24h (`discover.neighbors` + wait + GET; UI drops rows older
  than 7d). Long-running fleet re-checks due groups about every 60s
  while idle, and after each swim-lane batch (so apply-due units
  do not wait for the whole fleet to go quiet). fw/bl/ota once; name/gps/advert/acl audit-only (Pull /
  `--group` / `--force`). Status/telemetry samples log bound-site GPS.
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
  Successful GET_STATUS / GET_TELEMETRY / CLI log a one-line result as soon
  as they land (same beat as `login OK`).
  Every mesh send resets companion out_path to flood first (`mesh_audit.path`
  should read `flood`; hop strings indicate a firmware leak). Neighbor discover wait (default 12s) is a background timer, not radio hold.
  Manual Refresh/Pull/Push replace that unit's remaining jobs except a
  queued console send (stays in front). A click mid-GET supersedes the
  in-flight result (does not pop the new head). Sqlite stamps incrementally per successful GET/SET. Apply GETs ACL when
  due to drop extras. A SET timeout parks and retries. A SET CLI error or
  exhausted `--attempts` abort remaining SET jobs this pass (GET stays).
  Poll and apply password-login every MeshCore unit. Live RTC from login
  timestamp or ``clock`` CLI.
- Duty-cycle default is 100% (`nodes.yaml` `dutycycle` overrides).
  `set dutycycle` needs MeshCore 1.15+; older 1.x uses `set af`.
  Onboard also SETs `path.hash.mode` 1 (2-byte), same as fleet apply,
  then stamps the private profile. A successful onboard is fleet-ready
  (no first mesh apply). Onboard always reboots (`set radio` is
  prefs-only until reboot) unless `--no-reboot`. `get radio` matching is
  not evidence the radio is live.
- Do not copy passwords or keypairs into this repo.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later.
- **EnvyOS package (native):** pinned `0.1.0` in `releases.next` (unpublished).

## Desk radios

BLE companion for `fleet` / `trust` / `cmd`. USB DUT for `onboard`.
Separate USB OTA repeater for `envybot seed` (see `docs/commands/seed.md`).

- **Fleet UI:** `./envybot fleet` serves `127.0.0.1:8787` by default.
  Open card is `?unit=<key>` (`replaceState`; reload restores).
  `--web-only` browses the book without a radio. Never expose secrets.
  **Refresh**, **Pull**, and **Push** always enqueue (even while that unit
  is polling) and run ahead of auto work until the click is done. Overrides
  `--skip` and `paused`. Refresh is live GET only; Pull adds sticky GET;
  Push is SET-only force.   **Console** (header or unit-row icon): tabbed modal, optional extra sessions to the
  same unit. Row icon focuses the first tab for that unit or opens one.
  Open is radio-free. CLI jumps that unit (login on first send).
  Timeout retries `--attempts` (default 10) on that unit, then parks
  (Retry beside the error; Continue if queued). Per-tab Cancel while
  sending (Retry only after the line has stopped, not during attempts).
  Busy send stages the next line. Hide
  keeps running. List or map select closes the console and opens the card. Per-tab up/down command recall (survives Clear history).
  Clipboard copies the transcript.
  Unread `*` on a tab (header icon too when hidden).
  `localStorage` + hello survive refresh / fleet restart.
  Audit `source=console`. Tracked CLI replies stamp last-seen.
  Push force-SETs profile (including passwords). CLI `--force` is Pull plus
  Push. Auto-apply queues when `apply_is_due` even if the fleet is mid-sync.
  **Pause** (detail checkbox) writes `paused: true` and drops the unit from
  auto poll/apply on the next job boundary (in-flight exchange finishes).
  Sidebar fades paused rows and shows a paused badge.
  Units carry `health` (worst-of component checks) and interval traffic
  deltas. Detail sparklines use native sqlite series; poll history is four
  always-open sections (status, telemetry, neighbors, ACL), 10 rows each
  with Load more +10, from `/api/polls/{unit}` (`source_histories`).
  Each GET persists only that
  group (no replay of earlier status). SSE `unit` events carry
  `{ source, sample }` so the open card updates live via `state.js`.
  List meta line shows a headline mark: Paused, Healthy, Unreachable,
  Needs attention. Status/telemetry history times show ☀️ or 🌙
  (clear-sky sun up = charging expected). Voltage sparkline and health
  Power use status `battery_mv`; telemetry table is temp only. Detail
  sparklines share a 72h wall-clock axis; Sun is elevation vs horizon.
  Detail card edits book `alias` and `notes` (blur saves).
  Map pins color by last-heard age (green→red over 24h); labels include
  `(3h)` and tick from `last_heard` + store clock.
  In-flight cards show the current job stage (Logging in, Fetching ACL, …).
  **OTA (stage-then-roll):** Refresh polls `ota status` + delayed `ota ls`
  into sqlite `ota_state`. Detail **Firmware** is identity (version, hw,
  target, full body hash + image K, bootloader). **OTA** is session
  (serving, keys, local, heard `[yours]`). List badges: `staged`,
  `sees update`, `downloading`. **Stage** per heard row (`ota pull <#> flash`);
  **Install** when local is ready (confirm; reboot). Ops:
  `initiatives/envybot-monitor-ota.md`.
  `unreachable` is only after login/GET give up (`--attempts`) or a hard
  fail. A login or SET timeout parks the unit and keeps the stage.
- Long BLE apply can drop the companion link; fleet reconnects transport,
  re-syncs clock/contacts, and clears cached logins before retrying.

Last updated: 2026-09-02 (leak is last-pull advert interval only)
