# `envybot trust`

Write site name + resolved location + pubkey onto a companion as
contacts/favorites. Includes bag/bench units (no site bind). Contact
name is the bound site's `name` (e.g. Ophir), or `unit_id` when the
unit has no site. Location comes from `sites.yaml` loc. In-app maps
read those stored coords.

```
./envybot [--book DIR] trust [flags]
```

Does not copy admin/guest passwords or field-node secret keys.

Re-run after book renames, site moves, or new units.

## Flags

Same companion transport flags as `fleet`.

| Flag | Meaning |
|------|---------|
| `--export FILE` | Write a JSON contact list |
| `--export-only` | Write `--export` and exit (no radio) |
| `--unit KEY` | One unit (repeatable) |

Book-level ACL allowlist (`trust.companions` in `nodes.yaml`) is applied
by `fleet`, not by this command.
