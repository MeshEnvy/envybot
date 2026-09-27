"""Radio silence streak and refresh bail."""

from __future__ import annotations

import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

from envybot.history import get_last_seen, merge_refresh_gaps, open_history, parse_refresh_gaps
from envybot.jobs import FleetScheduler, JobOutcome, RadioJob, UnitQueue
from envybot.radio import PollLog, RouterTarget
from envybot.silence_bail import (
    BAIL_PAYLOAD,
    GET_REPLY_UNPARSED,
    apply_silence_outcome,
)


def _target(key: str = "me0001") -> RouterTarget:
    return RouterTarget(
        key=key,
        unit_id=key.upper(),
        name=key.upper(),
        site=None,
        pubkey_hex="aa" * 32,
        admin_password="secret",
    )


class ApplySilenceOutcomeTests(unittest.TestCase):
    def test_three_silences_bail_and_drop_rest(self) -> None:
        job = RadioJob(kind="get:hop_retry_ms", unit_key="me0001")
        follow = RadioJob(kind="get:fem_rxgain", unit_key="me0001")
        uq = UnitQueue(target=_target(), jobs=deque([job, follow]))
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            for _ in range(2):
                out, pay = apply_silence_outcome(
                    uq,
                    job,
                    JobOutcome.TIMEOUT,
                    None,
                    conn=conn,
                    unit="me0001",
                )
                self.assertEqual(out, JobOutcome.TIMEOUT)
                self.assertIsNone(pay)
            out, pay = apply_silence_outcome(
                uq,
                job,
                JobOutcome.TIMEOUT,
                None,
                conn=conn,
                unit="me0001",
            )
            self.assertEqual(out, JobOutcome.TIMEOUT)
            self.assertEqual(pay, BAIL_PAYLOAD)
            self.assertEqual([j.kind for j in uq.jobs], ["get:hop_retry_ms"])
            gaps = parse_refresh_gaps((get_last_seen(conn, "me0001") or {}).get("refresh_gaps"))
            self.assertIn("hop_retry_ms", gaps)
            self.assertIn("fem_rxgain", gaps)

    def test_heard_resets_streak(self) -> None:
        job = RadioJob(kind="get:hop_retry", unit_key="me0001")
        uq = UnitQueue(target=_target(), jobs=deque([job]))
        apply_silence_outcome(
            uq, job, JobOutcome.TIMEOUT, None, conn=None, unit="me0001"
        )
        apply_silence_outcome(
            uq, job, JobOutcome.TIMEOUT, None, conn=None, unit="me0001"
        )
        apply_silence_outcome(
            uq, job, JobOutcome.HEARD, 0, conn=None, unit="me0001"
        )
        out, pay = apply_silence_outcome(
            uq, job, JobOutcome.TIMEOUT, None, conn=None, unit="me0001"
        )
        self.assertEqual(out, JobOutcome.TIMEOUT)
        self.assertIsNone(pay)

    def test_unparsed_does_not_increment_streak(self) -> None:
        job = RadioJob(kind="get:hop_retry", unit_key="me0001")
        uq = UnitQueue(target=_target(), jobs=deque([job]))
        for _ in range(3):
            out, pay = apply_silence_outcome(
                uq,
                job,
                JobOutcome.TIMEOUT,
                GET_REPLY_UNPARSED,
                conn=None,
                unit="me0001",
            )
            self.assertEqual(out, JobOutcome.TIMEOUT)
            self.assertEqual(pay, GET_REPLY_UNPARSED)
        self.assertEqual(len(uq.jobs), 1)


class BailSettleTests(unittest.TestCase):
    def test_bail_sets_cooldown_and_pops_job(self) -> None:
        sched = FleetScheduler(max_attempts=10, miss_cooldown=3600.0)
        job = RadioJob(kind="get:status", unit_key="me0001")
        uq = sched.get_or_create(_target())
        uq.jobs.append(job)
        uq.session_extra["bail_dropped"] = ["get:status"]
        before = time.monotonic()
        sched._settle_job(uq, job, JobOutcome.TIMEOUT, BAIL_PAYLOAD, {})
        self.assertFalse(uq.jobs)
        self.assertGreaterEqual(uq.cooldown_until, before + 3599.0)


class RefreshGapsHistoryTests(unittest.TestCase):
    def test_merge_and_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_history(Path(tmp))
            merge_refresh_gaps(conn, "me0001", ["hop_retry_ms"])
            merge_refresh_gaps(conn, "me0001", ["fem_rxgain"])
            gaps = parse_refresh_gaps(
                (get_last_seen(conn, "me0001") or {}).get("refresh_gaps")
            )
            self.assertEqual(gaps, ["hop_retry_ms", "fem_rxgain"])


if __name__ == "__main__":
    unittest.main()
