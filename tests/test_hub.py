"""Fleet SSE hub snapshot persistence."""

from __future__ import annotations

import asyncio
import unittest

from envybot.web.hub import FleetHub


class HubSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_snapshot_does_not_hello(self) -> None:
        hub = FleetHub()
        q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        hub._clients.add(q)
        snap = {"units": {"me0001": {"key": "me0001", "session": {"state": "queued"}}}}
        await hub.replace_snapshot(snap)
        self.assertEqual(hub.snapshot, snap)
        self.assertTrue(q.empty())

    async def test_publish_audit_broadcasts(self) -> None:
        hub = FleetHub()
        q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        hub._clients.add(q)
        event = {"unit": "me0001", "row": {"id": 1, "outcome": "pending"}}
        await hub.publish_audit(event)
        kind, payload = await asyncio.wait_for(q.get(), timeout=1.0)
        self.assertEqual(kind, "audit")
        self.assertEqual(payload, event)

    async def test_publish_unit_updates_snapshot(self) -> None:
        hub = FleetHub()
        await hub.replace_snapshot(
            {
                "units": {
                    "me0001": {"key": "me0001", "session": {"state": "queued"}},
                    "me0002": {"key": "me0002", "session": {"state": "queued"}},
                }
            }
        )
        await hub.publish_unit({"key": "me0001", "session": {"state": "ok"}})
        units = hub.snapshot["units"]
        self.assertEqual(units["me0001"]["session"]["state"], "ok")
        self.assertEqual(units["me0002"]["session"]["state"], "queued")
