"""Mesh audit sqlite + flood-first helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from meshcore import EventType

from envybot.history import (
    begin_mesh_audit,
    finish_mesh_audit,
    mark_mesh_audit_late,
    open_history,
)
from envybot.radio import (
    PollLog,
    RouterTarget,
    audit_path_at_send,
    log_contact_path,
    reset_to_flood,
)


def _row(conn, audit_id: int) -> dict:
    row = conn.execute("SELECT * FROM mesh_audit WHERE id = ?", (audit_id,)).fetchone()
    assert row is not None
    return dict(row)


class MeshAuditTests(unittest.TestCase):
    def test_timeout_leaves_ts_reply_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            aid = begin_mesh_audit(
                conn,
                unit="me0001",
                kind="cli",
                label="get name",
                attempt=1,
                path="flood",
                wait_s=12.0,
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=False, outcome="timeout")
            row = _row(conn, aid)
            self.assertIsNone(row["ts_reply"])
            self.assertEqual(row["outcome"], "timeout")
            self.assertEqual(row["ok"], 0)

    def test_success_fills_ts_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            aid = begin_mesh_audit(
                conn,
                unit="me0001",
                kind="login",
                label="login",
                attempt=1,
                path="flood",
                wait_s=20.0,
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=True, outcome="ok", reply="clock=1700000000")
            row = _row(conn, aid)
            self.assertIsNotNone(row["ts_reply"])
            self.assertEqual(row["outcome"], "ok")
            self.assertEqual(row["reply"], "clock=1700000000")

    def test_late_update_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            aid = begin_mesh_audit(
                conn,
                unit="me0001",
                kind="cli",
                label="clock",
                attempt=1,
                path="flood",
                wait_s=8.0,
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=False, outcome="timeout")
            mark_mesh_audit_late(conn, aid, reply="12:00 - 1/1/2026 UTC")
            row = _row(conn, aid)
            self.assertEqual(row["outcome"], "late")
            self.assertEqual(row["ok"], 1)
            self.assertIsNotNone(row["ts_reply"])
            self.assertIn("UTC", row["reply"] or "")


class ResetToFloodTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_in_memory_path(self) -> None:
        contact = {
            "out_path_len": 2,
            "out_path_hash_mode": 1,
            "out_path": "266ab3b3",
        }
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = contact
        ok = MagicMock(type=EventType.OK)
        client.commands.reset_path = AsyncMock(return_value=ok)
        target = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="test",
            site=None,
            pubkey_hex="b2f84713d830" + "0" * 52,
            admin_password="pw",
        )
        await reset_to_flood(client, target, log=PollLog())
        self.assertEqual(contact["out_path_len"], -1)
        self.assertEqual(contact["out_path"], "")
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, target, log=CaptureLog())
        self.assertEqual(lines, ["path: flood"])
        self.assertEqual(audit_path_at_send(client, target), "flood")


class LoginErrCoercionTests(unittest.TestCase):
    def test_dict_err_does_not_crash_rejected_check(self) -> None:
        err: object = {"reason": "no_event_received"}
        self.assertFalse(err and "rejected" in str(err).lower())


if __name__ == "__main__":
    unittest.main()
