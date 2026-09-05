"""Tests for OTA CLI reply parsers."""

from __future__ import annotations

import unittest

from envybot.ota_parse import (
    merge_ota_snapshot,
    ota_badge,
    ota_ls_heard_empty,
    ota_status_heard_empty,
    parse_ota_ls,
    parse_ota_stats,
    parse_ota_status,
)


class ParseOtaStatsTests(unittest.TestCase):
    def test_idle_fetch(self) -> None:
        raw = (
            "OTA | fw v1.17.1-ev1 id=deadbeef body=abcd1234 100b 434K | serv 0 dg=cafebabe | "
            "fetch idle | af=off hops=2"
        )
        parsed = parse_ota_stats(raw)
        assert parsed is not None
        self.assertEqual(parsed["running"]["body_hash"], "ABCD1234")
        self.assertEqual(parsed["running"]["serving_count"], 0)
        self.assertEqual(parsed["local"]["state"], "none")

    def test_active_fetch(self) -> None:
        raw = (
            "OTA | fw v1.17.0-ev1 id=11111111 body=22222222 50b 400K | serv 1 dg=33333333 | "
            "fetch dl 25/100 25% id=aabbccdd 120s | af=signed hops=4"
        )
        parsed = parse_ota_stats(raw)
        assert parsed is not None
        self.assertEqual(parsed["local"]["state"], "downloading")
        self.assertEqual(parsed["local"]["pct"], 25)


class ParseOtaStatusTests(unittest.TestCase):
    def test_idle_no_download(self) -> None:
        raw = (
            "OTA | this fw ABCD1234 (434K) hw=RAK4631 | no download | serving:off (0) | "
            "keys:0 | target:5C6AB408 (RAK_4631_repeater_slim) | bl:apply blrc:00"
        )
        parsed = parse_ota_status(raw)
        assert parsed is not None
        running = parsed["running"]
        self.assertEqual(running["body_hash"], "ABCD1234")
        self.assertEqual(running["image_kib"], 434)
        self.assertEqual(running["hw_id"], "RAK4631")
        self.assertEqual(running["target_id"], "5c6ab408")
        self.assertEqual(running["target_env"], "RAK_4631_repeater_slim")
        self.assertFalse(running["serving"])
        self.assertEqual(running["serving_count"], 0)
        self.assertEqual(running["keys"], 0)
        self.assertTrue(running["bl_apply"])
        self.assertEqual(running["bl_rc"], "00")
        self.assertNotIn("seed", running)
        self.assertEqual(parsed["local"]["state"], "none")

    def test_ready_download(self) -> None:
        raw = (
            "OTA | this fw ABCD1234 (434K) hw=RAK4631 | download: ready to install 100/100 "
            "(100%) id=deadbeef 3600s | serving:on (2) | keys:1 | target:5C6AB408 (env) | "
            "bl:NONE blrc:B8 | seed:on"
        )
        parsed = parse_ota_status(raw)
        assert parsed is not None
        running = parsed["running"]
        self.assertEqual(parsed["local"]["state"], "ready")
        self.assertEqual(parsed["local"]["mid"], "deadbeef")
        self.assertEqual(parsed["local"]["pct"], 100)
        self.assertTrue(running["serving"])
        self.assertEqual(running["serving_count"], 2)
        self.assertEqual(running["keys"], 1)
        self.assertFalse(running["bl_apply"])
        self.assertEqual(running["bl_rc"], "B8")
        self.assertTrue(running["seed"])

    def test_unknown_hw_and_fw(self) -> None:
        raw = (
            "OTA | this fw ? (0K) hw=? | no download | serving:off (0) | "
            "keys:0 | target:00000000 (?)"
        )
        parsed = parse_ota_status(raw)
        assert parsed is not None
        running = parsed["running"]
        self.assertNotIn("body_hash", running)
        self.assertEqual(running["image_kib"], 0)
        self.assertIsNone(running["hw_id"])
        self.assertEqual(running["target_env"], "?")

    def test_unknown_ota(self) -> None:
        self.assertTrue(ota_status_heard_empty("Unknown OTA command. Type `ota help`."))
        parsed = parse_ota_status("Unknown OTA command. Type `ota help`.")
        assert parsed is not None
        self.assertEqual(parsed["local"]["state"], "none")

    def test_unknown_command_help_is_missing_cli(self) -> None:
        from envybot.ota_parse import ota_cli_missing

        self.assertTrue(ota_cli_missing("Unknown command"))
        self.assertTrue(ota_cli_missing("Unknown command. Type `ota help`."))
        self.assertTrue(ota_cli_missing("Unknown OTA command. Type `ota help`."))
        self.assertFalse(ota_cli_missing("unknown config: radio.fem.rxgain off"))
        self.assertFalse(ota_cli_missing("ERR no EndF (firmware lacks the trailer?)"))
        self.assertTrue(ota_status_heard_empty("Unknown command. Type `help`."))


class ParseOtaLsTests(unittest.TestCase):
    def test_catalog_rows(self) -> None:
        raw = (
            "Updates nearby (2 src) — `ota get <#>` to download:\n"
            " 1) v1.17.1-ev1 delta [yours] 3n 120s [ready]\n"
            " 2) v1.17.0-ev1 delta [RAK_4631_repeater_slim] 1n 400s"
        )
        heard = parse_ota_ls(raw)
        assert heard is not None
        self.assertEqual(len(heard), 2)
        self.assertTrue(heard[0]["yours"])
        self.assertEqual(heard[0]["index"], 1)
        self.assertEqual(heard[0]["session_tag"], "ready")

    def test_empty_catalog(self) -> None:
        raw = "No updates seen yet — re-run `ota ls` in a few seconds (just asked around)."
        self.assertTrue(ota_ls_heard_empty(raw))
        self.assertEqual(parse_ota_ls(raw), [])


class OtaSnapshotTests(unittest.TestCase):
    def test_merge_and_badge(self) -> None:
        snap = merge_ota_snapshot(
            None,
            status=parse_ota_status(
                "OTA | this fw ABCD1234 (434K) hw=x | download: ready to install 10/10 "
                "(100%) id=aa11bb22 1s | serving:off (0) | keys:0 | target:01020304 (t)"
            ),
            heard=parse_ota_ls(
                "Updates nearby (1 src) — `ota get <#>` to download:\n"
                " 1) v1.17.1-ev1 delta [yours] 2n 5s"
            ),
        )
        self.assertEqual(ota_badge(snap), "staged")

    def test_sees_update_badge(self) -> None:
        snap = merge_ota_snapshot(
            {"local": {"state": "none"}},
            heard=parse_ota_ls(
                "Updates nearby (1 src) — `ota get <#>` to download:\n"
                " 1) v1.17.1-ev1 delta [yours] 2n 5s"
            ),
        )
        self.assertEqual(ota_badge(snap), "sees update")


class OtaStatusGapTests(unittest.TestCase):
    """COMPLETE is not IDLE in firmware; status shows download line until install/cancel."""

    def test_complete_not_reported_as_no_download(self) -> None:
        raw = (
            "OTA | this fw ABCD1234 (434K) hw=x | download: ready to install 50/50 "
            "(100%) id=cafebabe 10s | serving:off (0) | keys:0 | target:01020304 (t)"
        )
        parsed = parse_ota_status(raw)
        assert parsed is not None
        self.assertNotEqual(parsed["local"]["state"], "none")
        self.assertEqual(parsed["local"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
