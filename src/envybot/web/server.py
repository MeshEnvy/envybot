"""aiohttp server for the monitor web UI."""

from __future__ import annotations

import asyncio
import contextlib
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from envybot.apply import apply_is_due
from envybot.routing import (
    ForcedPath,
    parse_path_paste,
    path_pin_fields_from_extra,
)
from envybot.fleet_worker import build_manual_jobs
from envybot.history import (
    get_last_seen,
    history_series,
    list_mesh_audit,
    open_history,
    source_histories,
)
from envybot.jobs import FleetScheduler
from envybot.keys_doc import keys_path, load_keys
from envybot.nodes_doc import (
    is_decommissioned,
    is_meshcore_platform,
    load_nodes_doc,
    load_sites_for_book,
    write_nodes_doc,
)
from envybot.position import (
    bench_loc_from_doc,
    bind_node_to_site,
    is_placeholder_gps,
    load_sites_doc,
    site_loc_for_unit,
    write_sites_doc,
)
from envybot.sun import attach_sun, sun_series
from envybot.weather import attach_weather
from envybot.radio import load_targets
from envybot.web.console import ConsoleManager
from envybot.web.hub import FleetHub
from envybot.web.snapshot import (
    assert_no_secrets,
    build_fleet_snapshot,
    build_neighbor_edges,
    reported_locs_from_contacts,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"


def _attach_ingestor(snap: dict[str, Any], nodes_path: Path) -> None:
    doc = load_nodes_doc(nodes_path)
    loc = bench_loc_from_doc(doc)
    if loc is None:
        snap["ingestor"] = None
    else:
        snap["ingestor"] = {"lat": loc[0], "lon": loc[1]}
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
    console: ConsoleManager = field(default_factory=ConsoleManager, repr=False)
    _reported_locs: dict[str, tuple[float, float]] = field(default_factory=dict, repr=False)
    _include: set[str] | None = field(default=None, repr=False)
    _skip: set[str] | None = field(default=None, repr=False)

    def unit_visible(self, key: str) -> bool:
        from envybot.unit_filter import key_in_unit_filter

        return key_in_unit_filter(key, include=self._include, skip=self._skip)

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

    async def enqueue_job(
        self,
        key: str,
        job: str,
        *,
        stage_selector: str | None = None,
    ) -> tuple[int, str | None]:
        """Enqueue a manual fleet job. Returns (http_status, error)."""
        from envybot.history import latest_ota
        from envybot.poll import MANUAL_JOBS, in_flight_session

        key = key.lower()
        job = job.lower()
        if job not in MANUAL_JOBS:
            return 400, f"unknown job {job}"
        if not self.unit_visible(key):
            return 404, "unknown unit"
        if not self._accepting:
            return 409, "fleet worker not accepting manual jobs"
        doc = load_nodes_doc(self.nodes_path)
        nodes = doc.get("nodes") or {}
        node = nodes.get(key)
        if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
            return 404, "unknown unit"
        if self._binding is None:
            return 409, "fleet worker not accepting manual jobs"
        binding = self._binding
        if job == "install":
            ota = latest_ota(binding.conn, key)
            local = (ota or {}).get("local") or {}
            if local.get("state") != "ready":
                return 400, "OTA not ready to install (stage a pull first)"
        if job == "stage" and not (stage_selector or "").strip():
            return 400, "missing catalog index or mid for stage"
        targets = load_targets(
            self.nodes_path,
            deployed_only=False,
            include={key},
            skip=self._skip,
        )
        if not targets:
            return 404, "unknown unit"
        target = targets[0]
        apply_due = binding.do_apply and apply_is_due(
            binding.conn,
            key,
            node,
            binding.sites,
            force=False,
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
            stage_mid=stage_selector,
            seen=get_last_seen(binding.conn, key) if binding.conn else None,
        )
        if job == "stage" and not jobs:
            return 400, "missing catalog index or mid for stage"
        status, err = binding.scheduler.enqueue_manual(target, job, jobs)
        binding.manual_keys.add(key)
        self._session_states[key] = in_flight_session(
            manual_job=job,
            job_kind="login",
            queued=True,
        )
        return status, err

    async def enqueue_path_pin(
        self,
        key: str,
        forced: ForcedPath,
    ) -> tuple[int, str | None]:
        from envybot.poll import in_flight_session

        key = key.lower()
        if not self.unit_visible(key):
            return 404, "unknown unit"
        if not self._accepting or self._binding is None:
            return 409, "fleet worker not accepting manual jobs"
        status, err, target = self._load_target(key)
        if status != 200 or target is None:
            return status, err or "unknown unit"
        binding = self._binding
        status, err = binding.scheduler.enqueue_path_job(
            target,
            kind="path:pin",
            forced_extra=forced.to_extra(),
        )
        if status != 200:
            return status, err
        binding.manual_keys.add(key)
        sess = in_flight_session(
            manual_job="path",
            job_kind="path:pin",
            queued=True,
        )
        sess.update(path_pin_fields_from_extra({"forced_path": forced.to_extra()}))
        self._session_states[key] = sess
        return 200, None

    async def enqueue_path_clear(self, key: str) -> tuple[int, str | None]:
        from envybot.poll import in_flight_session

        key = key.lower()
        if not self.unit_visible(key):
            return 404, "unknown unit"
        if not self._accepting or self._binding is None:
            return 409, "fleet worker not accepting manual jobs"
        status, err, target = self._load_target(key)
        if status != 200 or target is None:
            return status, err or "unknown unit"
        binding = self._binding
        status, err = binding.scheduler.enqueue_path_job(target, kind="path:clear")
        if status != 200:
            return status, err
        binding.manual_keys.add(key)
        self._session_states[key] = in_flight_session(
            manual_job="path",
            job_kind="path:clear",
            queued=True,
        )
        return 200, None

    def _load_target(
        self, key: str, *, require_worker: bool = True
    ) -> tuple[int, str | None, Any | None]:
        from envybot.nodes_doc import is_decommissioned, is_meshcore_platform

        key = key.lower()
        if not self.unit_visible(key):
            return 404, "unknown unit", None
        if require_worker:
            if not self._accepting:
                return 409, "fleet worker not accepting manual jobs", None
            if self._binding is None:
                return 409, "fleet worker not accepting manual jobs", None
        doc = load_nodes_doc(self.nodes_path)
        nodes = doc.get("nodes") or {}
        node = nodes.get(key)
        if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
            return 404, "unknown unit", None
        targets = load_targets(
            self.nodes_path,
            deployed_only=False,
            include={key},
            skip=self._skip,
        )
        if not targets:
            return 404, "unknown unit", None
        return 200, None, targets[0]

    def _attach_console(self, poll: dict[str, Any] | None = None) -> dict[str, Any]:
        out = dict(poll if poll is not None else self._poll_state)
        out["console"] = self.console.poll_console()
        return out

    async def open_console(
        self, key: str, tab_id: str | None = None
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        status, err, target = self._load_target(key, require_worker=False)
        if status != 200 or target is None:
            return status, err, None
        binding = self._binding
        max_attempts = 10
        scheduler = None
        if binding is not None:
            scheduler = binding.scheduler
            max_attempts = binding.scheduler.max_attempts or 10
        sess = await self.console.open(
            key=key,
            tab_id=tab_id,
            scheduler=scheduler,
            max_attempts=max_attempts,
        )
        await self.refresh_snapshot(
            session_states=dict(self._session_states),
            companion=self._companion,
            poll=self._attach_console(),
        )
        return 200, None, sess.to_event()

    async def send_console(
        self, tab_id: str, cmd: str
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        cmd = cmd.strip()
        if not cmd:
            return 400, "empty command", None
        sess = self.console.get(tab_id)
        if sess is None:
            return 409, "console tab not open", None
        status, err, target = self._load_target(sess.key)
        if status != 200 or target is None or self._binding is None:
            return status, err, None
        fut = await self.console.enqueue_send(
            tab_id=tab_id,
            cmd=cmd,
            scheduler=self._binding.scheduler,
            target=target,
            session=getattr(self, "_fleet_session", None),
        )
        if fut is None:
            live = self.console.get(tab_id)
            return 200, None, {"ok": True, "staged": True, **(live.to_event() if live else {})}
        try:
            result = await fut
        except Exception as exc:
            return 500, str(exc), None
        if isinstance(result, str):
            result = {"ok": True, "reply": result, "error": None}
        if result.get("cancelled"):
            return 200, None, {"ok": False, "error": "cancelled"}
        return 200, None, {
            "ok": bool(result.get("ok", True)),
            "reply": result.get("reply"),
            "error": result.get("error"),
        }

    async def patch_console_pending(
        self, tab_id: str, pending_id: str, cmd: str
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        cmd = cmd.strip()
        if not cmd:
            return 400, "empty command", None
        sess = self.console.patch_pending(tab_id, pending_id, cmd)
        if sess is None:
            return 409, "console tab not open", None
        await self.hub.publish_console(sess.to_event())
        return 200, None, sess.to_event()

    async def delete_console_pending(
        self, tab_id: str, pending_id: str | None
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        sess = self.console.delete_pending(tab_id, pending_id)
        if sess is None:
            return 409, "console tab not open", None
        await self.hub.publish_console(sess.to_event())
        return 200, None, sess.to_event()

    async def clear_console_history(
        self, tab_id: str
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        sess = self.console.clear_history(tab_id)
        if sess is None:
            return 409, "console tab not open", None
        await self.hub.publish_console(sess.to_event())
        return 200, None, sess.to_event()

    async def cancel_console(self, tab_id: str) -> tuple[int, str | None]:
        if self.console.get(tab_id) is None:
            return 409, "console tab not open"
        session = getattr(self, "_fleet_session", None)
        scheduler = self._binding.scheduler if self._binding is not None else None
        await self.console.cancel(tab_id, scheduler=scheduler, session=session)
        return 200, None

    async def park_console(
        self, tab_id: str, cmd: str, error: str | None
    ) -> tuple[int, str | None, dict[str, Any] | None]:
        sess = self.console.get(tab_id)
        if sess is None:
            return 409, "console tab not open", None
        _status, _err, target = self._load_target(sess.key, require_worker=False)
        scheduler = self._binding.scheduler if self._binding is not None else None
        parked = await self.console.park(
            tab_id,
            cmd=cmd,
            error=error,
            scheduler=scheduler,
            target=target,
            session=getattr(self, "_fleet_session", None),
        )
        return 200, None, parked.to_event() if parked else None

    async def retry_console(self, tab_id: str) -> tuple[int, str | None, dict[str, Any] | None]:
        sess = self.console.get(tab_id)
        if sess is None:
            return 409, "console tab not open", None
        if sess.state not in ("failed", "sending") or not sess.cmd:
            return 409, "nothing to retry", None
        status, err, target = self._load_target(sess.key)
        if status != 200 or target is None or self._binding is None:
            return status, err or "radio not bound", None
        retried = await self.console.retry(
            tab_id,
            scheduler=self._binding.scheduler,
            target=target,
            session=getattr(self, "_fleet_session", None),
        )
        return 200, None, retried.to_event() if retried else None

    async def skip_console(self, tab_id: str) -> tuple[int, str | None, dict[str, Any] | None]:
        sess = self.console.get(tab_id)
        if sess is None:
            return 409, "console tab not open", None
        if sess.state != "failed":
            return 409, "nothing to skip", None
        _status, _err, target = self._load_target(sess.key, require_worker=False)
        skipped = await self.console.skip(
            tab_id,
            scheduler=self._binding.scheduler if self._binding is not None else None,
            target=target,
            session=getattr(self, "_fleet_session", None),
        )
        return 200, None, skipped.to_event() if skipped else None

    async def close_console(self, tab_id: str) -> tuple[int, str | None]:
        if self.console.get(tab_id) is None:
            return 409, "console tab not open"
        session = getattr(self, "_fleet_session", None)
        scheduler = self._binding.scheduler if self._binding is not None else None
        await self.console.close(tab_id, scheduler=scheduler, session=session)
        await self.refresh_snapshot(
            session_states=dict(self._session_states),
            companion=self._companion,
            poll=self._attach_console(),
        )
        return 200, None

    def _live_reported_locs(self) -> dict[str, tuple[float, float]] | None:
        session = getattr(self, "_fleet_session", None)
        client = getattr(session, "_recovery_client", None) if session else None
        live = reported_locs_from_contacts(getattr(client, "contacts", None) if client else None)
        if live:
            self._reported_locs = live
            return live
        return self._reported_locs or None

    def _merge_scheduler_path_pins(self, snap: dict[str, Any]) -> None:
        binding = self._binding
        if binding is None:
            return
        units = snap.get("units") or {}
        for key, uq in binding.scheduler.units.items():
            unit = units.get(key)
            if unit is None:
                continue
            unit.update(path_pin_fields_from_extra(uq.session_extra))

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
            reported_locs=self._live_reported_locs(),
            include=self._include,
            skip=self._skip,
        )
        snap["edges"] = build_neighbor_edges(snap["units"])
        _attach_ingestor(snap, self.nodes_path)
        snap.setdefault("poll", {})["console"] = self.console.poll_console()
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
            reported_locs=self._live_reported_locs(),
            include=self._include,
            skip=self._skip,
        )
        snap["edges"] = build_neighbor_edges(snap["units"])
        _attach_ingestor(snap, self.nodes_path)
        snap.setdefault("poll", {})["console"] = self.console.poll_console()
        unit = snap["units"].get(key)
        if unit is None:
            return
        unit = dict(unit)
        unit["session"] = session
        if session.get("live_route"):
            unit["live_route"] = session["live_route"]
            label = session["live_route"].get("label")
            if label:
                from envybot.routing import abbrev_live_route_label

                unit["live_route_label"] = abbrev_live_route_label(str(label))
        if sample is not None:
            source, row = sample
            unit["sample"] = {"source": source, "row": row}
        snap["units"][key] = unit
        await self.hub.replace_snapshot(snap)
        await self.hub.publish_unit(unit)
        if poll is not None:
            await self.hub.publish_session(self._poll_state)

    async def publish_session(self, poll: dict[str, Any]) -> None:
        self._poll_state = dict(poll)
        await self.hub.publish_session(poll)

    async def publish_audit(self, event: dict[str, Any]) -> None:
        assert_no_secrets(event)
        await self.hub.publish_audit(event)

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
    if not web_ctx.unit_visible(unit):
        return web.json_response({"error": "unknown unit"}, status=404)
    conn = open_history(web_ctx.nodes_path.parent)
    try:
        series = history_series(conn, unit, metric, hours=hours)
    finally:
        conn.close()
    payload = {"unit": unit, "metric": metric, "hours": hours, "points": series}
    assert_no_secrets(payload)
    return web.json_response(payload)


def _site_loc_for_polls(web_ctx: MonitorWeb, unit: str) -> tuple[float, float] | None:
    """Site or bench_loc GPS for sun on history rows that predate loc logging."""
    binding = web_ctx._binding
    if binding is not None:
        node = binding.nodes.get(unit)
        return site_loc_for_unit(
            unit,
            node if isinstance(node, dict) else None,
            binding.sites,
            doc=binding.doc,
        )
    doc = load_nodes_doc(web_ctx.nodes_path)
    nodes = doc.get("nodes") or {}
    node = nodes.get(unit)
    return site_loc_for_unit(
        unit,
        node if isinstance(node, dict) else None,
        load_sites_for_book(web_ctx.nodes_path),
        doc=doc,
    )


async def _handle_polls(request: web.Request) -> web.Response:
    unit = request.match_info["unit"]
    hours_raw = request.query.get("hours")
    limit_raw = request.query.get("limit")
    hours = int(hours_raw) if hours_raw and hours_raw.isdigit() else 72
    limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else 80
    web_ctx: MonitorWeb = request.app["web_ctx"]
    if not web_ctx.unit_visible(unit):
        return web.json_response({"error": "unknown unit"}, status=404)
    conn = open_history(web_ctx.nodes_path.parent)
    try:
        histories = source_histories(conn, unit, hours=hours, limit=limit)
        loc = _site_loc_for_polls(web_ctx, unit)
        attach_sun(histories.get("status"), loc=loc)
        attach_sun(histories.get("telemetry"), loc=loc)
        attach_sun(histories.get("polls"), loc=loc)
        attach_weather(conn, histories.get("status"), loc=loc)
        attach_weather(conn, histories.get("telemetry"), loc=loc)
        attach_weather(conn, histories.get("polls"), loc=loc)
        now = int(time.time())
        histories["sun"] = (
            sun_series(loc[0], loc[1], now - hours * 3600, now) if loc else []
        )
    finally:
        conn.close()
    payload = {"unit": unit, "hours": hours, "histories": histories}
    assert_no_secrets(payload)
    return web.json_response(payload)


async def _handle_audit(request: web.Request) -> web.Response:
    unit = request.match_info["unit"]
    limit_raw = request.query.get("limit")
    before_raw = request.query.get("before_id")
    limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else 40
    before_id = int(before_raw) if before_raw and before_raw.isdigit() else None
    web_ctx: MonitorWeb = request.app["web_ctx"]
    if not web_ctx.unit_visible(unit):
        return web.json_response({"error": "unknown unit"}, status=404)
    conn = open_history(web_ctx.nodes_path.parent)
    try:
        rows, has_more = list_mesh_audit(conn, unit, limit=limit, before_id=before_id)
    finally:
        conn.close()
    payload = {"unit": unit, "rows": rows, "has_more": has_more}
    assert_no_secrets(payload)
    return web.json_response(payload)


async def _handle_stage_job(request: web.Request) -> web.Response:
    key = request.match_info["key"].lower()
    web_ctx: MonitorWeb = request.app["web_ctx"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"error": "object required"}, status=400)
    selector = body.get("mid") or body.get("index")
    if selector is None:
        return web.json_response({"error": "mid or index required"}, status=400)
    status, err = await web_ctx.enqueue_job(key, "stage", stage_selector=str(selector))
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


async def _handle_install_job(request: web.Request) -> web.Response:
    key = request.match_info["key"].lower()
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err = await web_ctx.enqueue_job(key, "install")
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
    if not web_ctx.unit_visible(key):
        return web.json_response({"error": "unknown unit"}, status=404)
    doc = load_nodes_doc(web_ctx.nodes_path)
    nodes = doc.get("nodes") or {}
    node = nodes.get(key)
    if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
        return web.json_response({"error": "unknown unit"}, status=404)
    if "repeat" in body:
        repeat = body["repeat"]
        if repeat is True:
            node["repeat"] = True
        elif repeat is False:
            node["repeat"] = False
        else:
            node.pop("repeat", None)
    if "paused" in body:
        if body["paused"] is True:
            node["paused"] = True
        else:
            node.pop("paused", None)
    if "routing" in body:
        routing = body["routing"]
        if routing in (None, "", "auto"):
            node["routing"] = "auto"
            node.pop("route", None)
        elif routing == "path":
            node["routing"] = "path"
        elif routing in ("direct", "flood"):
            node["routing"] = routing
            node.pop("route", None)
        else:
            return web.json_response(
                {"error": "routing must be auto, path, direct, or flood"},
                status=400,
            )
    if "route" in body:
        from envybot.routing import parse_path_paste

        route = body["route"]
        if route is None or route == "":
            node.pop("route", None)
        elif isinstance(route, str):
            try:
                forced = parse_path_paste(route)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            node["routing"] = "path"
            node["route"] = forced.label()
        else:
            return web.json_response({"error": "route must be a string"}, status=400)
    if "flood" in body:
        return web.json_response(
            {"error": "flood is removed; use routing: auto|path|direct|flood"},
            status=400,
        )
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
    if "full_sync_interval" in body:
        from envybot.web.profile_edit import apply_profile_field_edit
        from envybot.position import load_sites_doc as _load_sites_doc

        sites_doc = _load_sites_doc(web_ctx.sites_path)
        sites_map = sites_doc.get("sites") or {}
        err = apply_profile_field_edit(
            key,
            node,
            doc,
            sites_doc,
            sites_map,
            "full_sync_interval",
            body.get("full_sync_interval"),
        )
        if err:
            return web.json_response({"error": err}, status=400)
        write_sites_doc(web_ctx.sites_path, sites_doc)
    if "profile" in body or ("profile_field" in body and "profile_value" in body):
        from envybot.web.profile_edit import apply_profile_patch_body

        err = apply_profile_patch_body(key, node, doc, web_ctx.sites_path, body)
        if err:
            return web.json_response({"error": err}, status=400)
    if body.get("owner_info_default") is True:
        node.pop("owner_info", None)
    if body.get("trust_inherit") is True:
        node.pop("trust", None)
    if body.get("ack_stability") is True:
        node["stability_ack_ts"] = int(time.time())
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


async def _handle_unit_path_set(request: web.Request) -> web.Response:
    web_ctx: MonitorWeb = request.app["web_ctx"]
    key = request.match_info["key"].lower()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "object required"}, status=400)
    paste = body.get("paste")
    if not isinstance(paste, str) or not paste.strip():
        return web.json_response({"error": "paste required"}, status=400)
    if not web_ctx.unit_visible(key):
        return web.json_response({"error": "unknown unit"}, status=404)
    doc = load_nodes_doc(web_ctx.nodes_path)
    nodes = doc.get("nodes") or {}
    node = nodes.get(key)
    if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
        return web.json_response({"error": "unknown unit"}, status=404)
    try:
        forced = parse_path_paste(paste)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    node["routing"] = "path"
    node["route"] = forced.label()
    write_nodes_doc(web_ctx.nodes_path, doc)
    snap = await web_ctx.refresh_snapshot()
    unit = snap["units"].get(key) or {}
    return web.json_response(unit)


async def _handle_unit_path_clear(request: web.Request) -> web.Response:
    web_ctx: MonitorWeb = request.app["web_ctx"]
    key = request.match_info["key"].lower()
    if not web_ctx.unit_visible(key):
        return web.json_response({"error": "unknown unit"}, status=404)
    doc = load_nodes_doc(web_ctx.nodes_path)
    nodes = doc.get("nodes") or {}
    node = nodes.get(key)
    if not isinstance(node, dict) or is_decommissioned(node) or not is_meshcore_platform(node):
        return web.json_response({"error": "unknown unit"}, status=404)
    node["routing"] = "auto"
    node.pop("route", None)
    write_nodes_doc(web_ctx.nodes_path, doc)
    snap = await web_ctx.refresh_snapshot()
    unit = snap["units"].get(key) or {}
    return web.json_response(unit)


async def _console_json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _handle_console_open(request: web.Request) -> web.Response:
    web_ctx: MonitorWeb = request.app["web_ctx"]
    body = await _console_json(request)
    key = str(body.get("key") or "").strip().lower()
    tab_id = body.get("tab_id")
    if not key:
        return web.json_response({"error": "key required"}, status=400)
    if tab_id is not None and not isinstance(tab_id, str):
        return web.json_response({"error": "tab_id must be a string"}, status=400)
    status, err, payload = await web_ctx.open_console(key, tab_id)
    if status == 404:
        return web.json_response({"error": err}, status=404)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_send(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    body = await _console_json(request)
    cmd = body.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return web.json_response({"error": "cmd required"}, status=400)
    status, err, payload = await web_ctx.send_console(tab_id, cmd)
    if status == 400:
        return web.json_response({"error": err}, status=400)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_pending_patch(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    pending_id = request.match_info["pending_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    body = await _console_json(request)
    cmd = body.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return web.json_response({"error": "cmd required"}, status=400)
    status, err, payload = await web_ctx.patch_console_pending(tab_id, pending_id, cmd)
    if status == 400:
        return web.json_response({"error": err}, status=400)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_pending_delete(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    pending_id = request.match_info.get("pending_id")
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err, payload = await web_ctx.delete_console_pending(tab_id, pending_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_clear(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err, payload = await web_ctx.clear_console_history(tab_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_cancel(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err = await web_ctx.cancel_console(tab_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response({"ok": True})


async def _handle_console_park(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    body = await _console_json(request)
    cmd = body.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return web.json_response({"error": "cmd required"}, status=400)
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    status, err, payload = await web_ctx.park_console(tab_id, cmd.strip(), error)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_retry(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err, payload = await web_ctx.retry_console(tab_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_skip(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err, payload = await web_ctx.skip_console(tab_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response(payload or {})


async def _handle_console_close(request: web.Request) -> web.Response:
    tab_id = request.match_info["tab_id"]
    web_ctx: MonitorWeb = request.app["web_ctx"]
    status, err = await web_ctx.close_console(tab_id)
    if status == 409:
        return web.json_response({"error": err}, status=409)
    return web.json_response({"ok": True})


async def _handle_bench(request: web.Request) -> web.Response:
    web_ctx: MonitorWeb = request.app["web_ctx"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "json required"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "object required"}, status=400)
    lat = body.get("lat")
    lon = body.get("lon")
    if is_placeholder_gps(lat, lon):
        return web.json_response({"error": "invalid coordinates"}, status=400)
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid coordinates"}, status=400)
    doc = load_nodes_doc(web_ctx.nodes_path)
    doc["bench_loc"] = [lat_f, lon_f]
    write_nodes_doc(web_ctx.nodes_path, doc)
    await web_ctx.refresh_snapshot()
    return web.json_response({"bench_loc": [lat_f, lon_f]})


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
    app.router.add_post("/api/bench", _handle_bench)
    app.router.add_post("/api/unit/{key}", _handle_unit_edit)
    app.router.add_post("/api/unit/{key}/path", _handle_unit_path_set)
    app.router.add_delete("/api/unit/{key}/path", _handle_unit_path_clear)
    app.router.add_post("/api/sync/{key}", lambda r: _handle_manual_job(r, "sync"))
    app.router.add_post("/api/full/{key}", lambda r: _handle_manual_job(r, "full"))
    app.router.add_post("/api/stage/{key}", _handle_stage_job)
    app.router.add_post("/api/install/{key}", _handle_install_job)
    app.router.add_post("/api/console/open", _handle_console_open)
    app.router.add_post("/api/console/{tab_id}/send", _handle_console_send)
    app.router.add_post("/api/console/{tab_id}/cancel", _handle_console_cancel)
    app.router.add_post("/api/console/{tab_id}/park", _handle_console_park)
    app.router.add_post("/api/console/{tab_id}/retry", _handle_console_retry)
    app.router.add_post("/api/console/{tab_id}/skip", _handle_console_skip)
    app.router.add_post("/api/console/{tab_id}/close", _handle_console_close)
    app.router.add_post("/api/console/{tab_id}/clear", _handle_console_clear)
    app.router.add_patch("/api/console/{tab_id}/pending/{pending_id}", _handle_console_pending_patch)
    app.router.add_delete("/api/console/{tab_id}/pending/{pending_id}", _handle_console_pending_delete)
    app.router.add_delete("/api/console/{tab_id}/pending", _handle_console_pending_delete)
    app.router.add_get("/api/history/{unit}", _handle_history)
    app.router.add_get("/api/polls/{unit}", _handle_polls)
    app.router.add_get("/api/audit/{unit}", _handle_audit)
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
    include: set[str] | None = None,
    skip: set[str] | None = None,
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
        _include=include,
        _skip=skip,
    )
    web_ctx.console.bind_publish(hub.publish_console)
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
    include: set[str] | None = None,
    skip: set[str] | None = None,
) -> MonitorWeb:
    web_ctx = await create_monitor_web(
        nodes_path=nodes_path,
        sites_path=sites_path,
        host=host,
        port=port,
        stale_secs=stale_secs,
        include=include,
        skip=skip,
    )
    await web_ctx.refresh_snapshot()
    print(f"Fleet UI: {web_ctx.url}")
    if open_browser:
        webbrowser.open(web_ctx.url)
    return web_ctx
