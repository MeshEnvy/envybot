# Changelog

User-facing notes for the EnvyBot fleet CLI.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match git tags (`v0.1.0`, …). First ship is still pending.

## [Unreleased]

### Added

- `./envybot fleet` replaces `monitor`: localhost manager, sqlite history,
  privacy-mask apply unless `public: true`.
- `./envybot trust` imports book name + resolved loc + pubkey as companion
  contacts. Optional `--export`.
- `data/fleet/history.sqlite` is the observed store (last-seen, graphs,
  apply log, `cmd` audit).

### Removed

- `./envybot monitor` (no alias).
- `data/fleet/polls.jsonl` (imported once, then deleted).

### Changed

- `nodes.yaml` is desired identity only. Poll does not write last-seen.
- Default apply SETs `Repeater`, `0,0`, adverts off, guest password, ACL
  allowlist. Book GPS is SET only when `public: true`.
- Onboard omits `public`, does not store `Repeater` as the book name, and
  uses device GPS `0,0` (not the Manila placeholder).

## [0.1.0] - 2026-08-29

### Added

- `./envybot monitor` (was `pull`) — companion LoRa poll into a private `nodes.yaml` book.
- `./envybot onboard` — USB repeater text-CLI onboard (idempotent).
- `./envybot cmd` — remote MeshCore CLI on one unit (one-shot + REPL).
- Selector resolve: unit key/id, pubkey prefix, name, site slug.
- Book resolve: `--book`, `ENVYBOT_HOME`, or cwd with `nodes.yaml`.
- Per-command manuals under `docs/commands/`.
- **Monitor web UI** — default local map at `http://127.0.0.1:8787/` (ESM +
  MapLibre, SSE live updates). `--no-web`, `--web-only`, `--bind`, `--port`,
  `--open`. Sanitized snapshot only (no passwords or secret keys in the browser).

### Changed

- `add_companion_args()` shared by `monitor` and `cmd`.
- Monitor UI labels a bound unit by its **site name** (map pin, list, detail,
  neighbors). Bag/bench units still show unit id.
- `set dutycycle` is MeshCore 1.15+. On older firmware (e.g. `v1.14.1`),
  monitor and onboard use `set af 0` for the same 100% policy and stamp
  `dutycycle: 100` so the group does not stay due forever.
- Book GPS is SET to the radio. Device coords are not pulled into the book.

### Fixed

- Hatch wheel no longer `force-include`s `web/static` (duplicate `index.html` on `uv build`).
