# AGENTS.md — EnvyBot

Fleet CLI for MeshEnvy radios. Read [`MEMORY.md`](MEMORY.md) first.
Greenfield (unshipped): [`.cursor/rules/greenfield.mdc`](.cursor/rules/greenfield.mdc).
No builtin migrations, dual-read, or leftover book keys.

```bash
uv sync
export ENVYBOT_HOME=/path/to/book
./envybot fleet
./envybot fleet --web-only   # browse book without radio
./envybot onboard
./envybot seed build/otatest
```

Book is a private directory with `nodes.yaml`. Point with `--book` or
`ENVYBOT_HOME`. Never commit secrets.

Command manuals: [`docs/commands/`](docs/commands/).

New commands: add `src/envybot/commands/<name>.py` with `main(argv)` and register it in `cli.py` `COMMANDS`. Add `docs/commands/<name>.md` and a README row in the same change set.
