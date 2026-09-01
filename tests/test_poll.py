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
        self.bootloader_version = None
        self.firmware_platform = None
        self.name = None
        self.lat = None
        self.lon = None
        self.node_clock = None
        self.status = kwargs.get("status")
        self.telemetry = kwargs.get("telemetry")
        self.advert_interval_min = None
        self.flood_advert_interval_h = None
        self.acl = None
        self.neighbors = None
        self.polled_groups = kwargs.get("polled_groups", frozenset())


class JobStageTests(unittest.TestCase):
    def test_known_kinds(self) -> None:
        from envybot.poll import in_flight_session, job_stage_label

        self.assertEqual(job_stage_label("login"), "Logging in")
        self.assertEqual(job_stage_label("get:acl"), "Fetching ACL")
        self.assertEqual(job_stage_label("get:status"), "Fetching status")
        self.assertEqual(job_stage_label("apply:acl"), "Setting ACL")
        self.assertEqual(job_stage_label(None), "Queued")

    def test_in_flight_keeps_manual_state(self) -> None:
        from envybot.poll import in_flight_session

        sess = in_flight_session(
            manual_job="refresh",
            job_kind="login",
            attempt=3,
            max_attempts=10,
            queued=True,
        )
        self.assertEqual(sess["state"], "refreshing")
        self.assertEqual(sess["stage"], "Logging in")
        self.assertEqual(sess["attempt"], 3)
        self.assertTrue(sess["manual"])

    def test_in_flight_auto_queued(self) -> None:
        from envybot.poll import in_flight_session

        sess = in_flight_session(manual_job=None, job_kind="get:acl", queued=True)
        self.assertEqual(sess["state"], "queued")
        self.assertEqual(sess["stage"], "Fetching ACL")


class ManualDueGroupsTests(unittest.TestCase):
    def test_refresh_is_periodic_only(self) -> None:
        from envybot.poll import GET_GROUP_ORDER, PERIODIC_GROUPS, refresh_due_groups

        self.assertEqual(refresh_due_groups(), list(PERIODIC_GROUPS))
        for group in refresh_due_groups():
            self.assertIn(group, GET_GROUP_ORDER)

    def test_pull_is_all_groups(self) -> None:
        from envybot.poll import GET_GROUP_ORDER, pull_due_groups

        self.assertEqual(pull_due_groups(), list(GET_GROUP_ORDER))


class PollCadenceTests(unittest.TestCase):
    def test_audit_groups_skip_by_default(self) -> None:
        policy = PollPolicy(force=False, min_interval=86400.0)
        now = 1_700_000_000
        seen = {"name_at": now - 100, "gps_at": now - 100, "acl_at": now - 100}
        for group in ("name", "lat", "lon", "advert", "flood_advert", "acl"):
            self.assertFalse(group_is_due(seen, group, policy=policy, now=now))

    def test_audit_groups_on_force(self) -> None:
        policy = PollPolicy(force=True)
        seen = {"name_at": 1, "status_at": 1}
        self.assertTrue(group_is_due(seen, "name", policy=policy, now=2_000_000_000))

    def test_periodic_respects_interval(self) -> None:
        policy = PollPolicy(min_interval=3600.0)
        now = 1_700_000_000
        seen = {"status_at": now - 100}
        self.assertFalse(group_is_due(seen, "status", policy=policy, now=now))
        self.assertTrue(group_is_due(seen, "status", policy=policy, now=now + 4000))

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

            insert_apply(conn, unit="me0001", field="profile", desired=pre, ok=True)
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
        self.assertIn("(audit)", skip)


if __name__ == "__main__":
    unittest.main()
