# EnvyBot

Public fleet CLI. Book (identity, creds) is private and outside this repo.
Observed last-seen lives in the book's SQLite, not in YAML.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.1.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `fleet`, `trust`, `cmd`, `onboard` |
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
| `src/envybot/history.py` | `data/fleet/history.sqlite` |
| `src/envybot/health.py` | Per-node health checks (snapshot + UI grade) |
| `src/envybot/position.py` | Book GPS from `sites.yaml` (`node:` bind + `loc`) |
| `src/envybot/radio.py` | Companion session, login, CLI/binary |
| `src/envybot/poll.py` | GET cadence (live / inventory / audit) → sqlite |
| `src/envybot/apply.py` | SET mask unless `public: true`; `v1:` profile hash |
| `src/envybot/passwords.py` | Password strength + uniqueness (no shared defaults) |
| `src/envybot/commands/fleet.py` | Localhost manager |
| `src/envybot/commands/trust.py` | Contacts + channels + keys.yaml / ACL login |
| `src/envybot/commands/cmd.py` | Remote MeshCore CLI |
| `src/envybot/commands/onboard.py` | USB repeater text CLI onboard (`path.hash.mode` 1, `get acl` is `ACL:` dump) |
| `src/envybot/web/` | Fleet UI (`:8787`) |

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
- `decommissioned:` (epoch) rows stay in `nodes.yaml` for the number
  but envybot ignores them: no UI, poll, apply, trust, cmd, or onboard.
- `trust` imports every pollable MeshCore unit, including bag/bench
  (no site bind). Contact name is the site `name` (e.g. Ophir), else
  `unit_id`. Stale advert names are removed and re-added. `trust ben`
  records the live tag in `keys.yaml` and password-auths onto MeshCore
  units (`--unit` scopes that pass; contacts still import the full book).
  On units already profile-synced, trust stamps the new hash so fleet
  does not re-push. Guest grants do not update remotes; fleet `setperm`s.
  `admin1_*` is Meshtastic, not MC ACL.
- `channels.yaml` (channel-first) lists group name + 16-byte PSK + who
  gets it (`everyone` or person slugs). `trust` add/updates granted
  channels on the companion; extras on the tag are left alone. Missing
  file skips channel work.
- Mask name is `Repeater`. Book name stays in YAML.
- Passwords are unique and strong per unit. Apply/onboard roll blank, weak
  (`m35h3nvy`, placeholders, short), or colliding guests. Admin is never
  invented by apply. Apply SETs book admin (`password`) after ACL/login.
- Apply due = `profile_id` vs last ok sqlite stamp, or weak guest assign.
  Per-field stamps in sqlite `applies` (name, lat, lon, …). Legacy
  `profile` ok row still counts as fully synced. `--force` clears stamps.
  UI leak / mismatch is the inverse of that stamp (private due / public
  due), not heard last-seen identity. Poll default: status/telemetry/neighbors
  on interval; fw/bl once; name/gps/advert/acl audit-only (`--force`).
  Apply GETs ACL when due to drop extras. SET fields use the full retry
  budget (login is the reachability check). Any due field failure aborts
  the rest for that unit this pass. Poll and apply password-login every
  MeshCore unit. Live RTC
  from login timestamp or ``clock`` CLI.
- Duty-cycle default is 100% (`nodes.yaml` `dutycycle` overrides).
  `set dutycycle` needs MeshCore 1.15+; older 1.x uses `set af`.
  Onboard also SETs `path.hash.mode` 1 (2-byte), same as fleet apply.
  Onboard always reboots (`set radio` is prefs-only until reboot) unless
  `--no-reboot`. `get radio` matching is not evidence the radio is live.
- Do not copy passwords or keypairs into this repo.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later.
- **EnvyOS package (native):** pinned `0.1.0` in `releases.next` (unpublished).

## Desk radios

BLE companion for `fleet` / `trust` / `cmd`. USB DUT for `onboard`.
Separate USB OTA repeater for `motatool serve`.

- **Fleet UI:** `./envybot fleet` serves `127.0.0.1:8787` by default.
  `--web-only` browses the book without a radio. Never expose secrets.
  Units carry `health` (worst-of component checks) and interval traffic
  deltas; detail sparklines use `/api/history/{unit}?metric=`.
- Long BLE apply can drop the companion link; fleet reconnects transport,
  re-syncs clock/contacts, and clears cached logins before retrying.

Last updated: 2026-09-01
