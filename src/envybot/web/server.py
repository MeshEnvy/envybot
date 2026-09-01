"""aiohttp server for the monitor web UI."""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from envybot.apply import apply_is_due
from envybot.fleet_worker import build_manual_jobs
from envybot.history import history_series, open_history, source_histories
from envybot.jobs import FleetScheduler
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import (
    is_decommissioned,
    is_meshcore_platform,
    load_nodes_doc,
    load_sites_for_book,
    write_nodes_doc,
)
from envybot.position import bind_node_to_site, load_sites_doc, write_sites_doc
from envybot.radio import load_targets
from envybot.web.hub import FleetHub
from envybot.web.snapshot import assert_no_secrets, build_fleet_snapshot, build_neighbor_edges

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


@dataclass
class FleetWebBinding:
    scheduler: FleetScheduler
    manual_keys: set[str]
    conn: Any
    nodes: dict[str, Any]
    sites: dict[str, Any]
    doc: dict[str, Any]
    keys: dict[str, list[str]]
    do_poll: bool
    do_apply: bool
    skip_discover: bool
    discover_wait: float


@dataclass
class MonitorWeb:
    """Running fleet web server + hub."""

    hub: FleetHub
    nodes_path: Path
    sites_path: Path
    stale_secs: float
    url: str
    runner: web.AppRunner | None = None
    site: web.TCPSite | None = None
    _watch_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _binding: FleetWebBinding | None = field(default=None, repr=False)
    _accepting: bool = field(default=False, repr=False)
    _session_states: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _poll_state: dict[str, Any] = field(default_factory=dict, repr=False)
    _companion: str | None = field(default=None, repr=False)

    def bind_scheduler(
        self,
        scheduler: FleetScheduler,
        *,
        manual_keys: set[str],
        conn: Any,
        nodes: dict[str, Any],
        sites: dict[str, Any],
        doc: dict[str, Any],
        keys: dict[str, list[str]],
        do_poll: bool,
        do_apply: bool,
        skip_discover: bool,
        discover_wait: float,
    ) -> None:
        self._binding = FleetWebBinding(
            scheduler=scheduler,
            manual_keys=manual_keys,
            conn=conn,
            nodes=nodes,
            sites=sites,
            doc=doc,
            keys=keys,
            do_poll=do_poll,
            do_apply=do_apply,
            skip_discover=skip_discover,
            discover_wait=discover_wait,
        )

    def set_worker_active(self, active: bool) -> None:
        self._accepting = active

    def sync_worker_state(
        self,
        *,
        session_states: dict[str, dict[str, Any]] | None = None,
        poll: dict[str, Any] | None = None,
        companion: str | None = None,
    ) -> None:
        if session_states is not None:
            self._session_states = session_states
        if poll is not None:
            self._poll_state = dict(poll)
        if companion is not None:
            self._companion = companion

    async def enqueue_job(self, key: str, job: str) -> tuple[int, str | None]:
        """Enqueue a manual Refresh, Pull, or Push. Returns (http_status, error)."""
        from envybot.poll import IN_FLIGHT_STATES, MANUAL_JOBS, in_flight_session

        key = key.lower()
        job = job.lower()
        if job not in MANUAL_JOBS:
            return 400, f"unknown job {job}"
        if not self._accepting:
            return 409, "fleet worker not accepting manual jobs"
        doc = load_nodes_doc(self.nodes_path)
        nodes = doc.get("nodes") or {}
        node = nodes.get(key)
        if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
            return 404, "unknown unit"
        session = self._session_states.get(key) or {}
        state = session.get("state")
        if state in IN_FLIGHT_STATES:
            return 200, None
        if self._binding is None:
            return 409, "fleet worker not accepting manual jobs"
        binding = self._binding
        targets = load_targets(
            self.nodes_path, deployed_only=False, include={key}, skip=None
        )
        if not targets:
            return 404, "unknown unit"
        target = targets[0]
        apply_due = binding.do_apply and apply_is_due(
            binding.conn,
            key,
            node,
            binding.sites,
            force=job == "push",
            doc=binding.doc,
            keys=binding.keys,
        )
        jobs = build_manual_jobs(
            target,
            job,
            do_poll=binding.do_poll,
            do_apply=binding.do_apply,
            apply_due=apply_due,
            skip_discover=binding.skip_discover,
            discover_wait=binding.discover_wait,
        )
        status, err = binding.scheduler.enqueue_manual(target, job, jobs)
        binding.manual_keys.add(key)
        self._session_states[key] = in_flight_session(
            manual_job=job,
            job_kind="login",
            queued=True,
        )
        return status, err

    async def refresh_snapshot(
        self,
        *,
        session_states: dict[str, dict[str, Any]] | None = None,
        companion: str | None = None,
        poll: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session_states is not None:
            self._session_states = session_states
        if poll is not None:
            self._poll_state = dict(poll)
        if companion is not None:
            self._companion = companion
        snap = build_fleet_snapshot(
            nodes_path=self.nodes_path,
            sites_path=self.sites_path,
            stale_secs=self.stale_secs,
            session_states=self._session_states,
            companion=self._companion,
            poll=self._poll_state or poll or {"phase": "idle"},
        )
        snap["edges"] = build_neighbor_edges(snap["units"])
        await self.hub.set_snapshot(snap)
        return snap

    async def publish_unit(
        self,
        key: str,
        *,
        session: dict[str, Any],
        session_states: dict[str, dict[str, Any]] | None = None,
        companion: str | None = None,
        poll: dict[str, Any] | None = None,
        sample: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        if session_states is not None:
            self._session_states = session_states
        if poll is not None:
            self._poll_state = dict(poll)
        if companion is not None:
            self._companion = companion
        snap = build_fleet_snapshot(
            nodes_path=self.nodes_path,
            sites_path=self.sites_path,
            stale_secs=self.stale_secs,
            session_states=self._session_states,
            companion=self._companion,
            poll=self._poll_state or poll or {"phase": "idle"},
        )
        snap["edges"] = build_neighbor_edges(snap["units"])
        unit = snap["units"].get(key)
        if unit is None:
            return
        unit = dict(unit)
        unit["session"] = session
        if sample is not None:
            source, row = sample
            unit["sample"] = {"source": source, "row": row}
        snap["units"][key] = unit
        await self.hub.replace_snapshot(snap)
        await self.hub.publish_unit(unit)

    async def publish_session(self, poll: dict[str, Any]) -> None:
        self._poll_state = dict(poll)
        await self.hub.publish_session(poll)

    async def start_yaml_watch(self, interval: float = 1.0) -> None:
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch_yaml(interval))

    async def _watch_yaml(self, interval: float) -> None:
        last_mtime: float | None = None
        while not self._stop.is_set():
            try:
                mtime = self.nodes_path.stat().st_mtime
                try:
                    mtime = max(mtime, self.sites_path.stat().st_mtime)
                except OSError:
                    pass
                if last_mtime is None or mtime != last_mtime:
                    last_mtime = mtime
                    poll = dict(self._poll_state) if self._poll_state else {"phase": "watch"}
                    await self.refresh_snapshot(
                        session_states=dict(self._session_states),
                        companion=self._companion,
                        poll=poll,
                    )
            except OSError:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def serve_forever(self) -> None:
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            pass

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.request_stop()
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
        if self.runner is not None:
            await self.runner.cleanup()


async def _handle_fleet(request: web.Request) -> web.Response:
    hub: FleetHub = request.app["hub"]
    if hub.snapshot is None:
        web_ctx: MonitorWeb = request.app["web_ctx"]
        await web_ctx.refresh_snapshot()
    return web.json_response(hub.snapshot or {})


async def _handle_events(request: web.Request) -> web.StreamResponse:
    hub: FleetHub = request.app["hub"]
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    async def stream() -> None:
        try:
            async for event, data in hub.subscribe():
                await response.write(FleetHub.sse_format(event, data).encode("utf-8"))
                await response.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass

    task = asyncio.create_task(stream())
    try:
        while not task.done():
            try:
                await response.write(FleetHub.sse_comment("keepalive").encode("utf-8"))
                await response.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except asyncio.TimeoutError:
                continue
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return response


async def _handle_history(request: web.Request) -> web.Response:
    unit = request.match_info["unit"]
    metric = request.query.get("metric") or "battery_mv"
    hours_raw = request.query.get("hours")
    hours = int(hours_raw) if hours_raw and hours_raw.isdigit() else 72
    web_ctx: MonitorWeb = request.app["web_ctx"]
    conn = open_history(web_ctx.nodes_path.parent)
    try:
        series = history_series(conn, unit, metric, hours=hours)
    finally:
        conn.close()
    payload = {"unit": unit, "metric": metric, "hours": hours, "points": series}
    assert_no_secrets(payload)
    return web.json_response(payload)


async def _handle_polls(request: web.Request) -> web.Response:
    unit = request.match_info["unit"]
    hours_raw = request.query.get("hours")
    limit_raw = request.query.get("limit")
    hours = int(hours_raw) if hours_raw and hours_raw.isdigit() else 72
    limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else 48
    web_ctx: MonitorWeb = request.app["web_ctx"]
    conn = open_history(web_ctx.nodes_path.parent)
    try:
        histories = source_histories(conn, unit, hours=hours, limit=limit)
    finally:
        conn.close()
    payload = {"unit": unit, "hours": hours, "histories": histories}
    assert_no_secrets(payload)
    return web.json_response(payload)


async def _handle_manual_job(request: web.Request, job: str) -> web.Response:
    key = request.match_info["key"].lower()
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err = await web_ctx.enqueue_job(key, job)
    if status == 404:
        return web.json_response({"error": err}, status=404)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    if status == 400:
        return web.json_response({"error": err}, status=400)
    session = dict(web_ctx._session_states.get(key) or {})
    prev = ((web_ctx.hub.snapshot or {}).get("units") or {}).get(key) or {"key": key}
    unit = dict(prev)
    unit["session"] = session
    asyncio.create_task(
        web_ctx.publish_unit(
            key,
            session=session,
            session_states=web_ctx._session_states,
            companion=web_ctx._companion,
            poll=web_ctx._poll_state,
        )
    )
    return web.json_response(unit)


async def _handle_unit_edit(request: web.Request) -> web.Response:
    key = request.match_info["key"].lower()
    web_ctx: MonitorWeb = request.app["web_ctx"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "object required"}, status=400)
    doc = load_nodes_doc(web_ctx.nodes_path)
    nodes = doc.get("nodes") or {}
    node = nodes.get(key)
    if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
        return web.json_response({"error": "unknown unit"}, status=404)
    if "public" in body:
        if body["public"] is True:
            node["public"] = True
        else:
            node.pop("public", None)
    if "paused" in body:
        if body["paused"] is True:
            node["paused"] = True
        else:
            node.pop("paused", None)
    if "alias" in body:
        alias = body["alias"]
        if isinstance(alias, str) and alias.strip():
            node["alias"] = alias.strip()
        else:
            node.pop("alias", None)
    if "notes" in body:
        notes = body["notes"]
        if isinstance(notes, str) and notes.strip():
            node["notes"] = notes
        else:
            node.pop("notes", None)
    if "site" in body:
        site = body["site"]
        sites_doc = load_sites_doc(web_ctx.sites_path)
        sites = sites_doc.get("sites") or {}
        if site is None or site == "":
            bind_node_to_site(sites, key, None)
            write_sites_doc(web_ctx.sites_path, sites_doc)
        elif isinstance(site, str):
            slug = site.strip()
            if slug not in sites:
                return web.json_response({"error": f"unknown site {slug}"}, status=400)
            bind_node_to_site(sites, key, slug)
            write_sites_doc(web_ctx.sites_path, sites_doc)
    node.pop("site", None)
    node.pop("lat", None)
    node.pop("lon", None)
    write_nodes_doc(web_ctx.nodes_path, doc)
    snap = await web_ctx.refresh_snapshot()
    unit = snap["units"].get(key) or {}
    return web.json_response(unit)


async def _handle_index(_request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@web.middleware
async def no_cache_js_middleware(request: web.Request, handler):
    response = await handler(request)
    if request.path.startswith("/js/") or request.path.startswith("/css/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def make_app(web_ctx: MonitorWeb) -> web.Application:
    app = web.Application(middlewares=[no_cache_js_middleware])
    app["hub"] = web_ctx.hub
    app["web_ctx"] = web_ctx
    app.router.add_get("/api/fleet", _handle_fleet)
    app.router.add_post("/api/unit/{key}", _handle_unit_edit)
    app.router.add_post("/api/refresh/{key}", lambda r: _handle_manual_job(r, "refresh"))
    app.router.add_post("/api/pull/{key}", lambda r: _handle_manual_job(r, "pull"))
    app.router.add_post("/api/push/{key}", lambda r: _handle_manual_job(r, "push"))
    app.router.add_get("/api/history/{unit}", _handle_history)
    app.router.add_get("/api/polls/{unit}", _handle_polls)
    app.router.add_get("/events", _handle_events)
    app.router.add_get("/", _handle_index)
    app.router.add_get("/index.html", _handle_index)
    app.router.add_static("/css", STATIC_DIR / "css")
    app.router.add_static("/js", STATIC_DIR / "js")
    return app


async def create_monitor_web(
    *,
    nodes_path: Path,
    sites_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stale_secs: float = 86400.0,
) -> MonitorWeb:
    book_dir = nodes_path.parent
    if sites_path is None:
        sites_path = book_dir / "sites.yaml"
    hub = FleetHub()
    web_ctx = MonitorWeb(
        hub=hub,
        nodes_path=nodes_path,
        sites_path=sites_path,
        stale_secs=stale_secs,
        url=f"http://{host}:{port}/",
    )
    runner = web.AppRunner(make_app(web_ctx))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    web_ctx.runner = runner
    web_ctx.site = site
    return web_ctx


async def start_monitor_web(
    *,
    nodes_path: Path,
    sites_path: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stale_secs: float = 86400.0,
    open_browser: bool = False,
) -> MonitorWeb:
    web_ctx = await create_monitor_web(
        nodes_path=nodes_path,
        sites_path=sites_path,
        host=host,
        port=port,
        stale_secs=stale_secs,
    )
    await web_ctx.refresh_snapshot()
    print(f"Fleet UI: {web_ctx.url}")
    if open_browser:
        webbrowser.open(web_ctx.url)
    return web_ctx
