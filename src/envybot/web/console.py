"""Fleet console session: priority CLI on one unit, no radio lock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from envybot.jobs import FleetScheduler, RadioJob
from envybot.radio import FleetSession, RouterTarget

PublishConsoleFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ConsoleState:
    key: str
    state: str = "ready"
    attempt: int = 0
    max_attempts: int = 10
    cmd: str | None = None
    reply: str | None = None
    error: str | None = None
    cancel_gen: int = 0
    _send_waiter: asyncio.Future[dict[str, Any]] | None = field(default=None, repr=False)

    def to_event(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "cmd": self.cmd,
            "reply": self.reply,
            "error": self.error,
        }

    def poll_console(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state}


class ConsoleManager:
    """UI session plus priority console jobs. Cadence keeps running while idle."""

    def __init__(self) -> None:
        self._session: ConsoleState | None = None
        self._publish: PublishConsoleFn | None = None

    @property
    def active(self) -> ConsoleState | None:
        return self._session

    def bind_publish(self, publish: PublishConsoleFn) -> None:
        self._publish = publish

    async def _emit(self) -> None:
        if self._publish is None or self._session is None:
            return
        await self._publish(self._session.to_event())

    def cancel_check(self, cancel_gen: int) -> bool:
        sess = self._session
        if sess is None:
            return False
        return sess.cancel_gen > cancel_gen

    async def open(
        self,
        *,
        key: str,
        scheduler: FleetScheduler | None = None,
        max_attempts: int = 10,
    ) -> ConsoleState:
        if self._session is not None and self._session.key != key:
            await self.close(scheduler=scheduler)
        self._session = ConsoleState(key=key, state="ready", max_attempts=max_attempts)
        await self._emit()
        return self._session

    async def enqueue_send(
        self,
        *,
        cmd: str,
        scheduler: FleetScheduler,
        target: RouterTarget,
        session: FleetSession | None = None,
    ) -> asyncio.Future[dict[str, Any]]:
        sess = self._session
        if sess is None or sess.key != target.key:
            raise RuntimeError("console not open for this unit")
        if sess.state == "sending":
            raise RuntimeError("console busy")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        sess._send_waiter = fut
        sess.state = "sending"
        sess.cmd = cmd
        sess.reply = None
        sess.error = None
        sess.attempt = 0
        cancel_gen = sess.cancel_gen
        jobs: list[RadioJob] = []
        if session is None or not session.is_authed(target.key):
            jobs.append(
                RadioJob(
                    kind="console:login",
                    unit_key=target.key,
                    manual=True,
                    manual_job="console",
                    extra={"cancel_gen": cancel_gen},
                )
            )
        jobs.append(
            RadioJob(
                kind="console:cli",
                unit_key=target.key,
                manual=True,
                manual_job="console",
                extra={"cmd": cmd, "cancel_gen": cancel_gen},
                future=fut,
            )
        )
        scheduler.enqueue_console(target, jobs)
        await self._emit()
        return fut

    async def cancel(
        self,
        *,
        scheduler: FleetScheduler | None,
        session: Any,
    ) -> None:
        sess = self._session
        if sess is None:
            return
        sess.cancel_gen += 1
        if session is not None:
            session.console_cli_cancel_gen = sess.cancel_gen
        if scheduler is not None:
            scheduler.cancel_console_jobs(sess.key)
        if sess._send_waiter and not sess._send_waiter.done():
            sess._send_waiter.set_result({"cancelled": True})
            sess._send_waiter = None
        sess.state = "ready"
        sess.cmd = None
        sess.error = "cancelled"
        await self._emit()

    async def close(
        self,
        *,
        scheduler: FleetScheduler | None = None,
        session: Any | None = None,
    ) -> None:
        if self._session is None:
            return
        key = self._session.key
        if self._session._send_waiter and not self._session._send_waiter.done():
            self._session._send_waiter.set_result({"cancelled": True})
        self._session.state = "closed"
        await self._emit()
        self._session = None
        if session is not None:
            if session.audit_source == "console":
                session.audit_source = None
            session.console_cli_cancel_gen = 0
        if scheduler is not None:
            scheduler.cancel_console_jobs(key)

    async def on_login_start(self, attempt: int, max_attempts: int) -> None:
        if self._session is None:
            return
        self._session.state = "sending"
        self._session.attempt = attempt
        self._session.max_attempts = max_attempts
        await self._emit()

    async def on_login_ready(self) -> None:
        if self._session is None:
            return
        if self._session.state != "sending":
            self._session.state = "ready"
            self._session.attempt = 0
            self._session.error = None
            await self._emit()

    async def on_cli_start(self, attempt: int, max_attempts: int, cmd: str) -> None:
        if self._session is None:
            return
        self._session.state = "sending"
        self._session.attempt = attempt
        self._session.max_attempts = max_attempts
        self._session.cmd = cmd
        await self._emit()

    async def on_cli_done(
        self,
        *,
        ok: bool,
        reply: str | None,
        error: str | None,
    ) -> None:
        if self._session is None:
            return
        self._session.state = "ready"
        self._session.attempt = 0
        self._session.reply = reply
        self._session.error = error
        if self._session._send_waiter and not self._session._send_waiter.done():
            self._session._send_waiter.set_result(
                {"ok": ok, "reply": reply, "error": error}
            )
            self._session._send_waiter = None
        await self._emit()
