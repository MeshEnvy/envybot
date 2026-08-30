# `envybot cmd`

Run one MeshCore CLI command on a remote repeater over the companion link.
Admin login always. No clock sync, radio policy SET, or `nodes.yaml` writes.

Uses a **companion** radio (BLE first). Same desk radio as [`monitor`](monitor.md).
Not USB repeater text CLI ([`onboard`](onboard.md)).

```
./envybot [--book DIR] cmd [flags] <selector> [cli words…]
```

## What it does

1. Resolve `<selector>` against the book (unit key, `ME####`, pubkey prefix,
   name, normalized name, unique site slug).
2. Connect a companion and admin-login the target.
3. Send the CLI string and print the reply body on **stdout**.
4. Progress (`login OK`, retries) goes to **stderr**.
5. Append an audit line to `data/fleet/polls.jsonl` (`event: cmd`).

Omit CLI words (or pass only the selector) to enter an interactive REPL on
the same login session. Type `quit` or EOF to exit.

Does not update last-seen state. Use [`monitor`](monitor.md) for that.

## Selector

No `alias:` field in the book. Short names work when unique after
normalization (strip `{…}`, trailing `| …`, collapse space).

Ambiguous matches print candidates on stderr and exit 1. Meshtastic rows
(no admin password) are refused with a one-line reason.

Examples:

```bash
./envybot cmd poito get name
./envybot cmd me0016 ver
./envybot cmd "PV Peak" get advert.interval
./envybot cmd poito
```

## Companion flags

Same as [`monitor`](monitor.md): `--transport`, `--ble`, `--serial`, `--tcp`,
`--scan-timeout`, `--baud`, `--timeout`, `--login-timeout`, `--attempts`, `-v`.

| Flag | Default | Meaning |
|------|---------|---------|
| `-q` / `--quiet` | | No login/retry progress on stderr |

`--nodes` and `--log-file` are injected from the book.

## Exit status

| Code | Meaning |
|------|---------|
| `0` | Reply received (one-shot) or clean REPL exit |
| `1` | Unknown or ambiguous selector, or meshtastic/no-creds |
| `2` | Login failure, timeout, or auth-denied CLI |
| `130` | REPL interrupted (Ctrl+C) |

## Audit log

Each command appends one JSONL line. Commands or replies mentioning
`password`, `prv.key`, or `guest.password` are stored as `[redacted]`.

## Examples

```bash
./envybot cmd me0016 get name
./envybot cmd poito ver
./envybot cmd russell reboot
./envybot cmd me0016
```

After a GET that should update the book, run monitor:

```bash
./envybot monitor --unit me0016 --group name
```
