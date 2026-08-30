# Changelog

User-facing notes for the EnvyBot fleet CLI.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match git tags (`v0.1.0`, …).

## [Unreleased]

## [0.2.0] - 2026-08-29

### Added

- `./envybot cmd` — remote MeshCore CLI on one unit (one-shot + REPL).
- Selector resolve: unit key/id, pubkey prefix, name, site slug.
- `docs/commands/cmd.md`. jsonl audit `event: cmd` with credential redaction.

### Changed

- `add_companion_args()` shared by `monitor` and `cmd`.

## [0.1.0] - 2026-08-29

### Added

- `./envybot monitor` (was `pull`) — companion LoRa poll into a private `nodes.yaml` book.
- `./envybot onboard` — USB repeater text-CLI onboard (idempotent).
- Book resolve: `--book`, `ENVYBOT_HOME`, or cwd with `nodes.yaml`.
- Per-command manuals under `docs/commands/`.
