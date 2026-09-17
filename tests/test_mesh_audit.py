"""Mesh audit sqlite + flood-first helpers."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from meshcore import EventType
from ruamel.yaml import YAML

from envybot.history import (
    begin_mesh_audit,
    finish_mesh_audit,
    list_mesh_audit,
    mark_mesh_audit_late,
    mesh_audit_row,
    open_history,
)
from envybot.jobs import FleetScheduler
from envybot.web.hub import FleetHub
from envybot.web.server import MonitorWeb, make_app
from envybot.radio import (
    FleetSession,
    PollLog,
    RouterTarget,
    _audit_begin,
    _audit_finish,
    audit_path_at_send,
    log_contact_path,
    prepare_login_route,
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

    def test_password_cli_label_and_reply_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            session = FleetSession()
            session.conn = conn
            aid = _audit_begin(
                session,
                unit="ME0001",
                kind="cli",
                label="set guest.password hunter2",
                attempt=1,
                path="flood",
                wait_s=8.0,
            )
            assert aid is not None
            _audit_finish(
                session,
                aid,
                ok=True,
                outcome="ok",
                reply="password now: hunter2",
            )
            row = _row(conn, aid)
            self.assertEqual(row["label"], "[redacted]")
            self.assertEqual(row["reply"], "[redacted]")
            aid2 = _audit_begin(
                session,
                unit="ME0001",
                kind="cli",
                label="get name",
                attempt=1,
                path="flood",
                wait_s=8.0,
            )
            assert aid2 is not None
            self.assertEqual(_row(conn, aid2)["label"], "get name")

    def test_late_orphan_cli_reply_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            session = FleetSession()
            session.conn = conn
            session._orphan_log = PollLog()
            aid = _audit_begin(
                session,
                unit="ME0001",
                kind="cli",
                label="password AdminOneStrong1",
                attempt=1,
                path="flood",
                wait_s=8.0,
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=False, outcome="timeout")
            exp = session.track_expect(
                kind="cli",
                label="password",
                unit="ME0001",
                pubkey_prefix="aabbccddeeff",
                deadline=time.monotonic() - 1,
                n_of="1/1",
            )
            exp.audit_id = aid
            event = MagicMock()
            event.payload = {
                "text": "password now: hunter2",
                "pubkey_prefix": "aabbccddeeff",
            }
            event.attributes = {"pubkey_prefix": "aabbccddeeff"}
            session._note_orphan("cli", event)
            row = _row(conn, aid)
            self.assertEqual(row["outcome"], "late")
            self.assertEqual(row["reply"], "[redacted]")
            self.assertNotIn("hunter2", row["reply"] or "")
            self.assertEqual(row["label"], "[redacted]")


class MeshAuditRowTests(unittest.TestCase):
    def test_mesh_audit_row_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            aid = begin_mesh_audit(
                conn,
                unit="me0045",
                kind="cli",
                label="get name",
                attempt=2,
                path="flood",
                wait_s=12.0,
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=True, outcome="ok", reply="ME0045")
            loaded = mesh_audit_row(conn, aid)
            assert loaded is not None
            unit, row = loaded
            self.assertEqual(unit, "me0045")
            self.assertEqual(row["label"], "get name")
            self.assertEqual(row["outcome"], "ok")
            self.assertIsNotNone(row["duration_s"])


class ListMeshAuditTests(unittest.TestCase):
    def test_newest_first_with_before_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            ids: list[int] = []
            for idx in range(5):
                aid = begin_mesh_audit(
                    conn,
                    unit="ME0045",
                    kind="cli",
                    label=f"cmd-{idx}",
                    attempt=idx + 1,
                    path="flood",
                    wait_s=8.0,
                )
                assert aid is not None
                finish_mesh_audit(conn, aid, ok=True, outcome="ok", reply=f"ok-{idx}")
                ids.append(aid)

            page1, more1 = list_mesh_audit(conn, "me0045", limit=2)
            self.assertTrue(more1)
            self.assertEqual([row["label"] for row in page1], ["cmd-4", "cmd-3"])
            self.assertEqual(page1[0]["id"], ids[-1])

            page2, more2 = list_mesh_audit(
                conn, "me0045", limit=2, before_id=page1[-1]["id"]
            )
            self.assertTrue(more2)
            self.assertEqual([row["label"] for row in page2], ["cmd-2", "cmd-1"])

            page3, more3 = list_mesh_audit(
                conn, "me0045", limit=2, before_id=page2[-1]["id"]
            )
            self.assertFalse(more3)
            self.assertEqual([row["label"] for row in page3], ["cmd-0"])

    def test_timeout_has_null_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            aid = begin_mesh_audit(
                conn,
                unit="me0001",
                kind="login",
                label="login",
                attempt=3,
                path="266a b3b3",
                wait_s=11.0,
                source="console",
            )
            assert aid is not None
            finish_mesh_audit(conn, aid, ok=False, outcome="timeout")
            rows, has_more = list_mesh_audit(conn, "me0001", limit=10)
            self.assertFalse(has_more)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["outcome"], "timeout")
            self.assertIsNone(row["duration_s"])
            self.assertEqual(row["source"], "console")


class PrepareSendRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_mode_rides_cached_route(self) -> None:
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
            site="ophir",
            pubkey_hex="b2f84713d830" + "0" * 52,
            admin_password="pw",
        )
        await prepare_login_route(client, target, log=PollLog())
        self.assertEqual(contact["out_path_len"], 2)
        client.commands.reset_path.assert_not_awaited()
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

            def substep(self, msg: str) -> None:
                lines.append(f"    {msg}")

        log_contact_path(client, target, log=CaptureLog())
        self.assertEqual(lines, ["path:", "    266a", "    → b3b3"])
        self.assertEqual(audit_path_at_send(client, target), "266a b3b3")


class LoginErrCoercionTests(unittest.TestCase):
    def test_dict_err_does_not_crash_rejected_check(self) -> None:
        err: object = {"reason": "no_event_received"}
        self.assertFalse(err and "rejected" in str(err).lower())


def _write_book(book: Path) -> tuple[Path, Path]:
    nodes = book / "nodes.yaml"
    sites = book / "sites.yaml"
    yaml = YAML()
    with nodes.open("w") as fh:
        yaml.dump(
            {
                "nodes": {
                    "me0003": {
                        "unit_id": "ME0003",
                        "firmware_platform": "meshcore",
                        "identity_pubkey": "b" * 64,
                        "admin_password": "AdminTwoStrong2",
                    },
                }
            },
            fh,
        )
    with sites.open("w") as fh:
        yaml.dump({"sites": {"ophir": {"node": "me0003", "loc": [39.3, -119.6]}}}, fh)
    return nodes, sites


class AuditApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        book = Path(self.tmp.name)
        nodes_path, sites_path = _write_book(book)
        self.conn = open_history(book)
        aid = begin_mesh_audit(
            self.conn,
            unit="me0003",
            kind="cli",
            label="get name",
            attempt=1,
            path="flood",
            wait_s=12.0,
        )
        assert aid is not None
        finish_mesh_audit(self.conn, aid, ok=True, outcome="ok", reply="ME0003")
        self.web_ctx = MonitorWeb(
            hub=FleetHub(),
            nodes_path=nodes_path,
            sites_path=sites_path,
            stale_secs=86400.0,
            url="http://127.0.0.1:8787/",
        )
        self.web_ctx.bind_scheduler(
            FleetScheduler(),
            manual_keys=set(),
            conn=self.conn,
            nodes={"me0003": {}},
            sites={},
            doc={"nodes": {"me0003": {}}},
            keys={},
            do_poll=True,
            do_apply=True,
            skip_discover=False,
            discover_wait=12.0,
        )
        await self.web_ctx.refresh_snapshot()
        app = make_app(self.web_ctx)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.conn.close()
        self.tmp.cleanup()

    async def test_get_audit_returns_rows(self) -> None:
        resp = await self.client.get("/api/audit/me0003")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("unit"), "me0003")
        rows = body.get("rows")
        self.assertIsInstance(rows, list)
        assert isinstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("label"), "get name")
        self.assertFalse(body.get("has_more"))

    async def test_get_audit_unknown_unit_empty(self) -> None:
        resp = await self.client.get("/api/audit/me9999")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("rows"), [])
        self.assertFalse(body.get("has_more"))


if __name__ == "__main__":
    unittest.main()
