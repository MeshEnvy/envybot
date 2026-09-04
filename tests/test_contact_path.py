"""Companion out_path formatting and path logging before mesh sends."""

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
    prepare_send_route,
    uses_flood_route,
)


def _target(*, site: str | None, flood: bool = False) -> RouterTarget:
    return RouterTarget(
        key="me0003",
        unit_id="ME0003",
        name="test",
        site=site,
        pubkey_hex="b2f84713d830" + "0" * 52,
        admin_password="pw",
        flood=flood,
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

        log_contact_path(client, _target(site=None), log=CaptureLog())
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

        log_contact_path(client, _target(site="ophir"), log=CaptureLog())
        self.assertEqual(lines, ["path: flood"])

    def test_log_bench_zero_hop(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {
            "out_path_len": 0,
            "out_path_hash_mode": 0,
            "out_path": "",
        }
        lines: list[str] = []

        class CaptureLog(PollLog):
            def step(self, msg: str) -> None:
                lines.append(msg)

        log_contact_path(client, _target(site=None), log=CaptureLog())
        self.assertEqual(lines, ["path: direct"])


class PrepareSendRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_deployed_resets_flood(self) -> None:
        contact = {
            "out_path_len": 2,
            "out_path_hash_mode": 1,
            "out_path": "266ab3b3",
        }
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = contact
        ok = MagicMock(type=EventType.OK)
        client.commands.reset_path = AsyncMock(return_value=ok)
        client.commands.update_contact = AsyncMock()
        await prepare_send_route(client, _target(site="ophir"), log=PollLog())
        client.commands.reset_path.assert_awaited_once()
        client.commands.update_contact.assert_not_awaited()
        self.assertEqual(contact["out_path_len"], -1)
        self.assertEqual(contact["out_path"], "")

    async def test_bench_sets_zero_hop(self) -> None:
        contact = {
            "public_key": "b2f84713d830" + "0" * 52,
            "out_path_len": -1,
            "out_path_hash_mode": -1,
            "out_path": "",
            "adv_name": "ME0003 test",
            "type": 1,
            "flags": 1,
            "last_advert": 0,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
        }
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = contact
        ok = MagicMock(type=EventType.OK)

        async def _update(c: dict, path: str = "", path_hash_mode: int | None = None) -> MagicMock:
            c["out_path_len"] = 0
            c["out_path_hash_mode"] = 0
            c["out_path"] = ""
            return ok

        client.commands.update_contact = AsyncMock(side_effect=_update)
        client.commands.reset_path = AsyncMock()
        await prepare_send_route(client, _target(site=None), log=PollLog())
        client.commands.update_contact.assert_awaited_once()
        client.commands.reset_path.assert_not_awaited()
        self.assertEqual(contact["out_path_len"], 0)

    async def test_bench_missing_cache_still_forces_direct(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = None
        client.contacts = {}
        ok = MagicMock(type=EventType.OK)
        client.commands.update_contact = AsyncMock(return_value=ok)
        client.commands.reset_path = AsyncMock()
        await prepare_send_route(client, _target(site=None), log=PollLog())
        client.commands.update_contact.assert_awaited_once()
        self.assertEqual(client.commands.update_contact.await_args.kwargs.get("path"), "")
        client.commands.reset_path.assert_not_awaited()
        stub = next(iter(client.contacts.values()))
        self.assertEqual(stub["out_path_len"], 0)

    async def test_bench_pins_direct_if_update_leaves_flood_cache(self) -> None:
        contact = {
            "public_key": "b2f84713d830" + "0" * 52,
            "out_path_len": -1,
            "out_path_hash_mode": -1,
            "out_path": "",
            "adv_name": "x",
            "type": 2,
            "flags": 1,
            "last_advert": 0,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
        }
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = contact
        ok = MagicMock(type=EventType.OK)
        client.commands.update_contact = AsyncMock(return_value=ok)
        await prepare_send_route(client, _target(site=None), log=PollLog())
        self.assertEqual(contact["out_path_len"], 0)

    async def test_bench_flood_flag_resets_flood(self) -> None:
        contact = {
            "out_path_len": 0,
            "out_path_hash_mode": 0,
            "out_path": "",
        }
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = contact
        ok = MagicMock(type=EventType.OK)
        client.commands.reset_path = AsyncMock(return_value=ok)
        client.commands.update_contact = AsyncMock()
        target = _target(site=None, flood=True)
        self.assertTrue(uses_flood_route(target))
        await prepare_send_route(client, target, log=PollLog())
        client.commands.reset_path.assert_awaited_once()
        client.commands.update_contact.assert_not_awaited()
        self.assertEqual(contact["out_path_len"], -1)
        self.assertEqual(contact["out_path"], "")


if __name__ == "__main__":
    unittest.main()
