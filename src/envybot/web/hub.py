"""SSE fan-out hub for monitor web clients."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


class FleetHub:
    """In-memory pub/sub for fleet snapshot and live poll events."""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._snapshot: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return self._snapshot

    async def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        async with self._lock:
            self._snapshot = snapshot
        await self._broadcast("hello", snapshot)

    async def replace_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Store snapshot without a hello (live clients get unit/session events)."""
        async with self._lock:
            self._snapshot = snapshot

    async def publish_unit(self, unit: dict[str, Any]) -> None:
        key = unit.get("key")
        if self._snapshot is not None and isinstance(key, str) and key:
            units = self._snapshot.setdefault("units", {})
            units[key] = unit
        await self._broadcast("unit", unit)

    async def publish_session(self, session: dict[str, Any]) -> None:
        if self._snapshot is not None:
            poll = self._snapshot.setdefault("poll", {})
            poll.update(session)
        await self._broadcast("session", session)

    async def publish_audit(self, event: dict[str, Any]) -> None:
        await self._broadcast("audit", event)

    async def publish_console(self, event: dict[str, Any]) -> None:
        if self._snapshot is not None:
            poll = self._snapshot.setdefault("poll", {})
            console = poll.setdefault("console", {"tabs": []})
            tabs = console.setdefault("tabs", [])
            if not isinstance(tabs, list):
                tabs = []
                console["tabs"] = tabs
            tab_id = event.get("tab_id")
            if event.get("state") == "closed" and isinstance(tab_id, str):
                console["tabs"] = [t for t in tabs if t.get("tab_id") != tab_id]
            elif isinstance(tab_id, str) and tab_id:
                replaced = False
                for i, tab in enumerate(tabs):
                    if tab.get("tab_id") == tab_id:
                        tabs[i] = event
                        replaced = True
                        break
                if not replaced:
                    tabs.append(event)
        await self._broadcast("console", event)

    async def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        dead: list[asyncio.Queue[tuple[str, dict[str, Any]]]] = []
        for queue in list(self._clients):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self._clients.discard(queue)

    async def subscribe(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=64)
        self._clients.add(queue)
        if self._snapshot is not None:
            await queue.put(("hello", self._snapshot))
        try:
            while True:
                yield await queue.get()
        finally:
            self._clients.discard(queue)

    @staticmethod
    def sse_format(event: str, data: dict[str, Any]) -> str:
        body = json.dumps(data, separators=(",", ":"), default=str)
        return f"event: {event}\ndata: {body}\n\n"

    @staticmethod
    def sse_comment(text: str = "") -> str:
        return f": {text}\n\n"
