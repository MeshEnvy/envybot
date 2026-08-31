"""Skip-login clock is the reachability probe. Timeout skips remaining ops."""

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


class SkipLoginClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_clock_timeout_fails_access(self) -> None:
        with (
            patch("envybot.radio.companion_identity", return_value="bb" * 16),
            patch("envybot.radio.companion_in_desired_acl", return_value=True),
            patch(
                "envybot.radio.fetch_repeater_clock",
                new=AsyncMock(return_value=(None, False)),
            ),
        ):
            ok, err, clock = await maybe_admin_access(
                MagicMock(),
                _target(),
                node={},
                doc={},
                login_timeout=8,
                cmd_timeout=8,
                attempts=1,
                session=None,
                log=PollLog(progress=False),
            )
        self.assertFalse(ok)
        self.assertEqual(err, "clock timeout")
        self.assertIsNone(clock)

    async def test_clock_heard_allows_access(self) -> None:
        with (
            patch("envybot.radio.companion_identity", return_value="bb" * 16),
            patch("envybot.radio.companion_in_desired_acl", return_value=True),
            patch(
                "envybot.radio.fetch_repeater_clock",
                new=AsyncMock(return_value=(1_700_000_000, True)),
            ),
        ):
            ok, err, clock = await maybe_admin_access(
                MagicMock(),
                _target(),
                node={},
                doc={},
                login_timeout=8,
                cmd_timeout=8,
                attempts=1,
                session=None,
                log=PollLog(progress=False),
            )
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(clock, 1_700_000_000)
