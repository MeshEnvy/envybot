# EnvyBot

MeshEnvy fleet CLI. Companion pull and USB repeater onboard.

The **book** (`nodes.yaml`, creds, `data/fleet/polls.jsonl`) stays in private
[`peaky-nevada`](https://github.com/MeshEnvy). This repo has no secrets.

```bash
uv sync
./envybot pull
./envybot onboard
./envybot onboard /dev/cu.usbmodem1444301
./envybot pull --unit me0016 --force
```

Book path: `--book DIR`, `ENVYBOT_HOME`, sibling `../peaky-nevada`, or cwd
if it contains `nodes.yaml`.

Commands are modules under `src/envybot/commands/`. Add a function to
`COMMANDS` in `cli.py`.

Daemon / radio mux is later. `mcmt-gateway` remains the burn bridge.
