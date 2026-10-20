"""Refresh target resolution and job build."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.history import open_history, stamp_apply
from envybot.jobs import RadioJob
from envybot.refresh_registry import (
    REFRESH_ALL,
    REFRESH_SYNC,
    REFRESH_TARGETS,
    build_refresh_jobs,
    merge_refresh_jobs,
    resolve_refresh_work,
)
from envybot.radio import RouterTarget


class RefreshRegistryTests(unittest.TestCase):
    def test_resolve_status_pull_only(self) -> None:
        pull, apply = resolve_refresh_work(
            ["status"],
            conn=None,
            unit="me0001",
            node={},
            sites={},
            doc={"nodes": {}},
            keys={},
            seen=None,
            do_apply=True,
        )
        self.assertEqual(pull, ["status"])
        self.assertEqual(apply, set())

    def test_merge_skips_duplicate_login(self) -> None:
        existing = {"login", "get:status"}
        jobs = [
            RadioJob(kind="login", unit_key="me0001"),
            RadioJob(kind="get:telemetry", unit_key="me0001"),
        ]
        merged = merge_refresh_jobs(existing, jobs)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].kind, "get:telemetry")

    def test_full_preset_lists_all_groups(self) -> None:
        pull, _ = resolve_refresh_work(
            [REFRESH_ALL],
            conn=None,
            unit="me0001",
            node={"identity_pubkey": "a" * 64},
            sites={},
            doc={"nodes": {}},
            keys={},
            seen=None,
            do_apply=False,
        )
        self.assertIn("firmware", pull)
        self.assertIn("neighbors", pull[0:3])

    def test_sync_preset_live_groups(self) -> None:
        pull, _ = resolve_refresh_work(
            [REFRESH_SYNC],
            conn=None,
            unit="me0001",
            node={},
            sites={},
            doc={"nodes": {}},
            keys={},
            seen=None,
            do_apply=False,
        )
        self.assertEqual(pull[0], "neighbors")
        self.assertIn("status", pull)

    def test_apply_due_name_on_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            conn = open_history(book)
            node = {
                "unit_id": "ME0001",
                "identity_pubkey": "b" * 64,
                "name": "New",
            }
            stamp_apply(conn, unit="me0001", field="name", desired="Old", ok=True)
            pull, apply = resolve_refresh_work(
                [REFRESH_ALL],
                conn=conn,
                unit="me0001",
                node=node,
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                seen=None,
                do_apply=True,
            )
            self.assertIn("name", apply)

    def test_build_refresh_jobs_includes_login(self) -> None:
        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="me0001",
            site=None,
            pubkey_hex="c" * 64,
            admin_password="secret",
        )
        jobs = build_refresh_jobs(
            target,
            ["status"],
            conn=None,
            unit="me0001",
            node={},
            sites={},
            doc={"nodes": {}},
            keys={},
            seen=None,
            do_apply=False,
            do_poll=True,
            skip_discover=True,
            discover_wait=1.0,
        )
        kinds = [j.kind for j in jobs]
        self.assertEqual(kinds[0], "login")
        self.assertIn("get:status", kinds)

    def test_lat_target_maps_to_registry(self) -> None:
        spec = REFRESH_TARGETS["lat"]
        self.assertEqual(spec.apply_fields, ("lat", "lon"))


if __name__ == "__main__":
    unittest.main()
