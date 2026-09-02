"""Console manual CLI → poll sqlite capture."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from envybot.fleet_worker import (
    PollAccumulator,
    WorkerContext,
    _apply_cli_poll_reply,
    _console_capture_tracked,
    _console_poll_group,
)
from envybot.history import get_last_seen, open_history
from envybot.jobs import UnitQueue
from envybot.radio import FleetSession, PollLog, RouterTarget


def _worker_ctx(conn: sqlite3.Connection) -> WorkerContext:
    return WorkerContext(
        client=MagicMock(),
        conn=conn,
        nodes={},
        sites={},
        doc={},
        keys={},
        session=FleetSession(),
        log=PollLog(),
        cmd_timeout=9.0,
        login_timeout=12.0,
        discover_wait=3.0,
        skip_discover=True,
        do_poll=True,
        do_apply=False,
    )


class ConsolePollGroupTests(unittest.TestCase):
    def test_exact_and_alias(self) -> None:
        self.assertEqual(_console_poll_group("ver"), "firmware")
        self.assertEqual(_console_poll_group("  OTA   stats  "), "ota_status")
        self.assertEqual(_console_poll_group("ota neighbors"), "ota_ls")
        self.assertEqual(_console_poll_group("ota ls 3"), "ota_ls")


class ConsolePollCaptureTests(unittest.TestCase):
    def test_ver_stamps_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            ctx = _worker_ctx(conn)
            target = RouterTarget(
                key="me0032",
                unit_id="me0032",
                name="t",
                site=None,
                pubkey_hex="aa" * 32,
                admin_password="secret",
            )
            uq = UnitQueue(target=target)
            raw = "v1.17.1-ev1 RAK4631"
            group = _console_capture_tracked(ctx, uq, target, "ver", raw)
            self.assertEqual(group, "firmware")
            seen = get_last_seen(conn, "me0032")
            assert seen is not None
            self.assertEqual(seen["firmware_version"], "v1.17.1-ev1")

    def test_ota_stats_stamps_ota_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            ctx = _worker_ctx(conn)
            target = RouterTarget(
                key="me0032",
                unit_id="me0032",
                name="t",
                site=None,
                pubkey_hex="aa" * 32,
                admin_password="secret",
            )
            uq = UnitQueue(target=target)
            raw = (
                "OTA | fw v1.17.1-ev1 id=deadbeef body=abcd1234 100b 434K | serv 2 dg=cafebabe | "
                "fetch dl 50/100 50% id=11223344 3600s | af=any hops=3"
            )
            group = _console_capture_tracked(ctx, uq, target, "ota stats", raw)
            self.assertEqual(group, "ota_status")
            seen = get_last_seen(conn, "me0032")
            assert seen is not None
            self.assertIsNotNone(seen.get("ota_status_at"))

    def test_apply_ignores_garbage(self) -> None:
        acc = PollAccumulator()
        ctx = _worker_ctx(MagicMock())
        target = RouterTarget(
            key="x",
            unit_id="x",
            name="t",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="secret",
        )
        ok = _apply_cli_poll_reply("firmware", "ERR nope", acc=acc, ctx=ctx, target=target)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
