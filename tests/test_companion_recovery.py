"""Companion reconnect after transport drop."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.radio import FleetSession, PollLog, recover_companion
from meshcore import EventType


class RecoverCompanionTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_already_connected(self) -> None:
        client = MagicMock()
        client.is_connected = True
        session = FleetSession()
        log = PollLog(progress=False)
        ok = await recover_companion(client, session=session, targets=[], log=log)
        self.assertTrue(ok)
        client.connection_manager.connect.assert_not_called()

    async def test_reconnects_and_clears_auth(self) -> None:
        client = MagicMock()
        client.is_connected = False
        client.dispatcher.running = True
        client.connection_manager.connect = AsyncMock(return_value="/dev/ble")
        ok_event = SimpleNamespace(type=EventType.OK, payload={})
        client.commands.send_appstart = AsyncMock(return_value=ok_event)
        client.commands.set_time = AsyncMock(return_value=ok_event)
        client.ensure_contacts = AsyncMock()
        session = FleetSession()
        session.mark_authed("me0001")
        log = PollLog(progress=False)
        target = SimpleNamespace(key="me0001", pubkey_hex="aa" * 32, unit_id="ME0001")

        connected = False

        def mark_connected() -> bool:
            return connected

        type(client).is_connected = property(lambda self: connected)

        async def connect_side_effect() -> str:
            nonlocal connected
            connected = True
            return "/dev/ble"

        client.connection_manager.connect = AsyncMock(side_effect=connect_side_effect)

        with patch("envybot.radio.sync_fleet_contacts", new=AsyncMock()) as sync:
            ok = await recover_companion(
                client, session=session, targets=[target], log=log, attempts=1
            )

        self.assertTrue(ok)
        sync.assert_awaited_once()
        self.assertEqual(session.authed_units, set())
        client.commands.send_appstart.assert_awaited_once()

    async def test_ensure_companion_noop_without_recovery(self) -> None:
        session = FleetSession()
        log = PollLog(progress=False)
        self.assertTrue(await session.ensure_companion_connected(log=log))


if __name__ == "__main__":
    unittest.main()
