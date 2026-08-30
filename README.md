# EnvyBot

MeshEnvy fleet CLI. Companion monitor and USB repeater onboard.

The **book** (`nodes.yaml`, creds, poll log) is private and is not in this
repo. Point at it with `--book` or `ENVYBOT_HOME`. If the current directory
already contains `nodes.yaml`, that is used.

```bash
uv sync
export ENVYBOT_HOME=/path/to/book
./envybot monitor
./envybot cmd me0016 ver
./envybot onboard
```

## Commands

| Command | Manual | What it does |
|---------|--------|--------------|
| [`monitor`](docs/commands/monitor.md) | [docs/commands/monitor.md](docs/commands/monitor.md) | Poll deployed MeshCore units over LoRa. Write last-seen into the book. |
| [`cmd`](docs/commands/cmd.md) | [docs/commands/cmd.md](docs/commands/cmd.md) | Run remote MeshCore CLI on one unit (one-shot or REPL). |
| [`onboard`](docs/commands/onboard.md) | [docs/commands/onboard.md](docs/commands/onboard.md) | USB text-CLI onboard of a repeater under test. |

Global flags (before the command): `--book DIR`.

```
./envybot [--book DIR] <command> [args…]
```

Add a command by putting `src/envybot/commands/<name>.py` with `main(argv)`
and registering it in `cli.py` `COMMANDS`.

Daemon / radio mux is later. `mcmt-gateway` remains the burn bridge.
