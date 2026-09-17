# EnvyBot docs

Command manuals and operator notes. Agents: read [`.cursor/skills/fleet/SKILL.md`](../.cursor/skills/fleet/SKILL.md) before answering fleet questions — do not grep the codebase for basics covered there.

## Command manuals

| Command | Manual |
|---------|--------|
| `fleet` | [commands/fleet.md](commands/fleet.md) |
| `trust` | [commands/trust.md](commands/trust.md) |
| `cmd` | [commands/cmd.md](commands/cmd.md) |
| `onboard` | [commands/onboard.md](commands/onboard.md) |
| `seed` | [commands/seed.md](commands/seed.md) |
| `weather` | [commands/weather.md](commands/weather.md) |

## Book layout

The **book** is a private directory (`ENVYBOT_HOME` or `--book`). Never commit it.

| File | Role |
|------|------|
| `nodes.yaml` | Desired identity, passwords, apply prefs, optional `routing`, `paused` |
| `sites.yaml` | Site `loc` and 1:1 `node:` bind (apply GPS for deployed units) |
| `keys.yaml` | Person → companion pubkeys; ACL name resolution |
| `channels.yaml` | Group channels for `trust` (optional) |
| `data/fleet/history.sqlite` | Last-seen, telemetry, apply stamps, `mesh_audit` |

Observed telemetry lives in sqlite only, not in YAML.

## Fleet quick reference

Full detail: [commands/fleet.md](commands/fleet.md) and the [fleet skill](../.cursor/skills/fleet/SKILL.md).

### Common runs

```bash
export ENVYBOT_HOME=/path/to/book
cd /path/to/envybot

# Default: UI + auto poll/apply
./envybot fleet

# Headless one unit, force deploy + GETs
./envybot fleet --only me0048 --full-sync --no-web --companion 3355

# Stale companion hop cache: dump old path, clear, flood on next login
./envybot fleet --only 3d35 --refresh-paths --full-sync --no-web --companion 3355
```

### Why "Fleet idle"?

Auto work only queues units that are **due**. With `--apply-only`, nothing runs when every SET field is already stamped synced in sqlite. Companion may still connect; neighbor ping is not apply.

Force work: UI **Deploy**, or CLI **`--full-sync`** (Deploy + GETs), or edit the book so apply is due again.

### Routing and paths

| Policy | Book | Behavior |
|--------|------|----------|
| **path** (default) | omit `routing` or `routing: path` | Use companion cached `out_path` when present; flood-login when empty; discard cache after 3 timeouts |
| **flood** | `routing: flood` | Every send floods (high airtime) |
| **direct** | `routing: direct` | Zero-hop every send |

| Flag | Use when |
|------|----------|
| `--refresh-paths` | Cached hops are wrong: prints **`path was:`**, clears cache, next login shows **`path: flood`** |
| `--force-path EA6E,E9BD,…` | Pin hops for this run (opposite of flood; stale discard disabled) |

Login prints the route about to be used under `path:` (or `path: flood` / `path: direct`). `--refresh-paths` prints the stale route first as `path was:`.

### Unit filters

`--only` / `--skip` accept comma lists and globs. Matches: book key, `unit_id`, alias, site slug, site name, pubkey prefix (4+ hex, e.g. `3d35`). Filters dashboard and auto poll/apply.

## Repo docs map

| Doc | Audience |
|-----|----------|
| [README.md](../README.md) | Install, command list |
| [MEMORY.md](../MEMORY.md) | Agent architecture index |
| [AGENTS.md](../AGENTS.md) | Agent entrypoint |
| [.cursor/skills/fleet/SKILL.md](../.cursor/skills/fleet/SKILL.md) | Fleet operator FAQ for agents |
| [CHANGELOG.md](../CHANGELOG.md) | Product changes |
