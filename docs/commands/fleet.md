# `envybot fleet`

Localhost fleet manager. Serves the map UI, GETs telemetry into
`data/fleet/history.sqlite`, and SETs radio policy.

```
./envybot [--book DIR] fleet [flags]
```

Default: UI at `http://127.0.0.1:8787/` plus live GET (status/telemetry/neighbors),
inventory gaps (fw/bl), and apply when the profile hash misses.

## Stores

| Store | Role |
|-------|------|
| `nodes.yaml` | Desired identity + `trust.admin` / `trust.guest`. Poll does not write. No GPS. |
| `keys.yaml` | Person → MeshCore companion pubkeys. Apply resolves names to ACL. |
| `sites.yaml` | Places (`loc`) and the 1:1 `node:` bind. Fleet writes bind only. |
| `data/fleet/history.sqlite` | Observed last-seen, telemetry, apply log, `cmd` audit |

First run imports leftover `polls.jsonl` (then deletes it) and YAML
`*_pulled_at` blobs, then strips observed keys from `nodes.yaml`.

## Poll cadence

| Mode | Groups | When |
|------|--------|------|
| periodic | `status`, `telemetry`, `neighbors` | `--min-interval` (default 24h) |
| inventory | `firmware`, `bootloader` | until sqlite stamp exists |
| audit | `name`, `lat`, `lon`, `advert`, `flood_advert`, `acl` | `--force` or `--group` only |

Default runs never GET sticky identity fields. Use `--force --poll-only` to
refresh leak/mismatch in the UI without SET.

## Apply

Nodes without `public: true` get the privacy mask: name `Repeater`, lat/lon
`0,0`, adverts off, a unique strong guest password. ACL is resolved from
`keys.yaml` + `trust.admin` (perm 3) / `trust.guest` (perm 1). Heard keys
that are not in that list are dropped (`setperm 0`). `admin1_*` is
Meshtastic and is not applied. Blank, weak, or colliding guests are
rolled and written back to the book. `public: true` pushes book name +
resolved GPS.

Always also SETs `path.hash.mode` (default 1 = 2-byte), `dutycycle`
(default 100), a strong book admin via `password`, and clock if unset
or behind. Password-login every unit before GET or SET (login establishes
the repeater session and refreshes mesh paths). Live clock comes from
the login timestamp or `clock` CLI afterward.

Apply is due when any SET field stamp misses the book desired value
(stored in sqlite `applies` per field: name, lat, lon, advert, flood,
guest, admin, path_hash, dutycycle, acl, identity). A legacy ok
`applies.profile` row still means fully synced. `--force` clears field
stamps and re-SETs everything. A private node still needs a guest password
assign. Heard name/GPS/adverts do **not** trigger apply. Edit a hashed field in `nodes.yaml` (or run `trust`) and restart fleet.

When apply runs, GET ACL once to drop keys not in the book allowlist.
The first due SET field uses at most two mesh attempts (direct + flood).
If it gets no response, apply aborts for that unit (no lat/lon/guest/…).

Hashed: public/name/gps/adverts, guest + admin (tokens), identity pubkey,
path.hash, dutycycle, resolved ACL (pubkey + perm). Not hashed / not pushed here:
identity secret (`roll`), radio preset (onboard), clock.

`trust ben` (admin) updates the book and radio ACL, then stamps the new hash
on units that were already profile-synced so fleet does not re-push.

## Flags

Same companion flags as `cmd` (`--ble`, `--serial`, `--tcp`, `--timeout`,
`--attempts`, …).

| Flag | Meaning |
|------|---------|
| `--web-only` | Browse the book. No radio. |
| `--no-web` | Headless poll/apply |
| `--unit KEY` | One unit (repeatable) |
| `--force` | Re-GET every group (incl. audit); re-SET profile |
| `--live` | Periodic GET only (status/telemetry/neighbors) |
| `--poll-only` | GET only |
| `--apply-only` | SET only |
| `--all-units` | Include bag/bench (no site `node:` bind) |

## UI

Map pins use **book** position (`sites.yaml` loc for the bound unit), never
device `0,0`. Detail shows book name + site name, a `public` toggle, drift
(`leak` vs `mismatch`), and battery history.
