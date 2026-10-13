# `envybot range`

Compare two USB repeaters against the same neighbors. Each board sends
`discover.neighbors`. The command reads the USB lines the repeater prints
for each reply and averages them.

```
./envybot range --a PORT --b PORT
./envybot range
```

With no ports, both boards must be the only two app-mode USB serial devices
(Xiao `2886:8044` or RAK4631 `239A:8029`).

## What the numbers are

The repeater image prints:

```
discover <pubkey8> rx=<dB> in=<dB>
```

| Field | Meaning |
|-------|---------|
| `rx` | SNR at this board. How well it heard that neighbor. |
| `in` | SNR at that neighbor. How well it heard this board. |
| `rssi` | This board's RSSI for that reply, in dBm. Older images omit it. |

After each listen the report also records that board's `noise_floor` (dBm) and `battery_mv` from `stats-radio` and `stats-core`. Those describe the board for the round, not one neighbor. A worse `rx` with the same RSSI and a higher noise floor is the room. A worse `rx` with a worse RSSI is a weaker signal at that radio.

`neighbors` still stores `rx` only. A current slim build is required. An
older image answers the discover and prints no `discover` line.

The table columns are the two local boards, named from `get name`. Each
cell is `rx/in` in dB, rounded to the nearest integer. A blank cell was not
heard. A `*` in front of a cell is the stronger side of that row (one point
for a better `rx`, one point for a better `in`). A `*` in front of a name
is the board that won more rows.

## Cadence

Each remote answers at most 4 discovers per 120 seconds, and both boards
share that budget. The command pings A, then B, and does not overlap them.
Default gap is 30 seconds. A shorter `--gap` will make remotes stop answering.

Defaults: 3 rounds, 12 second listen, 30 second gap.

## Live

```bash
./envybot range --live
```

`q` stops any run, including a fixed `--rounds` count, and prints the
report collected so far. Ctrl+C does the same. `--live` keeps alternating
discovers until then. The same 30 second gap
applies, so a remote's 4-per-120s budget still holds. After each board
the screen redraws the cumulative table and the latest round. `q` does
not need Enter. Without a terminal, Ctrl+C stops it.

`--rounds N` with `--live` stops after N rounds or when you press `q`.

## Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--a PORT` | autodetect | Board A serial port |
| `--b PORT` | autodetect | Board B serial port |
| `--live` | off | Repeat until `q` or Ctrl+C |
| `--rounds N` | `3` | Samples per board. With `--live`, a cap instead of the default |
| `--listen SEC` | `12` | How long to collect replies |
| `--gap SEC` | `30` | Time from one discover to the next |
| `--baud` | `115200` | USB console baud |
| `--timeout SEC` | `5` | CLI reply timeout for `get name` / `get radio` |
| `-v` | | Print USB traffic |

Heard repeaters use the fleet display name (site name, else alias, else
unit id) plus the 2-byte pubkey prefix in parentheses, for example
`Poito (a83f)`. That prefix is the MeshCore path hash. An unknown key
stays the 8-hex id with the same hash, `aabbccdd (aabb)`.

The header shows each board's name, port, channel, `tx` power, `rxgain`,
and FEM rx/tx gain (`n/a` when the board has no FEM). It says whether
those settings match.
