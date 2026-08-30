# EnvyBot

Public fleet CLI. Book (identity, creds, last-seen) is private and outside this repo.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.2.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `monitor`, `cmd`, `onboard` |
| Book | `--book` / `ENVYBOT_HOME` / cwd with `nodes.yaml` |

## Layout

| Path | Role |
|---|---|
| `./envybot` | Root shim (reexec `.venv`) |
| `src/envybot/cli.py` | Dispatcher; `COMMANDS` registry |
| `src/envybot/book.py` | Resolve book dir; never write secrets here |
| `src/envybot/position.py` | Book GPS (node lat/lon, else site loc). Not device. |
| `src/envybot/commands/monitor.py` | Companion LoRa poll → `nodes.yaml` |
| `src/envybot/commands/cmd.py` | Remote MeshCore CLI over companion |
| `src/envybot/selector.py` | Book selector resolution for `cmd` |
| `docs/commands/` | Per-command manuals |
| `src/envybot/commands/onboard.py` | USB repeater text CLI onboard |
| `src/envybot/web/` | Monitor localhost UI (`snapshot`, `hub`, `server`, ESM static) |

## Contract

- Greenfield: no `./fleet` shim, no push stub, no dual config.
- `nodes.yaml` is SoT for last-seen / fw / battery. Cite `*_pulled_at`.
- GPS is book-canonical (`lat`/`lon`, else `sites.yaml` loc). `monitor` SETs
  the radio. Never GET device coords into the book.
- Duty-cycle policy is 100%. `set dutycycle` needs MeshCore 1.15+; older
  1.x uses `set af 0` and still stamps `dutycycle`.
- Do not copy passwords or keypairs into this repo.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later. `mcmt-gateway` stays the burn bridge.
- **EnvyOS package (native):** pinned `0.1.0` in `releases.next`. Artifact is `envybot-<ver>-py3-none-any.whl`. Book stays private.

## Desk radios

BLE companion for `monitor`. USB DUT for `onboard`. Separate USB OTA repeater for `motatool serve`. No combined hub firmware.

- **Monitor UI:** `./envybot monitor` serves `127.0.0.1:8787` by default (map +
  live SSE). `--web-only` to browse the book without a radio. `--no-web` for
  headless poll. Never expose secrets to the browser.

Last updated: 2026-08-30
