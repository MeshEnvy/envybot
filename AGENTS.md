# AGENTS.md — EnvyBot

Fleet CLI for MeshEnvy radios. Read [`MEMORY.md`](MEMORY.md) first.

```bash
uv sync
./envybot pull
./envybot onboard
```

Book is private `peaky-nevada/` (`nodes.yaml`). Point with `--book` or `ENVYBOT_HOME`. Never commit secrets.

New commands: add `src/envybot/commands/<name>.py` with `main(argv)` and register it in `cli.py` `COMMANDS`.
