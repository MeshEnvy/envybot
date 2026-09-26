"""Companion reconnect after transport drop."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.radio import (
    FleetSession,
    PollLog,
    _hard_reset_transport,
    recover_companion,
)
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

    async def test_force_resets_connected_client(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.dispatcher.running = True
        ok_event = SimpleNamespace(type=EventType.OK, payload={})
        client.commands.send_appstart = AsyncMock(return_value=ok_event)
        client.commands.set_time = AsyncMock(return_value=ok_event)
        client.ensure_contacts = AsyncMock()
        session = FleetSession()
        log = PollLog(progress=False)

        connected = False

        def mark_connected(_self: object) -> bool:
            return connected

        type(client).is_connected = property(mark_connected)

        async def connect_side_effect() -> str:
            nonlocal connected
            connected = True
            return "/dev/ble"

        client.connection_manager.connect = AsyncMock(side_effect=connect_side_effect)

        with patch("envybot.radio.sync_fleet_contacts", new=AsyncMock()):
            ok = await recover_companion(
                client, session=session, targets=[], log=log, attempts=1, force=True
            )

        self.assertTrue(ok)
        client.connection_manager.connect.assert_awaited()

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

        def mark_connected(_self: object) -> bool:
            return connected

        type(client).is_connected = property(mark_connected)

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

    async def test_hard_reset_drops_stale_ble_device(self) -> None:
        bleak = SimpleNamespace(disconnect=AsyncMock())
        cx = SimpleNamespace(
            device="stale-device",
            _user_provided_device="stale-device",
            client=bleak,
            _user_provided_client=bleak,
            address="61518133-0BA9-2052-16D9-3F4CD149D1F3",
            _user_provided_address="61518133-0BA9-2052-16D9-3F4CD149D1F3",
        )
        cm = SimpleNamespace(connection=cx, disconnect=AsyncMock(), _is_connected=False)
        client = SimpleNamespace(connection_manager=cm)
        await _hard_reset_transport(client)
        bleak.disconnect.assert_awaited()
        self.assertIsNone(cx.device)
        self.assertIsNone(cx._user_provided_device)
        self.assertIsNone(cx.client)
        self.assertEqual(cx._user_provided_address, "61518133-0BA9-2052-16D9-3F4CD149D1F3")
        self.assertFalse(cm._is_connected)

    async def test_ensure_companion_noop_without_recovery(self) -> None:
        session = FleetSession()
        log = PollLog(progress=False)
        self.assertTrue(await session.ensure_companion_connected(log=log))

    async def test_ensure_cooldown_skips_repeat_recover(self) -> None:
        client = MagicMock()
        client.is_connected = False
        session = FleetSession()
        target = SimpleNamespace(key="me0001", pubkey_hex="aa" * 32, unit_id="ME0001")
        session.enable_companion_recovery(client, [target])
        log = PollLog(progress=False)
        with patch("envybot.radio.recover_companion", new=AsyncMock(return_value=False)) as rec:
            self.assertFalse(await session.ensure_companion_connected(log=log))
            rec.assert_awaited_once()
            self.assertFalse(await session.ensure_companion_connected(log=log))
            rec.assert_awaited_once()

    async def test_ensure_sleep_gap_forces_recover(self) -> None:
        client = MagicMock()
        client.is_connected = True
        session = FleetSession()
        target = SimpleNamespace(key="me0001", pubkey_hex="aa" * 32, unit_id="ME0001")
        session.enable_companion_recovery(client, [target])
        session._watch_wall = time.time() - 120
        session._watch_mono = time.monotonic()
        log = PollLog(progress=False)
        with patch("envybot.radio.recover_companion", new=AsyncMock(return_value=True)) as rec:
            self.assertTrue(await session.ensure_companion_connected(log=log))
            rec.assert_awaited()
            self.assertTrue(rec.await_args.kwargs.get("force"))


class HostSleepTests(unittest.TestCase):
    def test_idle_without_sleep_is_not_a_gap(self) -> None:
        session = FleetSession()
        session._watch_wall = time.time() - 3600
        session._watch_mono = time.monotonic() - 3600
        self.assertFalse(session.host_slept())

    def test_wall_jump_is_sleep(self) -> None:
        session = FleetSession()
        session._watch_wall = time.time() - 120
        session._watch_mono = time.monotonic()
        self.assertTrue(session.host_slept())


if __name__ == "__main__":
    unittest.main()
