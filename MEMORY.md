# EnvyBot

Public fleet CLI. Book (identity, creds, last-seen) lives in private `peaky-nevada/`.

| | |
|---|---|
| Repo | [MeshEnvy/envybot](https://github.com/MeshEnvy/envybot) |
| Version | 0.1.0 |
| Tooling | `uv` + `pyproject.toml` |
| Commands | `pull`, `onboard` |
| Book | `--book` / `ENVYBOT_HOME` / sibling `peaky-nevada` |

## Layout

| Path | Role |
|---|---|
| `./envybot` | Root shim (reexec `.venv`) |
| `src/envybot/cli.py` | Dispatcher; `COMMANDS` registry |
| `src/envybot/book.py` | Resolve book dir; never write secrets here |
| `src/envybot/commands/pull.py` | Companion LoRa poll → `nodes.yaml` |
| `src/envybot/commands/onboard.py` | USB repeater text CLI onboard |

## Contract

- Greenfield: no `./fleet` shim, no push stub, no dual config.
- `nodes.yaml` is SoT for last-seen / fw / battery. Cite `*_pulled_at`.
- Do not copy passwords or keypairs into this repo or `ops/`.
- Not mesh-api. Not the sidecar mux (`mesh-sidecar-daemon`).
- Daemon (`envybot serve`, N-radio mux) is later. `mcmt-gateway` stays the burn bridge.

## Desk radios

BLE companion for `pull`. USB DUT for `onboard`. Separate USB OTA repeater for `motatool serve`. No combined hub firmware.

Last updated: 2026-08-29
