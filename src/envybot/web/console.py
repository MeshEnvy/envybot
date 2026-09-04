"""Fleet console tabs: priority CLI, staging queue, no radio lock."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from envybot.jobs import FleetScheduler, RadioJob
from envybot.radio import FleetSession, RouterTarget

PublishConsoleFn = Callable[[dict[str, Any]], Awaitable[None]]

HISTORY_CAP = 200


@dataclass
class PendingCmd:
    id: str
    cmd: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "cmd": self.cmd}


@dataclass
class ConsoleState:
    tab_id: str
    key: str
    state: str = "ready"
    attempt: int = 0
    max_attempts: int = 10
    cmd: str | None = None
    reply: str | None = None
    error: str | None = None
    cancel_gen: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    pending: list[PendingCmd] = field(default_factory=list)
    _send_waiter: asyncio.Future[dict[str, Any]] | None = field(default=None, repr=False)
    _scheduler: FleetScheduler | None = field(default=None, repr=False)
    _target: RouterTarget | None = field(default=None, repr=False)
    _session: FleetSession | None = field(default=None, repr=False)

    def to_event(self) -> dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "key": self.key,
            "state": self.state,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "cmd": self.cmd,
            "reply": self.reply,
            "error": self.error,
            "history": list(self.history),
            "pending": [p.to_dict() for p in self.pending],
        }


class ConsoleManager:
    """One UI tab per session. Cadence keeps running while idle."""

    def __init__(self) -> None:
        self._tabs: dict[str, ConsoleState] = {}
        self._order: list[str] = []
        self._publish: PublishConsoleFn | None = None

    def bind_publish(self, publish: PublishConsoleFn) -> None:
        self._publish = publish

    def get(self, tab_id: str) -> ConsoleState | None:
        return self._tabs.get(tab_id)

    def tabs_payload(self) -> list[dict[str, Any]]:
        return [self._tabs[tid].to_event() for tid in self._order if tid in self._tabs]

    def poll_console(self) -> dict[str, Any]:
        return {"tabs": self.tabs_payload()}

    async def _emit(self, sess: ConsoleState) -> None:
        if self._publish is None:
            return
        await self._publish(sess.to_event())

    def cancel_check(self, tab_id: str, cancel_gen: int) -> bool:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return False
        return sess.cancel_gen > cancel_gen

    def _append_history(
        self,
        sess: ConsoleState,
        *,
        cmd: str,
        reply: str | None = None,
        error: str | None = None,
    ) -> None:
        block: dict[str, Any] = {"cmd": cmd}
        if reply is not None:
            block["reply"] = reply
        if error is not None:
            block["error"] = error
        sess.history.append(block)
        if len(sess.history) > HISTORY_CAP:
            sess.history = sess.history[-HISTORY_CAP:]

    async def open(
        self,
        *,
        key: str,
        tab_id: str | None = None,
        scheduler: FleetScheduler | None = None,
        max_attempts: int = 10,
    ) -> ConsoleState:
        key = key.lower()
        if tab_id and tab_id in self._tabs:
            return self._tabs[tab_id]
        tid = (tab_id or "").strip() or uuid.uuid4().hex
        sess = ConsoleState(
            tab_id=tid,
            key=key,
            state="ready",
            max_attempts=max_attempts,
        )
        sess._scheduler = scheduler
        self._tabs[tid] = sess
        if tid not in self._order:
            self._order.append(tid)
        await self._emit(sess)
        return sess

    async def _start_cmd(
        self,
        sess: ConsoleState,
        cmd: str,
        *,
        scheduler: FleetScheduler,
        target: RouterTarget,
        session: FleetSession | None,
    ) -> asyncio.Future[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        sess._send_waiter = fut
        sess._scheduler = scheduler
        sess._target = target
        sess._session = session
        sess.state = "sending"
        sess.cmd = cmd
        sess.reply = None
        sess.error = None
        sess.attempt = 1
        cancel_gen = sess.cancel_gen
        extra_base = {"cancel_gen": cancel_gen, "tab_id": sess.tab_id}
        jobs: list[RadioJob] = []
        uq = scheduler.units.get(target.key)
        login_queued = bool(
            uq and any(j.kind in ("login", "console:login") for j in uq.jobs)
        )
        if session is None or not session.is_authed(target.key) or login_queued:
            jobs.append(
                RadioJob(
                    kind="console:login",
                    unit_key=target.key,
                    manual=True,
                    manual_job="console",
                    extra=dict(extra_base),
                )
            )
        jobs.append(
            RadioJob(
                kind="console:cli",
                unit_key=target.key,
                manual=True,
                manual_job="console",
                extra={**extra_base, "cmd": cmd},
                future=fut,
            )
        )
        scheduler.enqueue_console(target, jobs)
        await self._emit(sess)
        return fut

    async def enqueue_send(
        self,
        *,
        tab_id: str,
        cmd: str,
        scheduler: FleetScheduler,
        target: RouterTarget,
        session: FleetSession | None = None,
    ) -> asyncio.Future[dict[str, Any]] | None:
        sess = self._tabs.get(tab_id)
        if sess is None or sess.key != target.key:
            raise RuntimeError("console tab not open")
        if sess.state in ("sending", "failed"):
            sess.pending.append(PendingCmd(id=uuid.uuid4().hex[:12], cmd=cmd))
            await self._emit(sess)
            return None
        return await self._start_cmd(
            sess, cmd, scheduler=scheduler, target=target, session=session
        )

    async def _drain_pending(self, sess: ConsoleState) -> None:
        if sess.state in ("sending", "failed") or not sess.pending:
            return
        scheduler = sess._scheduler
        target = sess._target
        if scheduler is None or target is None:
            return
        nxt = sess.pending.pop(0)
        await self._start_cmd(
            sess,
            nxt.cmd,
            scheduler=scheduler,
            target=target,
            session=sess._session,
        )

    def patch_pending(self, tab_id: str, pending_id: str, cmd: str) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return None
        for item in sess.pending:
            if item.id == pending_id:
                item.cmd = cmd
                return sess
        return None

    def delete_pending(self, tab_id: str, pending_id: str | None) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return None
        if pending_id is None:
            sess.pending.clear()
            return sess
        sess.pending = [p for p in sess.pending if p.id != pending_id]
        return sess

    def clear_history(self, tab_id: str) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return None
        sess.history.clear()
        return sess

    async def _finish_cmd(
        self,
        sess: ConsoleState,
        *,
        ok: bool,
        reply: str | None,
        error: str | None,
        drop_pending: bool = False,
        park: bool = False,
    ) -> None:
        if park:
            sess.state = "failed"
            sess.reply = reply
            sess.error = error
            if sess._send_waiter and not sess._send_waiter.done():
                sess._send_waiter.set_result({"ok": ok, "reply": reply, "error": error})
                sess._send_waiter = None
            if drop_pending:
                sess.pending.clear()
            return
        if sess.cmd:
            self._append_history(sess, cmd=sess.cmd, reply=reply, error=error)
        sess.state = "ready"
        sess.attempt = 0
        sess.reply = reply
        sess.error = error
        if sess._send_waiter and not sess._send_waiter.done():
            sess._send_waiter.set_result({"ok": ok, "reply": reply, "error": error})
            sess._send_waiter = None
        sess.cmd = None
        if drop_pending:
            sess.pending.clear()

    async def cancel(
        self,
        tab_id: str,
        *,
        scheduler: FleetScheduler | None,
        session: Any,
    ) -> None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return
        sess.cancel_gen += 1
        if scheduler is not None:
            scheduler.cancel_console_jobs(sess.key, tab_id)
        if sess.state == "sending":
            await self._finish_cmd(
                sess, ok=False, reply=None, error="cancelled", drop_pending=False
            )
        await self._emit(sess)
        await self._drain_pending(sess)

    async def close(
        self,
        tab_id: str,
        *,
        scheduler: FleetScheduler | None = None,
        session: Any | None = None,
    ) -> None:
        sess = self._tabs.pop(tab_id, None)
        if sess is None:
            return
        if tab_id in self._order:
            self._order.remove(tab_id)
        sess.cancel_gen += 1
        if sess._send_waiter and not sess._send_waiter.done():
            sess._send_waiter.set_result({"cancelled": True})
        sess.state = "closed"
        sess.pending.clear()
        await self._emit(sess)
        if session is not None and session.audit_source == "console":
            if not any(s.state == "sending" for s in self._tabs.values()):
                session.audit_source = None
        if scheduler is not None:
            scheduler.cancel_console_jobs(sess.key, tab_id)

    async def on_login_start(self, tab_id: str, attempt: int, max_attempts: int) -> None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return
        sess.state = "sending"
        sess.attempt = attempt
        sess.max_attempts = max_attempts
        await self._emit(sess)

    async def on_login_ready(self, tab_id: str) -> None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return
        if sess.state != "sending":
            sess.state = "ready"
            sess.attempt = 0
            sess.error = None
            await self._emit(sess)

    async def on_cli_start(self, tab_id: str, attempt: int, max_attempts: int, cmd: str) -> None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return
        sess.state = "sending"
        sess.attempt = attempt
        sess.max_attempts = max_attempts
        sess.cmd = cmd
        await self._emit(sess)

    async def fail_sending(self, key: str, *, error: str) -> None:
        """Park every sending tab on this unit (login exhausted)."""
        key = key.lower()
        for sess in list(self._tabs.values()):
            if sess.key != key or sess.state != "sending":
                continue
            await self.on_cli_done(
                sess.tab_id,
                ok=False,
                reply=None,
                error=error,
                drop_pending=True,
                drain=False,
            )

    async def on_cli_done(
        self,
        tab_id: str,
        *,
        ok: bool,
        reply: str | None,
        error: str | None,
        drop_pending: bool = False,
        drain: bool = True,
    ) -> None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return
        if sess.state != "sending":
            return
        park = not ok and not drop_pending and not drain
        await self._finish_cmd(
            sess,
            ok=ok,
            reply=reply,
            error=error,
            drop_pending=drop_pending,
            park=park,
        )
        await self._emit(sess)
        if drain and not drop_pending:
            await self._drain_pending(sess)

    async def park(
        self,
        tab_id: str,
        *,
        cmd: str,
        error: str | None,
        scheduler: FleetScheduler | None = None,
        target: RouterTarget | None = None,
        session: FleetSession | None = None,
    ) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None:
            return None
        sess.state = "failed"
        sess.cmd = cmd
        sess.error = error
        sess.reply = None
        if scheduler is not None:
            sess._scheduler = scheduler
        if target is not None:
            sess._target = target
        if session is not None:
            sess._session = session
        await self._emit(sess)
        return sess

    async def retry(
        self,
        tab_id: str,
        *,
        scheduler: FleetScheduler,
        target: RouterTarget,
        session: FleetSession | None = None,
    ) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None or not sess.cmd or sess.state not in ("failed", "sending"):
            return None
        sess.cancel_gen += 1
        scheduler.cancel_console_jobs(sess.key, sess.tab_id)
        await self._start_cmd(
            sess,
            sess.cmd,
            scheduler=scheduler,
            target=target,
            session=session if session is not None else sess._session,
        )
        return sess

    async def skip(
        self,
        tab_id: str,
        *,
        scheduler: FleetScheduler | None = None,
        target: RouterTarget | None = None,
        session: FleetSession | None = None,
    ) -> ConsoleState | None:
        sess = self._tabs.get(tab_id)
        if sess is None or sess.state != "failed":
            return None
        if scheduler is not None:
            sess._scheduler = scheduler
        if target is not None:
            sess._target = target
        if session is not None:
            sess._session = session
        if sess.cmd:
            self._append_history(
                sess, cmd=sess.cmd, reply=sess.reply, error=sess.error or "skipped"
            )
        sess.state = "ready"
        sess.attempt = 0
        sess.cmd = None
        sess.reply = None
        sess.error = None
        await self._emit(sess)
        await self._drain_pending(sess)
        return sess
