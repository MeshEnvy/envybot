#!/usr/bin/env python3
"""Run one MeshCore CLI command on a remote repeater over the companion link.

Login only if the companion is not already on the book's ACL.
No clock sync, radio policy SET, or nodes.yaml writes.
Stdout is the reply body only; progress goes to stderr.

Examples:
  ./envybot cmd poito get name
  ./envybot cmd me0016 ver
  ./envybot cmd "PV Peak" get advert.interval
  ./envybot cmd poito
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from meshcore import MeshCore
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "meshcore not installed. From envybot root:\n"
        "  uv sync\n"
        "  ./envybot cmd"
    ) from exc

from envybot.history import insert_command, open_history
from envybot.nodes_doc import load_nodes_doc
from envybot.radio import (
    FleetSession,
    RouterTarget,
    add_companion_args,
    cli_suggests_auth_failure,
    connect,
    maybe_admin_access,
    send_cmd_sync,
    sync_fleet_contacts,
)
from envybot.selector import format_candidates, resolve_selector

REDACT_HINTS = ("password", "prv.key", "guest.password", "prv_key", "private.key")
REPL_EXIT = frozenset({"quit", "exit", "q"})


@dataclass
class CmdLog:
    progress: bool = True
    verbose: bool = False

    def step(self, msg: str) -> None:
        if self.progress:
            print(msg, file=sys.stderr, flush=True)

    def detail(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)


def should_redact(text: str) -> bool:
    lower = text.lower()
    return any(hint in lower for hint in REDACT_HINTS)


def redact_snippet(text: str | None, *, max_len: int = 120) -> str | None:
    if text is None:
        return None
    if should_redact(text):
        return "[redacted]"
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def log_cmd_record(
    book: Path,
    *,
    target: RouterTarget,
    selector: str,
    command: str,
    reply: str | None,
    ok: bool,
) -> None:
    conn = open_history(book)
    insert_command(
        conn,
        unit=target.key,
        argv=redact_snippet(command, max_len=500) or command,
        reply=redact_snippet(reply),
        ok=ok,
    )
    conn.close()


async def send_remote_cli(
    client: MeshCore,
    target: RouterTarget,
    command: str,
    *,
    cmd_timeout: float,
    login_timeout: float,
    attempts: int,
    session: FleetSession,
    log: CmdLog,
) -> tuple[int, str | None]:
    text = await send_cmd_sync(
        client,
        target,
        command,
        timeout=cmd_timeout,
        attempts=attempts,
        log=log,  # type: ignore[arg-type]
        session=session,
    )
    if text is None:
        return 2, None
    if cli_suggests_auth_failure(text):
        session.clear_auth(target.key)
        log.step(f"auth denied for {command!r}")
        return 2, text
    return 0, text


async def run_repl(
    client: MeshCore,
    target: RouterTarget,
    *,
    selector: str,
    cmd_timeout: float,
    login_timeout: float,
    attempts: int,
    session: FleetSession,
    log: CmdLog,
    book: Path,
) -> int:
    prompt = f"{target.unit_id}> "
    log.step(f"REPL on {target.unit_id} ({target.name}); type quit to exit")
    while True:
        try:
            line = input(prompt)
        except EOFError:
            print(file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130
        cmd = line.strip()
        if not cmd:
            continue
        if cmd.lower() in REPL_EXIT:
            return 0
        code, reply = await send_remote_cli(
            client,
            target,
            cmd,
            cmd_timeout=cmd_timeout,
            login_timeout=login_timeout,
            attempts=attempts,
            session=session,
            log=log,
        )
        log_cmd_record(
            book,
            target=target,
            selector=selector,
            command=cmd,
            reply=reply,
            ok=code == 0 and reply is not None,
        )
        if reply is not None:
            print(reply)
        if code != 0:
            return code


async def run(args: argparse.Namespace) -> int:
    doc = load_nodes_doc(args.nodes)
    resolved = resolve_selector(doc, args.selector)
    if resolved.error or resolved.target is None:
        print(resolved.error or "unknown selector", file=sys.stderr)
        if resolved.candidates:
            print(format_candidates(resolved.candidates), file=sys.stderr)
        return 1

    target = resolved.target
    log = CmdLog(progress=not args.quiet, verbose=args.verbose)
    session = FleetSession()
    client = await connect(args)
    session.bind_companion(client)
    session.attach_orphan_watch(client, log)  # type: ignore[arg-type]

    try:
        await sync_fleet_contacts(client, [target], log=log)  # type: ignore[arg-type]

        node = (doc.get("nodes") or {}).get(target.key) or {}
        ok, err, _clock = await maybe_admin_access(
            client,
            target,
            node=node if isinstance(node, dict) else {},
            doc=doc,
            login_timeout=args.login_timeout,
            cmd_timeout=args.timeout,
            attempts=args.attempts,
            session=session,
            log=log,  # type: ignore[arg-type]
            fetch_clock=False,
        )
        if not ok:
            print(err or "admin login failed", file=sys.stderr)
            return 2

        command = " ".join(args.cli).strip()
        if not command:
            return await run_repl(
                client,
                target,
                selector=args.selector,
                cmd_timeout=args.timeout,
                login_timeout=args.login_timeout,
                attempts=args.attempts,
                session=session,
                log=log,
                book=args.nodes.parent,
            )

        code, reply = await send_remote_cli(
            client,
            target,
            command,
            cmd_timeout=args.timeout,
            login_timeout=args.login_timeout,
            attempts=args.attempts,
            session=session,
            log=log,
        )
        log_cmd_record(
            args.nodes.parent,
            target=target,
            selector=args.selector,
            command=command,
            reply=reply,
            ok=code == 0 and reply is not None,
        )
        if reply is not None:
            print(reply)
        return code
    finally:
        await client.stop_auto_message_fetching()
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, default=Path("nodes.yaml"))
    add_companion_args(parser)
    parser.add_argument(
        "selector",
        help="Unit key, ME####, pubkey prefix, name, or site slug",
    )
    parser.add_argument(
        "cli",
        nargs="*",
        help="Remote CLI words (omit for interactive REPL)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="No login/retry progress on stderr",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
