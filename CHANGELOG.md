# Changelog

User-facing notes for the EnvyBot fleet CLI.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions match git tags (`v0.1.0`, …). First ship is still pending.

## [Unreleased]

### Changed

- Apply stamps each SET field in sqlite (`applies.name`, `.lat`, …).
  Synced fields skip on the next run; `--force` clears stamps and re-SETs.
  Legacy `applies.profile` ok rows still count as fully synced.
  SET fields use `--attempts` (login is reachability). A SET failure
  still aborts remaining fields for that unit this pass.
  Apply-only password-logins when poll did not run.
- Fleet and `cmd` always password-login; skip-login removed (remote
  repeaters need the login handshake even when the book ACL lists admin).
- Log companion route (`path: abcd …` or `path: flood`) before each
  mesh send attempt (CLI, login, binary).
- Fleet retry rounds reprint poll/apply need vs skip so later passes
  show what is still due and what already stamped.
- Fleet poll cadence: default GET is status/telemetry/neighbors only.
  Name/GPS/advert/ACL are audit-only (`--force` / `--group`). Apply no
  longer triggers on heard leak/mismatch.
- `trust` stamps sqlite profile hash after admin ACL bootstrap on units
  that were already synced, so fleet does not re-push policy.

### Added

- Fleet list cards show a `leak` / `mismatch` badge next to freshness
  when heard identity drifts from book policy.
- Decommissioned `nodes.yaml` rows are omitted from fleet UI, poll,
  apply, trust, cmd, and onboard. The stamp stays in the book.
- Fleet reconnects the BLE/USB companion after an unexpected transport
  drop (clock sync, contacts, fleet favorites, cleared login cache).
- Admin `password` SET treats MeshCore's `password now: …` echo as success.
  privacy-mask apply unless `public: true`.
- `./envybot trust` imports site name + resolved loc + pubkey as companion
  contacts (bag/bench included; name is site `name`, else `unit_id`).
  Stale last-heard advert names are removed and re-added. Optional
  `--export`. Applies `channels.yaml` group-channel grants (add/update
  only). `trust ben` / `bill:guest` writes `keys.yaml` and book
  `trust` roles. Admin tags password-login the fleet (`--unit` to
  scope a sealed box).
- Book `channels.yaml` defines group channels (name + PSK + people).
  Book `keys.yaml` maps people to companion pubkeys. Fleet apply and
  USB onboard reconcile MeshCore ACL from `trust.admin` (perm 3) and
  `trust.guest` (perm 1), and drop unknown heard keys.
- `data/fleet/history.sqlite` is the observed store (last-seen, graphs,
  apply log, `cmd` audit).

### Removed

- `./envybot monitor` (no alias).
- `data/fleet/polls.jsonl` (imported once, then deleted).

### Changed

- Book GPS is `sites.yaml` only (`loc` + `node: me####`). `nodes.yaml` no
  longer has `site`, `lat`, or `lon`. Fleet bind writes the site.
- Apply `profile_id` is a `v1:` hash of the desired SET payload (name/gps/
  adverts, guest+admin tokens, identity pubkey, path.hash, dutycycle, ACL).
  Editing a hashed field in `nodes.yaml` makes that unit due on the next
  fleet start. Apply now SETs book admin after login. Identity secret
  rotation stays `roll`.
- Fleet and `cmd` skip password login when the companion is already on
  that unit's book ACL. Clock comes from the `clock` CLI. Remove the
  companion from the ACL to force login if the radio drifted.
- Guest/admin doctrine: every password is unique and strong. Privacy apply
  and onboard refuse the shared `m35h3nvy` default (and other weak/colliding
  values) and generate a new 14-char password into the book.
- `nodes.yaml` is desired identity only. Poll does not write last-seen.
- Default apply SETs `Repeater`, `0,0`, adverts off, guest password, ACL
  allowlist. Book GPS is SET only when `public: true`.
- Onboard omits `public`, does not store `Repeater` as the book name, and
  uses device GPS `0,0` (not the Manila placeholder).
- Onboard waits for an antenna confirm before neighbor discover (Enter
  to proceed, `s` to skip). No TTY skips discover.

### Fixed

- USB onboard accepts MeshCore `get acl` (firmware prints `ACL:` and
  skips the `->` reply). Book row is written before ACL so a later CLI
  miss no longer drops identity and passwords.
- USB onboard SETs `path.hash.mode` 1 (2-byte), matching fleet apply.
  Firmware default is 0 (1-byte).

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
