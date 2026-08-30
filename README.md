# EnvyBot

MeshEnvy fleet CLI. Localhost manager, companion trust import, remote CLI,
and USB repeater onboard.

The **book** (`nodes.yaml`, creds, `data/fleet/history.sqlite`) is private
and is not in this repo. Point at it with `--book` or `ENVYBOT_HOME`. If
the current directory already contains `nodes.yaml`, that is used.

```bash
uv sync
export ENVYBOT_HOME=/path/to/book
./envybot fleet            # poll + apply + live map at http://127.0.0.1:8787/
./envybot fleet --web-only
./envybot trust
./envybot cmd me0016 ver
./envybot onboard
```

## Commands

| Command | Manual | What it does |
|---------|--------|--------------|
| [`fleet`](docs/commands/fleet.md) | [docs/commands/fleet.md](docs/commands/fleet.md) | Map, poll GET → sqlite, apply privacy mask (or `public: true`). |
| [`trust`](docs/commands/trust.md) | [docs/commands/trust.md](docs/commands/trust.md) | Companion contacts plus `keys.yaml` / fleet ACL grant. |
| [`cmd`](docs/commands/cmd.md) | [docs/commands/cmd.md](docs/commands/cmd.md) | Run remote MeshCore CLI on one unit (one-shot or REPL). |
| [`onboard`](docs/commands/onboard.md) | [docs/commands/onboard.md](docs/commands/onboard.md) | USB text-CLI onboard of a repeater under test. |

Global flags (before the command): `--book DIR`.

```
./envybot [--book DIR] <command> [args…]
```

Add a command by putting `src/envybot/commands/<name>.py` with `main(argv)`
and registering it in `cli.py` `COMMANDS`.

Daemon / radio mux is later. `mcmt-gateway` remains the burn bridge.
