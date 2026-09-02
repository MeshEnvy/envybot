"""Manual Refresh / Pull / Push API."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.fleet_worker import build_manual_jobs
from envybot.history import open_history
from envybot.jobs import FleetScheduler
from envybot.poll import GET_GROUP_ORDER, refresh_due_groups
from envybot.radio import load_targets
from envybot.web.hub import FleetHub
from envybot.web.server import MonitorWeb
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
        self.scheduler = FleetScheduler()
        self.manual_keys: set[str] = set()
        self.conn = open_history(book)
        self.web_ctx = MonitorWeb(
            hub=FleetHub(),
            nodes_path=nodes_path,
            sites_path=sites_path,
            stale_secs=86400.0,
            url="http://127.0.0.1:8787/",
        )
        self.web_ctx.bind_scheduler(
            self.scheduler,
            manual_keys=self.manual_keys,
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

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_open_console_without_worker(self) -> None:
        status, err, payload = await self.web_ctx.open_console("me0003")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        assert payload is not None
        self.assertEqual(payload.get("state"), "ready")
        self.assertTrue(payload.get("tab_id"))
        self.assertIn("history", payload)
        self.assertIn("pending", payload)
        uq = self.scheduler.units.get("me0003")
        self.assertTrue(uq is None or not uq.jobs)

    async def test_refresh_while_console_open(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err, _payload = await self.web_ctx.open_console("me0003")
        self.assertEqual(status, 200)
        status, err = await self.web_ctx.enqueue_job("me0003", "refresh")
        self.assertEqual(status, 200)
        self.assertIsNone(err)

    async def test_enqueue_not_accepting(self) -> None:
        status, err = await self.web_ctx.enqueue_job("me0003", "refresh")
        self.assertEqual(status, 409)
        self.assertIn("not accepting", err or "")

    async def test_enqueue_unknown_unit(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue_job("me9999", "refresh")
        self.assertEqual(status, 404)
        self.assertEqual(err, "unknown unit")

    async def test_enqueue_puts_job_on_scheduler(self) -> None:
        self.web_ctx.set_worker_active(True)
        status, err = await self.web_ctx.enqueue_job("me0003", "pull")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        uq = self.scheduler.units.get("me0003")
        self.assertIsNotNone(uq)
        assert uq is not None
        self.assertTrue(uq.manual)
        self.assertEqual(uq.manual_job, "pull")

    async def test_enqueue_refresh_bumps_when_busy(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx._session_states["me0003"] = {"state": "polling"}
        status, err = await self.web_ctx.enqueue_job("me0003", "refresh")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        uq = self.scheduler.units.get("me0003")
        self.assertIsNotNone(uq)
        assert uq is not None
        self.assertEqual(uq.manual_job, "refresh")
        self.assertTrue(uq.manual)

    async def test_enqueue_push_bumps_when_busy(self) -> None:
        self.web_ctx.set_worker_active(True)
        self.web_ctx._session_states["me0003"] = {"state": "polling"}
        status, err = await self.web_ctx.enqueue_job("me0003", "push")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        uq = self.scheduler.units.get("me0003")
        self.assertIsNotNone(uq)
        assert uq is not None
        self.assertEqual(uq.manual_job, "push")
        self.assertTrue(any(j.kind.startswith("apply:") for j in uq.jobs))

    async def test_wait_for_work(self) -> None:
        from envybot.jobs import RadioJob
        from envybot.radio import RouterTarget

        sched = FleetScheduler()
        task = asyncio.create_task(sched.wait_for_work())
        await asyncio.sleep(0.02)
        t = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="Test",
            site=None,
            pubkey_hex="b" * 64,
            admin_password="x",
        )
        sched.enqueue_jobs(t, [RadioJob(kind="login", unit_key="me0003")])
        await asyncio.wait_for(task, timeout=1.0)


class ManualJobHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        book = Path(self.tmp.name)
        nodes_path, sites_path = _write_book(book)
        self.scheduler = FleetScheduler()
        self.web_ctx = MonitorWeb(
            hub=FleetHub(),
            nodes_path=nodes_path,
            sites_path=sites_path,
            stale_secs=86400.0,
            url="http://127.0.0.1:8787/",
        )
        self.web_ctx.bind_scheduler(
            self.scheduler,
            manual_keys=set(),
            conn=open_history(book),
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
        self.assertEqual(body.get("session", {}).get("stage"), "Logging in")
        uq = self.scheduler.units.get("me0003")
        self.assertIsNotNone(uq)
        assert uq is not None
        self.assertEqual(uq.manual_job, "refresh")

    async def test_post_pull_and_push(self) -> None:
        self.web_ctx.set_worker_active(True)
        resp = await self.client.post("/api/pull/me0003")
        self.assertEqual(resp.status, 200)
        uq = self.scheduler.units.get("me0003")
        assert uq is not None
        self.assertEqual(uq.manual_job, "pull")
        self.scheduler.units.pop("me0003", None)
        self.web_ctx._session_states.pop("me0003", None)
        resp = await self.client.post("/api/push/me0003")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("session", {}).get("state"), "pushing")
        uq = self.scheduler.units.get("me0003")
        assert uq is not None
        self.assertEqual(uq.manual_job, "push")

    async def test_post_unit_paused(self) -> None:
        resp = await self.client.post("/api/unit/me0003", json={"paused": True})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("paused"))
        resp = await self.client.post("/api/unit/me0003", json={"paused": False})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body.get("paused"))

    async def test_post_unit_alias_and_notes(self) -> None:
        resp = await self.client.post(
            "/api/unit/me0003",
            json={"alias": "Bench", "notes": "Spare tag\nBag shelf"},
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body.get("alias"), "Bench")
        self.assertEqual(body.get("notes"), "Spare tag\nBag shelf")
        resp = await self.client.post("/api/unit/me0003", json={"alias": "", "notes": ""})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertIsNone(body.get("alias"))
        self.assertIsNone(body.get("notes"))

    async def test_refresh_paused_unit(self) -> None:
        await self.client.post("/api/unit/me0003", json={"paused": True})
        self.web_ctx.set_worker_active(True)
        self.web_ctx.sync_worker_state(
            session_states={},
            poll={"phase": "idle", "accepting": True},
        )
        resp = await self.client.post("/api/refresh/me0003")
        self.assertEqual(resp.status, 200)
        uq = self.scheduler.units.get("me0003")
        assert uq is not None
        self.assertTrue(uq.manual)


class FleetManualJobBuildTests(unittest.TestCase):
    def test_refresh_pull_push_due_groups(self) -> None:
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
            target = load_targets(nodes_path, deployed_only=False, include={"me0003"})[0]
            refresh = build_manual_jobs(
                target, "refresh", do_poll=True, do_apply=True, apply_due=False, skip_discover=False
            )
            refresh_get = {j.kind for j in refresh if j.kind.startswith("get:")}
            for g in refresh_due_groups():
                if g == "neighbors":
                    self.assertIn("get:neighbors", refresh_get)
                else:
                    self.assertIn(f"get:{g}", refresh_get)
            pull = build_manual_jobs(
                target, "pull", do_poll=True, do_apply=True, apply_due=False, skip_discover=False
            )
            pull_kinds = [j.kind for j in pull if j.kind.startswith("get:")]
            self.assertIn("get:neighbors_discover", pull_kinds)
            push = build_manual_jobs(
                target, "push", do_poll=True, do_apply=True, apply_due=True, skip_discover=False
            )
            get_kinds = [j.kind for j in push if j.kind.startswith("get:")]
            self.assertEqual(get_kinds, [])
            apply_kinds = [j.kind for j in push if j.kind.startswith("apply:")]
            self.assertTrue(apply_kinds)


from envybot.web.server import make_app  # noqa: E402
