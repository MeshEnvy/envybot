"""Console CLI replies that match poll groups stamp history."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pathlib import Path

from envybot.fleet_worker import (
    _console_capture_tracked,
    _console_poll_group,
)
from envybot.history import get_last_seen, latest_ota, open_history
from envybot.jobs import UnitQueue
from envybot.radio import PollLog, RouterTarget


OTA_STATUS_SAMPLE = (
    "OTA | this fw ABCD1234 (434K) hw=RAK4631 | download: ready to install 100/100 "
    "(100%) id=deadbeef 3600s | serving:on (2) | keys:1 | target:5C6AB408 (env) | "
    "bl:NONE blrc:B8 | seed:on"
)


class ConsoleCaptureTests(unittest.TestCase):
    def test_poll_group_map(self) -> None:
        self.assertEqual(_console_poll_group("ota status"), "ota_status")
        self.assertEqual(_console_poll_group("  VER "), "firmware")
        self.assertEqual(_console_poll_group("get name"), "name")
        self.assertIsNone(_console_poll_group("reboot"))
        self.assertIsNone(_console_poll_group("ota install"))

    def test_ota_status_stamps_last_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            target = RouterTarget(
                key="me0032",
                unit_id="ME0032",
                name="Test",
                site=None,
                pubkey_hex="aa" * 32,
                admin_password="AdminOneStrong1",
            )
            uq = UnitQueue(target=target)
            ctx = SimpleNamespace(
                conn=conn,
                nodes={"me0032": {}},
                sites={},
                log=PollLog(progress=False),
            )
            group = _console_capture_tracked(ctx, uq, target, "ota status", OTA_STATUS_SAMPLE)
            self.assertEqual(group, "ota_status")
            seen = get_last_seen(conn, "me0032")
            self.assertIsNotNone(seen)
            assert seen is not None
            self.assertIsNotNone(seen.get("ota_status_at"))
            ota = latest_ota(conn, "me0032")
            self.assertIsNotNone(ota)
            assert ota is not None
            self.assertEqual((ota.get("local") or {}).get("state"), "ready")

    def test_ver_stamps_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            target = RouterTarget(
                key="me0032",
                unit_id="ME0032",
                name="Test",
                site=None,
                pubkey_hex="aa" * 32,
                admin_password="AdminOneStrong1",
            )
            uq = UnitQueue(target=target)
            ctx = SimpleNamespace(
                conn=conn,
                nodes={"me0032": {}},
                sites={},
                log=PollLog(progress=False),
            )
            group = _console_capture_tracked(
                ctx, uq, target, "ver", "v1.15.0  (Build: Mar  1 2026)"
            )
            self.assertEqual(group, "firmware")
            seen = get_last_seen(conn, "me0032")
            self.assertIsNotNone(seen)
            assert seen is not None
            self.assertEqual(seen.get("firmware_version"), "v1.15.0")


if __name__ == "__main__":
    unittest.main()
