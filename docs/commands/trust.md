# `envybot trust`

Write book name + resolved location + pubkey onto a companion as
contacts/favorites. In-app maps read those stored coords.

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
| `--all-units` | Include bag/bench |

Book-level ACL allowlist (`trust.companions` in `nodes.yaml`) is applied
by `fleet`, not by this command.
