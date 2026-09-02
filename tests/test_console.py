"""Tabbed console manager: staging queue, reclaim, isolation."""

from __future__ import annotations

import unittest
from envybot.jobs import FleetScheduler
from envybot.radio import RouterTarget
from envybot.web.console import ConsoleManager
from envybot.web.hub import FleetHub


def _target(key: str = "me0001") -> RouterTarget:
    return RouterTarget(
        key=key,
        unit_id=key.upper(),
        name=key,
        site=None,
        pubkey_hex="a" * 64,
        admin_password="secret",
    )


class ConsoleManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mgr = ConsoleManager()
        self.sched = FleetScheduler()
        self.target = _target()

    async def test_open_mints_and_reclaims_tab_id(self) -> None:
        a = await self.mgr.open(key="me0001", tab_id="tab-a")
        b = await self.mgr.open(key="me0001", tab_id="tab-a")
        c = await self.mgr.open(key="me0001")
        self.assertIs(a, b)
        self.assertEqual(a.tab_id, "tab-a")
        self.assertNotEqual(c.tab_id, "tab-a")
        self.assertEqual(len(self.mgr.tabs_payload()), 2)

    async def test_send_while_busy_stages(self) -> None:
        sess = await self.mgr.open(key="me0001", tab_id="t1")
        fut = await self.mgr.enqueue_send(
            tab_id="t1",
            cmd="ver",
            scheduler=self.sched,
            target=self.target,
        )
        self.assertIsNotNone(fut)
        staged = await self.mgr.enqueue_send(
            tab_id="t1",
            cmd="ota stats",
            scheduler=self.sched,
            target=self.target,
        )
        self.assertIsNone(staged)
        self.assertEqual([p.cmd for p in sess.pending], ["ota stats"])
        kinds = [j.kind for j in self.sched.units["me0001"].jobs]
        self.assertTrue(kinds[0].startswith("console:"))

    async def test_patch_and_delete_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="get name", scheduler=self.sched, target=self.target
        )
        sess = self.mgr.get("t1")
        assert sess is not None
        pid = sess.pending[0].id
        self.assertIsNotNone(self.mgr.patch_pending("t1", pid, "get lat"))
        self.assertEqual(sess.pending[0].cmd, "get lat")
        self.assertIsNotNone(self.mgr.delete_pending("t1", pid))
        self.assertEqual(sess.pending, [])

    async def test_cli_done_drains_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ota stats", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done("t1", ok=True, reply="v1", error=None)
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.history[-1]["cmd"], "ver")
        self.assertEqual(sess.state, "sending")
        self.assertEqual(sess.cmd, "ota stats")
        self.assertEqual(sess.pending, [])

    async def test_cancel_keeps_pending_and_drains(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="next", scheduler=self.sched, target=self.target
        )
        await self.mgr.cancel("t1", scheduler=self.sched, session=None)
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.history[-1]["error"], "cancelled")
        self.assertEqual(sess.cmd, "next")
        self.assertEqual(sess.state, "sending")

    async def test_timeout_does_not_drain(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ota stats", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="command timeout", drain=False
        )
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.state, "failed")
        self.assertEqual(sess.cmd, "ver")
        self.assertEqual(sess.error, "command timeout")
        self.assertEqual([p.cmd for p in sess.pending], ["ota stats"])
        self.assertEqual(sess.history, [])

    async def test_hard_fail_does_not_drain(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="next", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="failed", drain=False
        )
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.state, "failed")
        self.assertEqual([p.cmd for p in sess.pending], ["next"])

    async def test_send_while_failed_stages(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="command timeout", drain=False
        )
        staged = await self.mgr.enqueue_send(
            tab_id="t1", cmd="get name", scheduler=self.sched, target=self.target
        )
        self.assertIsNone(staged)
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.state, "failed")
        self.assertEqual(sess.cmd, "ver")
        self.assertEqual([p.cmd for p in sess.pending], ["get name"])

    async def test_retry_while_sending_keeps_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ota stats", scheduler=self.sched, target=self.target
        )
        retried = await self.mgr.retry(
            "t1", scheduler=self.sched, target=self.target
        )
        sess = self.mgr.get("t1")
        assert sess is not None and retried is not None
        self.assertEqual(sess.state, "sending")
        self.assertEqual(sess.cmd, "ver")
        self.assertEqual([p.cmd for p in sess.pending], ["ota stats"])

    async def test_retry_restarts_failed_cmd(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ota stats", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="command timeout", drain=False
        )
        retried = await self.mgr.retry(
            "t1", scheduler=self.sched, target=self.target
        )
        sess = self.mgr.get("t1")
        assert sess is not None and retried is not None
        self.assertEqual(sess.state, "sending")
        self.assertEqual(sess.cmd, "ver")
        self.assertIsNone(sess.error)
        self.assertEqual([p.cmd for p in sess.pending], ["ota stats"])

    async def test_skip_drains_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ota stats", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="command timeout", drain=False
        )
        await self.mgr.skip("t1")
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.history[-1]["cmd"], "ver")
        self.assertEqual(sess.history[-1]["error"], "command timeout")
        self.assertEqual(sess.state, "sending")
        self.assertEqual(sess.cmd, "ota stats")
        self.assertEqual(sess.pending, [])

    async def test_login_fail_drops_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="next", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done(
            "t1", ok=False, reply=None, error="login failed", drop_pending=True
        )
        sess = self.mgr.get("t1")
        assert sess is not None
        self.assertEqual(sess.pending, [])
        self.assertEqual(sess.state, "ready")

    async def test_two_tabs_same_unit_isolated(self) -> None:
        await self.mgr.open(key="me0001", tab_id="a")
        await self.mgr.open(key="me0001", tab_id="b")
        await self.mgr.enqueue_send(
            tab_id="a", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="b", cmd="get name", scheduler=self.sched, target=self.target
        )
        await self.mgr.on_cli_done("a", ok=True, reply="ok", error=None)
        a = self.mgr.get("a")
        b = self.mgr.get("b")
        assert a is not None and b is not None
        self.assertEqual(a.history[-1]["cmd"], "ver")
        self.assertEqual(b.history, [])
        self.assertEqual(b.state, "sending")

    async def test_close_drops_tab(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.close("t1", scheduler=self.sched)
        self.assertIsNone(self.mgr.get("t1"))
        self.assertEqual(self.mgr.tabs_payload(), [])

    async def test_hello_payload_includes_history_and_pending(self) -> None:
        await self.mgr.open(key="me0001", tab_id="t1")
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="ver", scheduler=self.sched, target=self.target
        )
        await self.mgr.enqueue_send(
            tab_id="t1", cmd="queued", scheduler=self.sched, target=self.target
        )
        poll = self.mgr.poll_console()
        self.assertEqual(len(poll["tabs"]), 1)
        tab = poll["tabs"][0]
        self.assertEqual(tab["tab_id"], "t1")
        self.assertEqual(tab["pending"][0]["cmd"], "queued")
        self.assertEqual(tab["state"], "sending")


class ConsoleHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_upserts_tabs(self) -> None:
        hub = FleetHub()
        await hub.replace_snapshot({"poll": {}, "units": {}})
        await hub.publish_console(
            {"tab_id": "t1", "key": "me0001", "state": "ready", "history": [], "pending": []}
        )
        tabs = hub.snapshot["poll"]["console"]["tabs"]
        self.assertEqual(len(tabs), 1)
        await hub.publish_console(
            {
                "tab_id": "t1",
                "key": "me0001",
                "state": "sending",
                "history": [{"cmd": "ver"}],
                "pending": [],
            }
        )
        self.assertEqual(hub.snapshot["poll"]["console"]["tabs"][0]["state"], "sending")
        await hub.publish_console({"tab_id": "t1", "key": "me0001", "state": "closed"})
        self.assertEqual(hub.snapshot["poll"]["console"]["tabs"], [])

