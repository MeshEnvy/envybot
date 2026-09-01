"""Manual fleet queue API."""

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


class MonitorWebQueueTests(unittest.IsolatedAsyncioTestCase):
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
        status, err = await self.web_ctx.enqueue("me0003")
        self.assertEqual(status, 409)
        self.assertIn("not accepting", err or "")

    async def test_enqueue_unknown_unit(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue("me9999")
        self.assertEqual(status, 404)
        self.assertEqual(err, "unknown unit")

    async def test_enqueue_puts_key_on_queue(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue("me0003")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        keys = await self.web_ctx.drain_manual_queue()
        self.assertEqual(keys, ["me0003"])

    async def test_enqueue_noop_when_already_queued(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx._session_states["me0003"] = {"state": "queued"}
        status, err = await self.web_ctx.enqueue("me0003")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(await self.web_ctx.drain_manual_queue(), [])

    async def test_drain_dedupes(self) -> None:
        self.web_ctx._manual_queue.put_nowait("me0003")
        self.web_ctx._manual_queue.put_nowait("me0003")
        keys = await self.web_ctx.drain_manual_queue()
        self.assertEqual(keys, ["me0003"])

    async def test_wait_manual_queue(self) -> None:
        self.web_ctx.set_worker_active(True)

        async def enqueue_later() -> None:
            await asyncio.sleep(0.05)
            self.web_ctx._manual_queue.put_nowait("me0003")
            self.web_ctx._wake_idle.set()

        task = asyncio.create_task(enqueue_later())
        keys = await self.web_ctx.wait_manual_queue()
        await task
        self.assertEqual(keys, ["me0003"])


class QueueHandlerTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_post_queue_409_when_idle(self) -> None:
        resp = await self.client.post("/api/queue/me0003")
        self.assertEqual(resp.status, 409)

    async def test_post_queue_marks_queued(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx.sync_worker_state(
            session_states={},
            poll={"phase": "idle", "accepting": True},
        )
        resp = await self.client.post("/api/queue/me0003")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("session", {}).get("state"), "queued")
        keys = await self.web_ctx.drain_manual_queue()
        self.assertEqual(keys, ["me0003"])
