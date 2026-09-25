"""GET cadence: live vs inventory vs audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.history import open_history, record_poll
from envybot.poll import PollPolicy, group_is_due, partition_paused
from envybot.radio import RouterTarget


class _Res:
    def __init__(self, **kwargs):
        self.firmware_version = kwargs.get("firmware_version")
        self.bootloader_version = kwargs.get("bootloader_version")
        self.base_hash = kwargs.get("base_hash")
        self.firmware_platform = kwargs.get("firmware_platform")
        self.name = kwargs.get("name")
        self.lat = kwargs.get("lat")
        self.lon = kwargs.get("lon")
        self.node_clock = None
        self.status = kwargs.get("status")
        self.telemetry = kwargs.get("telemetry")
        self.advert_interval_min = kwargs.get("advert_interval_min")
        self.flood_advert_interval_h = kwargs.get("flood_advert_interval_h")
        self.acl = kwargs.get("acl")
        self.neighbors = kwargs.get("neighbors")
        self.polled_groups = kwargs.get("polled_groups", frozenset())
        self.ota = kwargs.get("ota")


class JobStageTests(unittest.TestCase):
    def test_known_kinds(self) -> None:
        from envybot.poll import in_flight_session, job_stage_label

        self.assertEqual(job_stage_label("login"), "Logging in")
        self.assertEqual(job_stage_label("get:ota"), "Fetching OTA")
        self.assertEqual(job_stage_label("get:acl"), "Fetching ACL")
        self.assertEqual(job_stage_label("get:status"), "Fetching status")
        self.assertEqual(job_stage_label("apply:acl"), "Setting ACL")
        self.assertEqual(job_stage_label(None), "Queued")

    def test_in_flight_keeps_manual_state(self) -> None:
        from envybot.poll import in_flight_session

        sess = in_flight_session(
            manual_job="sync",
            job_kind="login",
            attempt=3,
            max_attempts=10,
            queued=True,
        )
        self.assertEqual(sess["state"], "syncing")
        self.assertEqual(sess["stage"], "Logging in")
        self.assertEqual(sess["attempt"], 3)
        self.assertTrue(sess["manual"])

    def test_in_flight_auto_queued(self) -> None:
        from envybot.poll import in_flight_session

        sess = in_flight_session(manual_job=None, job_kind="get:acl", queued=True)
        self.assertEqual(sess["state"], "queued")
        self.assertEqual(sess["stage"], "Fetching ACL")


class ManualDueGroupsTests(unittest.TestCase):
    def test_refresh_is_live_only(self) -> None:
        from envybot.poll import DAILY_GROUPS, GET_GROUP_ORDER, LIVE_GROUPS, refresh_due_groups

        self.assertEqual(refresh_due_groups(), list(LIVE_GROUPS))
        self.assertEqual(set(LIVE_GROUPS), {"status", "telemetry"})
        self.assertEqual(set(DAILY_GROUPS), {"ota_status", "ota_ls", "neighbors"})
        for group in refresh_due_groups():
            self.assertIn(group, GET_GROUP_ORDER)
            self.assertNotIn(group, DAILY_GROUPS)

    def test_pull_is_all_groups(self) -> None:
        from envybot.poll import GET_GROUP_ORDER, pull_due_groups

        self.assertEqual(pull_due_groups(), list(GET_GROUP_ORDER))


class PollCadenceTests(unittest.TestCase):
    def test_audit_groups_skip_by_default(self) -> None:
        policy = PollPolicy(force=False, min_interval=86400.0)
        now = 1_700_000_000
        seen = {
            "name_at": now - 100,
            "gps_at": now - 100,
            "advert_at": now - 100,
            "flood_advert_at": now - 100,
            "acl_at": now - 100,
        }
        for group in ("name", "lat", "lon", "advert", "flood_advert", "acl"):
            self.assertFalse(group_is_due(seen, group, policy=policy, now=now))

    def test_audit_groups_excluded_from_force(self) -> None:
        policy = PollPolicy(force=True)
        now = 2_000_000_000
        seen = {
            "name_at": now - 100,
            "gps_at": now - 100,
            "advert_at": now - 100,
            "flood_advert_at": now - 100,
            "acl_at": now - 100,
            "status_at": now - 100,
        }
        self.assertFalse(group_is_due(seen, "name", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "status", policy=policy, now=now + 4000))

    def test_audit_groups_on_force_groups(self) -> None:
        policy = PollPolicy(force=False, force_groups=frozenset({"name"}))
        seen = {"name_at": 1_700_000_000}
        self.assertTrue(group_is_due(seen, "name", policy=policy, now=1_700_000_100))

    def test_audit_groups_weekly_interval(self) -> None:
        from envybot.radio import AUDIT_POLL_INTERVAL

        policy = PollPolicy(force=False)
        now = 1_700_000_000
        seen = {"name_at": now - int(AUDIT_POLL_INTERVAL) + 100}
        self.assertFalse(group_is_due(seen, "name", policy=policy, now=now))
        self.assertTrue(
            group_is_due(seen, "name", policy=policy, now=now + AUDIT_POLL_INTERVAL)
        )

    def test_periodic_respects_interval(self) -> None:
        policy = PollPolicy(min_interval=3600.0)
        now = 1_700_000_000
        seen = {"status_at": now - 100}
        self.assertFalse(group_is_due(seen, "status", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "status", policy=policy, now=now + 4000))

    def test_status_hourly_neighbors_daily(self) -> None:
        policy = PollPolicy(min_interval=3600.0)
        now = 1_700_000_000
        seen = {
            "status_at": now - 4000,
            "telemetry_at": now - 4000,
            "neighbors_at": now - 4000,
        }
        self.assertTrue(group_is_due(seen, "status", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "telemetry", policy=policy, now=now))
        self.assertFalse(group_is_due(seen, "neighbors", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "neighbors", policy=policy, now=now + 86400))

    def test_ota_daily_like_neighbors(self) -> None:
        policy = PollPolicy(min_interval=3600.0)
        now = 1_700_000_000
        seen = {
            "ota_status_at": now - 4000,
            "ota_ls_at": now - 4000,
        }
        self.assertFalse(group_is_due(seen, "ota_status", policy=policy, now=now))
        self.assertFalse(group_is_due(seen, "ota_ls", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "ota_status", policy=policy, now=now + 86400))
        self.assertTrue(group_is_due(seen, "ota_ls", policy=policy, now=now + 86400))

    def test_ota_skipped_when_firmware_has_no_cli(self) -> None:
        from envybot.history import mark_ota_unsupported
        from envybot.poll import due_groups, format_get_plan, ota_cli_absent

        policy = PollPolicy(min_interval=3600.0)
        now = 1_700_000_000
        seen = {
            "firmware_version": "1.14.0",
            "ota_unsupported": 1,
            "ota_unsupported_fw": "1.14.0",
        }
        self.assertTrue(ota_cli_absent(seen))
        for group in ("ota", "ota_status", "ota_ls"):
            self.assertFalse(group_is_due(seen, group, policy=policy, now=now + 86400))
        seen_new_fw = {**seen, "firmware_version": "1.17.1"}
        self.assertFalse(ota_cli_absent(seen_new_fw))
        self.assertTrue(group_is_due(seen_new_fw, "ota_status", policy=policy, now=now + 86400))

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(firmware_version="1.14.0", polled_groups=frozenset({"firmware"})),
            )
            mark_ota_unsupported(conn, "me0001", firmware_version="1.14.0")
            due = due_groups(conn, "me0001", policy=policy, now=now)
            self.assertNotIn("ota", due)
            self.assertNotIn("ota_status", due)
            self.assertNotIn("ota_ls", due)
            _need, skip = format_get_plan(conn, "me0001", due, policy=policy, now=now)
            self.assertIn("no CLI", skip)

    def test_inventory_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            policy = PollPolicy()
            now = 1_700_000_000
            self.assertTrue(group_is_due(None, "firmware", policy=policy, now=now))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(firmware_version="1.15.0", polled_groups=frozenset({"firmware"})),
            )
            seen = conn.execute("SELECT * FROM last_seen WHERE unit = 'me0001'").fetchone()
            seen = dict(seen)
            self.assertFalse(group_is_due(seen, "firmware", policy=policy, now=now + 99999))

    def test_ota_inventory_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            policy = PollPolicy()
            now = 1_700_000_000
            self.assertTrue(group_is_due(None, "ota", policy=policy, now=now))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(base_hash="AABBCCDDEEFF0011", polled_groups=frozenset({"ota"})),
            )
            seen = conn.execute("SELECT * FROM last_seen WHERE unit = 'me0001'").fetchone()
            seen = dict(seen)
            self.assertEqual(seen["base_hash"], "AABBCCDDEEFF0011")
            self.assertFalse(group_is_due(seen, "ota", policy=policy, now=now + 99999))


class TrustStampTests(unittest.TestCase):
    def test_stamp_after_trust_when_reconciled(self) -> None:
        from envybot.apply import (
            applicable_field_desireds,
            profile_id,
            stamp_profile_after_trust,
        )

        node = {
            "name": "Ophir",
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
            "identity_pubkey": "aa" * 32,
        }
        doc_before = {"nodes": {"me0001": node}, "trust": {"admin": ["ben"]}}
        keys_before = {"ben": ["cc" * 32]}
        doc_after = {"nodes": {"me0001": node}, "trust": {"admin": ["ben", "bill"]}}
        keys_after = {
            "ben": ["cc" * 32],
            "bill": ["dd" * 32],
        }
        pre = profile_id(node, None, doc=doc_before, keys=keys_before)
        post = profile_id(node, None, doc=doc_after, keys=keys_after)
        self.assertNotEqual(pre, post)

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            from envybot.history import insert_apply

            for field, des in applicable_field_desireds(
                node, None, doc=doc_before, keys=keys_before, key="me0001"
            ).items():
                insert_apply(conn, unit="me0001", field=field, desired=des, ok=True)
            self.assertTrue(
                stamp_profile_after_trust(
                    conn,
                    "me0001",
                    node,
                    None,
                    pre_apply_hash=pre,
                    doc=doc_after,
                    keys=keys_after,
                    doc_before=doc_before,
                    keys_before=keys_before,
                )
            )
            from envybot.history import last_ok_apply

            post_acl = applicable_field_desireds(
                node, None, doc=doc_after, keys=keys_after
            )["acl"]
            self.assertEqual(last_ok_apply(conn, "me0001", "acl"), post_acl)

    def test_no_stamp_when_never_synced(self) -> None:
        from envybot.apply import (
            applicable_field_desireds,
            profile_id,
            stamp_profile_after_trust,
        )

        node = {"name": "Ophir", "guest_password": "GuestOneStrong1"}
        doc = {"trust": {"admin": ["ben"]}}
        keys = {"ben": ["cc" * 32]}
        pre = profile_id(node, None, doc={"trust": {}}, keys={})
        post = profile_id(node, None, doc=doc, keys=keys)

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            self.assertFalse(
                stamp_profile_after_trust(
                    conn,
                    "me0001",
                    node,
                    None,
                    pre_apply_hash=pre,
                    doc=doc,
                    keys=keys,
                    doc_before={"trust": {}},
                    keys_before={},
                )
            )
            from envybot.history import last_ok_apply

            self.assertIsNone(last_ok_apply(conn, "me0001", "profile"))
            self.assertNotEqual(pre, post)


def _target(key: str) -> RouterTarget:
    return RouterTarget(
        key=key,
        unit_id=key.upper(),
        name=key,
        site=None,
        pubkey_hex="a" * 64,
        admin_password="pw",
    )


class PartitionPausedTests(unittest.TestCase):
    def test_splits_paused_unless_forced(self) -> None:
        targets = [_target("me0001"), _target("me0002"), _target("me0003")]
        nodes = {
            "me0001": {"paused": True},
            "me0002": {},
            "me0003": {"paused": True},
        }
        active, paused = partition_paused(targets, nodes)
        self.assertEqual([t.key for t in active], ["me0002"])
        self.assertEqual([t.key for t in paused], ["me0001", "me0003"])
        active, paused = partition_paused(targets, nodes, forced_keys={"me0001"})
        self.assertEqual([t.key for t in active], ["me0001", "me0002"])
        self.assertEqual([t.key for t in paused], ["me0003"])


class GetPlanTests(unittest.TestCase):
    def test_format_get_plan_need_and_skip(self) -> None:
        from envybot.poll import format_get_plan

        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            record_poll(
                conn,
                unit="me0001",
                res=_Res(
                    firmware_version="1.14.0",
                    status={"uptime_secs": 1},
                    telemetry=[1],
                    polled_groups=frozenset(
                        {"firmware", "status", "telemetry", "neighbors"}
                    ),
                ),
            )
            need, skip = format_get_plan(
                conn,
                "me0001",
                ["status", "telemetry"],
                policy=PollPolicy(min_interval=86400.0),
                now=1_700_086_400,
            )
        self.assertIn("status", need)
        self.assertIn("telemetry", need)
        self.assertIn("(have)", skip)
        self.assertIn("neighbors (fresh", skip)
        self.assertIn("name", skip)
        self.assertIn("(audit <", skip)


class SeedAutoWorkTests(unittest.TestCase):
    def test_seed_skips_busy_and_queues_idle(self) -> None:
        from envybot.commands.fleet import _seed_auto_work
        from envybot.jobs import FleetScheduler, RadioJob

        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="a" * 64,
            admin_password="secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            sched = FleetScheduler()
            first = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": {"unit_id": "ME0001"}},
                sites={},
                doc={"nodes": {}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=False,
                force=False,
                now=1_700_000_000,
            )
            self.assertEqual(first, 1)
            queued = len(sched.units["me0001"].jobs)
            self.assertGreater(queued, 0)
            second = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": {"unit_id": "ME0001"}},
                sites={},
                doc={"nodes": {}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=False,
                force=False,
                now=1_700_000_000,
            )
            self.assertEqual(second, 0)
            self.assertEqual(len(sched.units["me0001"].jobs), queued)
            sched.units["me0001"].jobs.clear()
            sched.enqueue_jobs(target, [RadioJob(kind="login", unit_key="me0001")])
            busy = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": {"unit_id": "ME0001"}},
                sites={},
                doc={"nodes": {}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=False,
                force=False,
                now=1_700_000_000,
            )
            self.assertEqual(busy, 0)
            self.assertEqual(len(sched.units["me0001"].jobs), 1)

    def test_seed_splices_apply_onto_busy_unit(self) -> None:
        from envybot.commands.fleet import _seed_auto_work
        from envybot.jobs import FleetScheduler, RadioJob

        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="a" * 64,
            admin_password="AdminOneStrong1",
        )
        node = {
            "unit_id": "ME0001",
            "admin_password": "AdminOneStrong1",
            "guest_password": "GuestOneStrong1",
            "identity_pubkey": "aa" * 32,
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            sched = FleetScheduler()
            sched.enqueue_jobs(
                target,
                [
                    RadioJob(kind="login", unit_key="me0001"),
                    RadioJob(kind="get:status", unit_key="me0001"),
                ],
            )
            seeded = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": node},
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=True,
                force=False,
                now=1_700_000_000,
            )
            self.assertEqual(seeded, 1)
            kinds = [j.kind for j in sched.units["me0001"].jobs]
            self.assertEqual(kinds[0], "login")
            self.assertIn("apply:name", kinds)
            self.assertLess(kinds.index("apply:name"), kinds.index("get:status"))
            again = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": node},
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=True,
                force=False,
                now=1_700_000_000,
            )
            self.assertEqual(again, 0)
            self.assertEqual([j.kind for j in sched.units["me0001"].jobs], kinds)

    def test_seed_skips_fresh_periodic_on_restart(self) -> None:
        from envybot.commands.fleet import _seed_auto_work
        from envybot.history import record_poll
        from envybot.jobs import FleetScheduler

        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="a" * 64,
            admin_password="AdminOneStrong1",
        )
        node = {
            "unit_id": "ME0001",
            "admin_password": "AdminOneStrong1",
            "guest_password": "GuestOneStrong1",
            "identity_pubkey": "aa" * 32,
            "full_sync_interval": "off",
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            now = 1_700_000_000
            record_poll(
                conn,
                unit="me0001",
                res=_Res(
                    firmware_version="v1.0.0",
                    bootloader_version="1",
                    base_hash="abc",
                    name="ME0001",
                    lat=0.0,
                    lon=0.0,
                    advert_interval_min=0,
                    flood_advert_interval_h=0,
                    acl=[],
                    status={"battery_mv": 3900},
                    telemetry=[{"channel": "power", "type": "voltage", "value": 3.9}],
                    neighbors=[],
                    ota={"running": {}, "local": {"state": "none"}, "heard": []},
                    polled_groups=frozenset(
                        {
                            "firmware",
                            "bootloader",
                            "ota",
                            "name",
                            "lat",
                            "lon",
                            "advert",
                            "flood_advert",
                            "acl",
                            "status",
                            "telemetry",
                            "ota_status",
                            "ota_ls",
                            "neighbors",
                            "repeat",
                            "path_hash",
                            "dutycycle",
                            "powersaving",
                            "hop_retry",
                            "hop_retry_ms",
                            "fem_rxgain",
                            "agc_reset_interval",
                            "rxgain",
                            "ota_autofetch",
                        }
                    ),
                ),
                ts=now - 300,
            )
            from envybot.full_sync import stamp_full_sync

            stamp_full_sync(conn, "me0001", ts=now - 300)
            sched = FleetScheduler()
            session_states: dict[str, dict] = {}
            seeded = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": node},
                sites={},
                doc={"nodes": {"me0001": node}},
                keys={},
                policy=PollPolicy(min_interval=3600.0),
                do_poll=True,
                do_apply=False,
                force=False,
                now=now,
                session_states=session_states,
            )
            self.assertEqual(seeded, 0)
            self.assertNotIn("me0001", session_states)

    def test_seed_respects_cooldown_without_false_queued_state(self) -> None:
        import time

        from envybot.commands.fleet import _seed_auto_work
        from envybot.jobs import FleetScheduler

        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="a" * 64,
            admin_password="secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            sched = FleetScheduler(miss_cooldown=3600.0)
            uq = sched.get_or_create(target)
            uq.cooldown_until = time.monotonic() + 3600.0
            session_states: dict[str, dict] = {}
            seeded = _seed_auto_work(
                sched,
                auto_targets=[target],
                conn=conn,
                nodes={"me0001": {"unit_id": "ME0001"}},
                sites={},
                doc={"nodes": {}},
                keys={},
                policy=PollPolicy(),
                do_poll=True,
                do_apply=False,
                force=False,
                now=1_700_000_000,
                session_states=session_states,
            )
            self.assertEqual(seeded, 0)
            self.assertNotIn("me0001", session_states)


class BuildPollJobsTests(unittest.TestCase):
    def test_apply_runs_after_login_before_get(self) -> None:
        from envybot.fleet_worker import build_poll_jobs

        target = RouterTarget(
            key="me0001",
            unit_id="ME0001",
            name="Test",
            site=None,
            pubkey_hex="a" * 64,
            admin_password="secret",
        )
        jobs = build_poll_jobs(
            target,
            ["status", "telemetry"],
            do_apply=True,
            apply_due=True,
            force_apply=False,
            skip_discover=True,
        )
        kinds = [j.kind for j in jobs]
        self.assertEqual(kinds[0], "login")
        self.assertIn("apply:name", kinds)
        self.assertLess(kinds.index("apply:name"), kinds.index("get:status"))
        self.assertLess(kinds.index("apply:clock"), kinds.index("get:status"))


if __name__ == "__main__":
    unittest.main()
