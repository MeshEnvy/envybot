"""Resolve the private fleet book (nodes.yaml). Never store secrets in this repo."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "ENVYBOT_HOME"
NODES_NAME = "nodes.yaml"
POLL_LOG = Path("data/fleet/polls.jsonl")


class BookError(SystemExit):
    pass


def _as_book_dir(path: Path) -> Path | None:
    path = path.expanduser().resolve()
    if path.is_file() and path.name == NODES_NAME:
        return path.parent
    if path.is_dir() and (path / NODES_NAME).is_file():
        return path
    return None


def resolve_book(explicit: Path | str | None = None) -> Path:
    """Directory that contains nodes.yaml.

    Order: ``--book`` / argument, ``ENVYBOT_HOME``, cwd.
    """
    if explicit:
        found = _as_book_dir(Path(explicit))
        if found:
            return found
        raise BookError(f"no {NODES_NAME} at {explicit}")

    env = os.environ.get(ENV_HOME)
    if env:
        found = _as_book_dir(Path(env))
        if found:
            return found
        raise BookError(f"{ENV_HOME}={env} has no {NODES_NAME}")

    found = _as_book_dir(Path.cwd())
    if found:
        return found

    raise BookError(
        f"No fleet book found. Set {ENV_HOME} or pass --book "
        f"to the directory that contains {NODES_NAME}."
    )


def nodes_path(book: Path) -> Path:
    return book / NODES_NAME


def poll_log_path(book: Path) -> Path:
    return book / POLL_LOG
