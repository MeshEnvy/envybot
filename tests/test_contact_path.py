"""Companion out_path formatting and path routing before mesh sends."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from meshcore import EventType

from envybot.radio import (
    RouterTarget,
    contact_out_path_label,
    contact_route_audit_label,
    log_contact_path,
    PollLog,
    prepare_login_route,
    prepare_send_route,
    reset_to_flood,
)
from envybot.routing import RoutingMode


def _target(*, routing: RoutingMode = RoutingMode.PATH) -> RouterTarget:
    return RouterTarget(
        key="me0003",
        unit_id="ME0003",
        name="test",
        site=None,
        pubkey_hex="b2f84713d830" + "0" * 52,
        admin_password="pw",
        routing=routing,
    )


class ContactOutPathTests(unittest.TestCase):
    def test_two_byte_hops(self) -> None:
        label = contact_out_path_label(
            {
                "out_path_len": 2,
                "out_path_hash_mode": 1,
                "out_path": "a1b2c3d4",
            }
        )
        self.assertEqual(label, "a1b2 c3d4")

    def test_flood_contact_returns_none(self) -> None:
        self.assertIsNone(
            contact_out_path_label(
                {"out_path_len": -1, "out_path_hash_mode": -1, "out_path": ""}
            )
        )
        self.assertIsNone(contact_out_path_label(None))

    def test_zero_hop_audit_label(self) -> None:
        self.assertEqual(
            contact_route_audit_label(
                {"out_path_len": 0, "out_path_hash_mode": 0, "out_path": ""}
            ),
            "direct",
        )

    def test_log_direct_path(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": 2,
            "out_path_hash_mode": 1,
            "out_path": "266ab3b3",
        }
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, _target(), log=CaptureLog())
        self.assertEqual(lines, ["path: 266a b3b3"])

    def test_log_flood_when_no_path(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": -1,
            "out_path_hash_mode": -1,
            "out_path": "",
        }
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, _target(), log=CaptureLog())
        self.assertEqual(lines, ["path: flood"])


class PrepareRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_mode_flood_when_no_cache(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = None
        client.contacts = {}
        client.commands.reset_path = AsyncMock(
            return_value=MagicMock(type=EventType.OK, payload={})
        )
        route_extra: dict = {}
        await prepare_login_route(client, _target(), log=PollLog(), route_extra=route_extra)
        client.commands.reset_path.assert_awaited()
        self.assertEqual(route_extra["live_route"]["kind"], "flood")
        self.assertTrue(route_extra["live_route"]["fallback"])

    async def test_path_mode_uses_cache(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": 0,
            "out_path_hash_mode": 0,
            "out_path": "",
        }
        client.commands.reset_path = AsyncMock()
        route_extra: dict = {}
        await prepare_send_route(client, _target(), log=PollLog(), route_extra=route_extra)
        client.commands.reset_path.assert_not_awaited()
        self.assertEqual(route_extra["live_route"]["kind"], "direct")

    async def test_direct_pins_zero_hop(self) -> None:
        client = MagicMock()
        contact = {
            "public_key": "b2f84713d830" + "0" * 52,
            "out_path_len": -1,
            "out_path_hash_mode": -1,
            "out_path": "",
        }
        client.get_contact_by_key_prefix.return_value = contact
        client.commands.update_contact = AsyncMock(
            return_value=MagicMock(type=EventType.OK, payload={})
        )
        await prepare_send_route(
            client, _target(routing=RoutingMode.DIRECT), log=PollLog()
        )
        self.assertEqual(contact["out_path_len"], 0)
        client.commands.update_contact.assert_awaited()

    async def test_flood_policy_resets_flood(self) -> None:
        client = MagicMock()
        contact = {
            "public_key": "b2f84713d830" + "0" * 52,
            "out_path_len": 0,
            "out_path_hash_mode": 0,
            "out_path": "",
        }
        client.get_contact_by_key_prefix.return_value = contact
        client.commands.reset_path = AsyncMock(
            return_value=MagicMock(type=EventType.OK, payload={})
        )
        target = _target(routing=RoutingMode.FLOOD)
        await reset_to_flood(client, target, log=PollLog())
        self.assertEqual(contact["out_path_len"], -1)


if __name__ == "__main__":
    unittest.main()
