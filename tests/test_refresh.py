"""Manual Refresh / Pull / Push API."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.web.hub import FleetHub
from envybot.web.server import MonitorWeb, make_app
from aiohttp.test_utils import TestClient, TestServer


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


class MonitorWebManualJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        book = Path(self.tmp.name)
        nodes_path, sites_path = _write_book(book)
        self.web_ctx = MonitorWeb(
            hub=FleetHub(),
            nodes_path=nodes_path,
            sites_path=sites_path,
            stale_secs=86400.0,
            url="http://127.0.0.1:8787/",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_enqueue_not_accepting(self) -> None:
        status, err = await self.web_ctx.enqueue_job("me0003", "refresh")
        self.assertEqual(status, 409)
        self.assertIn("not accepting", err or "")

    async def test_enqueue_unknown_unit(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue_job("me9999", "refresh")
        self.assertEqual(status, 404)
        self.assertEqual(err, "unknown unit")

    async def test_enqueue_puts_job_on_queue(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue_job("me0003", "pull")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        jobs = await self.web_ctx.drain_manual_queue()
        self.assertEqual(jobs, [("me0003", "pull")])

    async def test_enqueue_noop_when_busy(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx._session_states["me0003"] = {"state": "refreshing"}
        status, err = await self.web_ctx.enqueue_job("me0003", "refresh")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(await self.web_ctx.drain_manual_queue(), [])

    async def test_drain_dedupes(self) -> None:
        self.web_ctx._manual_queue.put_nowait(("me0003", "refresh"))
        self.web_ctx._manual_queue.put_nowait(("me0003", "pull"))
        jobs = await self.web_ctx.drain_manual_queue()
        self.assertEqual(jobs, [("me0003", "refresh")])

    async def test_wait_manual_queue(self) -> None:
        self.web_ctx.set_worker_active(True)

        async def enqueue_later() -> None:
            await asyncio.sleep(0.05)
            self.web_ctx._manual_queue.put_nowait(("me0003", "push"))
            self.web_ctx._wake_idle.set()

        task = asyncio.create_task(enqueue_later())
        jobs = await self.web_ctx.wait_manual_queue()
        await task
        self.assertEqual(jobs, [("me0003", "push")])


class ManualJobHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        book = Path(self.tmp.name)
        nodes_path, sites_path = _write_book(book)
        self.web_ctx = MonitorWeb(
            hub=FleetHub(),
            nodes_path=nodes_path,
            sites_path=sites_path,
            stale_secs=86400.0,
            url="http://127.0.0.1:8787/",
        )
        await self.web_ctx.refresh_snapshot()
        app = make_app(self.web_ctx)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    async def test_post_refresh_409_when_idle(self) -> None:
        resp = await self.client.post("/api/refresh/me0003")
        self.assertEqual(resp.status, 409)

    async def test_post_refresh_marks_refreshing(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx.sync_worker_state(
            session_states={},
            poll={"phase": "idle", "accepting": True},
        )
        resp = await self.client.post("/api/refresh/me0003")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("session", {}).get("state"), "refreshing")
        jobs = await self.web_ctx.drain_manual_queue()
        self.assertEqual(jobs, [("me0003", "refresh")])

    async def test_post_pull_and_push(self) -> None:
        self.web_ctx.set_worker_active(True)
        resp = await self.client.post("/api/pull/me0003")
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            (await self.web_ctx.drain_manual_queue()),
            [("me0003", "pull")],
        )
        self.web_ctx._session_states.pop("me0003", None)
        resp = await self.client.post("/api/push/me0003")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("session", {}).get("state"), "pushing")
        self.assertEqual(
            (await self.web_ctx.drain_manual_queue()),
            [("me0003", "push")],
        )

    async def test_post_unit_paused(self) -> None:
        resp = await self.client.post("/api/unit/me0003", json={"paused": True})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("paused"))
        resp = await self.client.post("/api/unit/me0003", json={"paused": False})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body.get("paused"))

    async def test_refresh_paused_unit(self) -> None:
        await self.client.post("/api/unit/me0003", json={"paused": True})
        self.web_ctx.set_worker_active(True)
        self.web_ctx.sync_worker_state(
            session_states={},
            poll={"phase": "idle", "accepting": True},
        )
        resp = await self.client.post("/api/refresh/me0003")
        self.assertEqual(resp.status, 200)
        jobs = await self.web_ctx.drain_manual_queue()
        self.assertEqual(jobs, [("me0003", "refresh")])


class FleetManualJobTargetsTests(unittest.TestCase):
    def test_refresh_pull_push_due_groups(self) -> None:
        from ruamel.yaml import YAML

        from envybot.commands.fleet import _manual_job_targets
        from envybot.history import open_history
        from envybot.poll import GET_GROUP_ORDER, refresh_due_groups

        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            yaml = YAML()
            with nodes_path.open("w") as fh:
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
            conn = open_history(book)
            apply_keys: set[str] = set()
            manual_keys: set[str] = set()
            manual_jobs: dict[str, str] = {}
            session_states: dict[str, dict] = {}
            refresh = _manual_job_targets(
                [("me0003", "refresh")],
                nodes_path=nodes_path,
                conn=conn,
                nodes={"me0003": {}},
                sites={},
                doc={"nodes": {"me0003": {}}},
                keys={},
                session_states=session_states,
                apply_keys=apply_keys,
                manual_keys=manual_keys,
                manual_jobs=manual_jobs,
                do_poll=True,
                do_apply=True,
                attempt_counts={},
            )
            self.assertEqual(refresh[0].due_groups, refresh_due_groups())
            self.assertEqual(manual_jobs["me0003"], "refresh")
            pull = _manual_job_targets(
                [("me0003", "pull")],
                nodes_path=nodes_path,
                conn=conn,
                nodes={"me0003": {}},
                sites={},
                doc={"nodes": {"me0003": {}}},
                keys={},
                session_states=session_states,
                apply_keys=apply_keys,
                manual_keys=manual_keys,
                manual_jobs=manual_jobs,
                do_poll=True,
                do_apply=True,
                attempt_counts={},
            )
            self.assertEqual(pull[0].due_groups, list(GET_GROUP_ORDER))
            push = _manual_job_targets(
                [("me0003", "push")],
                nodes_path=nodes_path,
                conn=conn,
                nodes={"me0003": {}},
                sites={},
                doc={"nodes": {"me0003": {}}},
                keys={},
                session_states=session_states,
                apply_keys=apply_keys,
                manual_keys=manual_keys,
                manual_jobs=manual_jobs,
                do_poll=True,
                do_apply=True,
                attempt_counts={},
            )
            self.assertEqual(push[0].due_groups, [])
            self.assertIn("me0003", apply_keys)
