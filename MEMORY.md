# EnvyBot

Public fleet CLI. Book (identity, creds) is private and outside this repo.
Observed last-seen lives in the book's SQLite, not in YAML.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.1.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `fleet`, `trust`, `cmd`, `onboard` |
| Book | `--book` / `ENVYBOT_HOME` / cwd with `nodes.yaml` |

## Layout

| Path | Role |
|---|---|
| `./envybot` | Root shim (reexec `.venv`) |
| `src/envybot/cli.py` | Dispatcher; `COMMANDS` registry |
| `src/envybot/book.py` | Resolve book dir; never write secrets here |
| `src/envybot/nodes_doc.py` | Desired `nodes.yaml` load/write/migrate |
| `src/envybot/history.py` | `data/fleet/history.sqlite` |
| `src/envybot/position.py` | Book GPS (node lat/lon, else site loc) |
| `src/envybot/radio.py` | Companion session, login, CLI/binary |
| `src/envybot/poll.py` | GET cadence → sqlite |
| `src/envybot/apply.py` | SET mask unless `public: true`; `v1:` profile hash |
| `src/envybot/passwords.py` | Password strength + uniqueness (no shared defaults) |
| `src/envybot/commands/fleet.py` | Localhost manager |
| `src/envybot/commands/trust.py` | Companion contact import |
| `src/envybot/commands/cmd.py` | Remote MeshCore CLI |
| `src/envybot/commands/onboard.py` | USB repeater text CLI onboard |
| `src/envybot/web/` | Fleet UI (`:8787`) |

## Contract

- Greenfield: no `monitor` alias, no `polls.jsonl`, no last-seen in YAML.
- `nodes.yaml` is SoT for **desired** identity. Cite sqlite `last_seen` for
  reachability / fw / battery.
- GPS is book-canonical (`lat`/`lon` override, else `sites.yaml` loc).
  Apply SETs `0,0` unless `public: true`. Never GET device coords into YAML.
- Mask name is `Repeater`. Book name stays in YAML.
- Passwords are unique and strong per unit. Apply/onboard roll blank, weak
  (`m35h3nvy`, placeholders, short), or colliding guests. Admin is never
  invented by apply. Apply SETs book admin (`password`) after ACL/login.
- Apply due = `profile_id` (`v1:` hash of desired SET payload) vs last
  ok apply stamp. Payload: name/gps/adverts, guest+admin tokens, identity
  pubkey, path.hash, dutycycle, ACL. Identity secret is `roll`, not apply.
- Skip password login when the companion is on that unit's book ACL.
  Live RTC is `clock` CLI (or login timestamp). Out of sync: drop the
  companion from the ACL so the next run logs in.
- Duty-cycle default is 100% (`nodes.yaml` `dutycycle` overrides).
  `set dutycycle` needs MeshCore 1.15+; older 1.x uses `set af`.
- Do not copy passwords or keypairs into this repo.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later.
- **EnvyOS package (native):** pinned `0.1.0` in `releases.next` (unpublished).

## Desk radios

BLE companion for `fleet` / `trust` / `cmd`. USB DUT for `onboard`.
Separate USB OTA repeater for `motatool serve`.

- **Fleet UI:** `./envybot fleet` serves `127.0.0.1:8787` by default.
  `--web-only` browses the book without a radio. Never expose secrets.

Last updated: 2026-08-30
