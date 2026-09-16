"""Companion out_path formatting and path routing before mesh sends."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from meshcore import EventType

from envybot.radio import (
    RouterTarget,
    contact_out_path_label,
    contact_route_audit_label,
    contact_route_display_label,
    log_contact_path,
    PollLog,
    prepare_login_route,
    prepare_send_route,
    refresh_fleet_paths,
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
        self.assertEqual(lines, ["path: 266a → b3b3"])

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

    def test_log_path_resolves_repeater_names(self) -> None:
        contact = {
            "out_path_len": 2,
            "out_path_hash_mode": 1,
            "out_path": "266ab3b3",
        }
        known = {
            "266a": {"adv_name": "Alpha", "public_key": "266a" + "0" * 60},
            "b3b3": {"adv_name": "Bravo", "public_key": "b3b3" + "0" * 60},
        }
        client = MagicMock()

        def lookup(prefix: str) -> dict[str, Any] | None:
            token = prefix.strip().lower()[:4]
            return known.get(token)

        client.get_contact_by_key_prefix.side_effect = lookup
        label = contact_route_display_label(client, contact)
        self.assertEqual(label, "Alpha (266a) → Bravo (b3b3)")


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

    async def test_refresh_fleet_paths_clears_all(self) -> None:
        client = MagicMock()
        client.commands._mesh_request_lock = asyncio.Lock()
        client.ensure_contacts = AsyncMock()
        client.commands.reset_path = AsyncMock(
            return_value=MagicMock(type=EventType.OK, payload={})
        )
        contacts: dict[str, dict[str, Any]] = {}

        def get_contact(prefix: str) -> dict[str, Any]:
            for c in contacts.values():
                if c["public_key"].startswith(prefix):
                    return c
            return {}

        client.get_contact_by_key_prefix.side_effect = get_contact
        client.contacts = contacts

        targets = [_target(), _target()]
        targets[1] = RouterTarget(
            key="me0010",
            unit_id="ME0010",
            name="night",
            site="nightengale",
            pubkey_hex="c1a2b3d4e5f6" + "0" * 52,
            admin_password="pw",
            routing=RoutingMode.PATH,
        )
        for t in targets:
            contacts[t.pubkey_hex.lower()] = {
                "public_key": t.pubkey_hex.lower(),
                "out_path_len": 2,
                "out_path_hash_mode": 1,
                "out_path": "aabbccdd",
            }

        cleared = await refresh_fleet_paths(client, targets, log=PollLog())
        self.assertEqual(cleared, 2)
        self.assertEqual(client.commands.reset_path.await_count, 2)
        for c in contacts.values():
            self.assertEqual(c["out_path_len"], -1)


if __name__ == "__main__":
    unittest.main()
