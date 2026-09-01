"""Apply fail-fast when the first due SET gets no response."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from envybot.apply import apply_one
from envybot.history import open_history
from envybot.radio import PollLog, RouterTarget


class ApplyFailFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_no_response_skips_lat(self) -> None:
        target = RouterTarget(
            key="me0003",
            unit_id="ME0003",
            name="Ophir",
            site="ophir-hill",
            pubkey_hex="aa" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "name": "Ophir",
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        steps: list[str] = []
        log = PollLog(progress=False)
        log.step = lambda msg: steps.append(msg)  # type: ignore[method-assign]
        client = MagicMock()
        session = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(tmp)
            calls: list[str] = []

            async def fake_send_cmd_sync(_client, _target, cmd, **kwargs):
                calls.append(cmd)
                return None

            with patch("envybot.apply.send_cmd_sync", new=fake_send_cmd_sync):
                ok = await apply_one(
                    client,
                    target,
                    node=node,
                    doc={"nodes": {"me0003": node}},
                    sites=None,
                    cmd_timeout=9.0,
                    attempts=10,
                    session=session,
                    log=log,
                    conn=conn,
                    keys={},
                )

        self.assertFalse(ok)
        self.assertEqual(calls, ["set name Repeater"])
        self.assertTrue(any("apply aborted: name unreachable" in s for s in steps))
        self.assertFalse(any(cmd.startswith("set lat") for cmd in calls))

    async def test_first_due_field_caps_attempts(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="x",
        )
        node = {"name": "Test", "guest_password": "GuestOneStrong1"}
        log = PollLog()
        seen_attempts: list[int] = []

        async def fake_send_cmd_sync(_client, _target, cmd, **kwargs):
            seen_attempts.append(kwargs.get("attempts", 0))
            return None

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(tmp)
            with patch("envybot.apply.send_cmd_sync", new=fake_send_cmd_sync):
                await apply_one(
                    MagicMock(),
                    target,
                    node=node,
                    doc={"nodes": {"me0001": node}},
                    sites=None,
                    cmd_timeout=9.0,
                    attempts=10,
                    session=MagicMock(),
                    log=log,
                    conn=conn,
                    keys={},
                )

        self.assertEqual(seen_attempts, [2])

    async def test_identity_stamped_when_only_metadata_due(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="bb" * 32,
            admin_password="AdminOneStrong1",
        )
        node = {
            "name": "Test",
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
            "identity_pubkey": "cc" * 32,
        }
        steps: list[str] = []
        log = PollLog(progress=False)
        log.step = lambda msg: steps.append(msg)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(tmp)
            from envybot.apply import applicable_field_desireds

            applicable = applicable_field_desireds(node, None)
            for field, des in applicable.items():
                if field == "identity":
                    continue
                from envybot.history import insert_apply

                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)

            ok = await apply_one(
                MagicMock(),
                target,
                node=node,
                doc={"nodes": {"me0001": node}},
                sites=None,
                cmd_timeout=9.0,
                attempts=10,
                session=MagicMock(),
                log=log,
                conn=conn,
                keys={},
            )

        self.assertTrue(ok)
        self.assertTrue(any(s.startswith("profile OK") for s in steps))
        from envybot.history import last_ok_apply

        self.assertEqual(
            last_ok_apply(conn, "me0001", "identity"), applicable["identity"]
        )
