"""Reachability and admin access helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.radio import (
    PollLog,
    RouterTarget,
    fetch_repeater_clock,
    maybe_admin_access,
    maybe_sync_repeater_clock,
)


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


class MaybeSyncRepeaterClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_not_heard(self) -> None:
        with (
            patch("envybot.radio.time.time", return_value=1_789_568_474),
            patch("envybot.radio.send_cmd_sync", new=AsyncMock(return_value=None)) as send,
        ):
            clock, send_kind = await maybe_sync_repeater_clock(
                MagicMock(),
                _target(),
                login_clock=1_789_568_049,
                stored_clock=None,
                cmd_timeout=8,
                attempts=1,
                log=PollLog(progress=False),
                attempt_num=1,
                attempt_cap=10,
            )
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["attempt_num"], 1)
        self.assertEqual(send.await_args.kwargs["attempt_cap"], 10)
        self.assertEqual(clock, 1_789_568_049)
        self.assertEqual(send_kind, "timeout")

    async def test_in_skew_skips_send(self) -> None:
        now = 1_789_568_474
        with (
            patch("envybot.radio.time.time", return_value=now),
            patch("envybot.radio.send_cmd_sync", new=AsyncMock()) as send,
        ):
            clock, send_kind = await maybe_sync_repeater_clock(
                MagicMock(),
                _target(),
                login_clock=now - 10,
                stored_clock=None,
                cmd_timeout=8,
                attempts=1,
                log=PollLog(progress=False),
            )
        send.assert_not_awaited()
        self.assertEqual(clock, now - 10)
        self.assertEqual(send_kind, "skip")


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
