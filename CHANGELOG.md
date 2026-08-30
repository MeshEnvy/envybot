# Changelog

User-facing notes for the EnvyBot fleet CLI.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match git tags (`v0.1.0`, …).

## [Unreleased]

### Added

- Per-command manuals under `docs/commands/`. README command table.

### Changed

- Book resolve is `--book`, `ENVYBOT_HOME`, or cwd with `nodes.yaml`. No implicit sibling-repo default.
- Renamed `pull` to `monitor`. No `pull` alias. jsonl `event` is `monitor`.

## [v0.1.0] - 2026-08-29

### Added

- `./envybot pull` — companion LoRa poll into a private `nodes.yaml` book.
- `./envybot onboard` — USB repeater text-CLI onboard (idempotent).
- Book resolve: `--book`, `ENVYBOT_HOME`, or cwd with `nodes.yaml`.
