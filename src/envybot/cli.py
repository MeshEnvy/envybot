"""envybot — fleet CLI. Commands are modules under envybot.commands."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from envybot.book import resolve_book

Command = Callable[[Path, list[str]], int]


def _inject_flag(argv: list[str], flag: str, value: str) -> list[str]:
    if flag in argv:
        return argv
    return [flag, value, *argv]


def cmd_fleet(book: Path, argv: list[str]) -> int:
    from envybot.book import nodes_path
    from envybot.commands import fleet

    return fleet.main(_inject_flag(argv, "--nodes", str(nodes_path(book))))


def cmd_trust(book: Path, argv: list[str]) -> int:
    from envybot.book import nodes_path
    from envybot.commands import trust

    return trust.main(_inject_flag(argv, "--nodes", str(nodes_path(book))))


def cmd_onboard(book: Path, argv: list[str]) -> int:
    from envybot.book import nodes_path
    from envybot.commands import onboard

    return onboard.main(_inject_flag(argv, "--nodes", str(nodes_path(book))))


def cmd_cmd(book: Path, argv: list[str]) -> int:
    from envybot.book import nodes_path
    from envybot.commands import cmd

    return cmd.main(_inject_flag(argv, "--nodes", str(nodes_path(book))))


def cmd_seed(book: Path, argv: list[str]) -> int:
    from envybot.commands import seed

    return seed.main(argv)


COMMANDS: dict[str, tuple[str, Command]] = {
    "fleet": ("Localhost fleet manager (map, poll, apply)", cmd_fleet),
    "trust": ("Companion contacts and keys.yaml ACL grant", cmd_trust),
    "onboard": ("USB-serial onboard a repeater (idempotent)", cmd_onboard),
    "cmd": ("Run remote MeshCore CLI on one unit", cmd_cmd),
    "seed": ("USB-serial OTA seeder (.mota folder relay)", cmd_seed),
}


def _usage() -> str:
    lines = [
        "usage: envybot [--book DIR] <command> [args…]",
        "",
        "Fleet book is a private directory that contains nodes.yaml.",
        "Set ENVYBOT_HOME or pass --book. Cwd works if it already has the file.",
        "",
        "commands:",
    ]
    width = max(len(name) for name in COMMANDS)
    for name, (help_text, _) in COMMANDS.items():
        lines.append(f"  {name.ljust(width)}  {help_text}")
    return "\n".join(lines)


def _split_globals(argv: list[str]) -> tuple[Path | None, list[str]]:
    book: Path | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--book":
            if i + 1 >= len(argv):
                raise SystemExit("envybot: --book requires a path")
            book = Path(argv[i + 1])
            i += 2
            continue
        if tok.startswith("--book="):
            book = Path(tok.split("=", 1)[1])
            i += 1
            continue
        break
    return book, argv[i:]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    book_arg, rest = _split_globals(argv)
    if not rest or rest[0] in ("-h", "--help"):
        print(_usage())
        return 0
    name = rest[0]
    if name not in COMMANDS:
        print(_usage(), file=sys.stderr)
        print(f"\nenvybot: unknown command {name!r}", file=sys.stderr)
        return 2
    book = resolve_book(book_arg)
    _help, run = COMMANDS[name]
    return run(book, rest[1:])


if __name__ == "__main__":
    raise SystemExit(main())
