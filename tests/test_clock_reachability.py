"""Reachability and admin access helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.radio import PollLog, RouterTarget, fetch_repeater_clock, maybe_admin_access


def _target() -> RouterTarget:
    return RouterTarget(
        key="me0001",
        unit_id="me0001",
        name="Test",
        site=None,
        pubkey_hex="aa" * 32,
        admin_password="x",
    )


class FetchRepeaterClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_not_heard(self) -> None:
        with patch("envybot.radio.send_cmd_sync", new=AsyncMock(return_value=None)):
            ts, heard = await fetch_repeater_clock(
                MagicMock(),
                _target(),
                cmd_timeout=8,
                attempts=1,
                log=PollLog(progress=False),
            )
        self.assertIsNone(ts)
        self.assertFalse(heard)

    async def test_unparsed_reply_is_heard(self) -> None:
        with patch("envybot.radio.send_cmd_sync", new=AsyncMock(return_value="???")):
            ts, heard = await fetch_repeater_clock(
                MagicMock(),
                _target(),
                cmd_timeout=8,
                attempts=1,
                log=PollLog(progress=False),
            )
        self.assertIsNone(ts)
        self.assertTrue(heard)


class AdminAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_admin_access_always_password_logins(self) -> None:
        with patch(
            "envybot.radio.admin_login",
            new=AsyncMock(return_value=(True, None, 1_700_000_000)),
        ) as login:
            ok, err, clock = await maybe_admin_access(
                MagicMock(),
                _target(),
                node={},
                doc={"trust": {"admin": ["ben"]}},
                login_timeout=8,
                cmd_timeout=8,
                attempts=1,
                session=None,
                log=PollLog(progress=False),
                keys={"ben": ["bb" * 32]},
            )
        login.assert_awaited_once()
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(clock, 1_700_000_000)
