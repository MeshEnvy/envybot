"""Repeater USB CLI framing. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.apply import apply_is_due
from envybot.commands.onboard import (
    apply_admin_password,
    apply_agc_reset_interval_policy,
    apply_ota_autofetch_policy,
    apply_path_hash_policy,
    antenna_ready,
    stamp_fleet_ready,
    wait_usb_gone,
)
from envybot.onboard_book import (
    flash_target_from_board,
    ensure_onboard_unit,
    ensure_reonboard_unit,
    register,
    resolve_unit,
    should_mint_fleet_identity,
)
from envybot.onboard_serial_apply import mint_identity_on_usb, restore_book_identity_on_usb
from envybot.repeater_serial import CliError, RepeaterSerial
from envybot.radio import parse_ota_autofetch
from envybot.history import open_history
from envybot.keys_doc import parse_serial_acl
from envybot.nodes_doc import write_nodes_doc

BEN = "aa" * 32


class CmdRetryTests(unittest.TestCase):
    def test_cmd_retries_transient_silence(self) -> None:
        reads = iter([b"", b"", b"  -> 0\n"])

        class FakePort:
            def read(self, _n: int) -> bytes:
                return next(reads, b"")

            def reset_input_buffer(self) -> None:
                pass

            def write(self, _data: bytes) -> None:
                pass

            def flush(self) -> None:
                pass

            is_open = True

            def close(self) -> None:
                pass

        cli = RepeaterSerial.__new__(RepeaterSerial)
        cli.port = "/dev/fake"
        cli.timeout = 1.0
        cli.verbose = False
        cli.wait_for_reply = True
        cli.ser = FakePort()

        reply = cli.send_command("get advert.interval")
        self.assertEqual(reply, "0")


class ExtractReplyTests(unittest.TestCase):
    def test_arrow_reply(self) -> None:
        buf = "ver\n  -> 1.17.1.1 (Build: 30Aug2026)\n"
        self.assertEqual(
            RepeaterSerial._extract_reply(buf),
            "1.17.1.1 (Build: 30Aug2026)",
        )
        self.assertIsNotNone(RepeaterSerial._reply_start(buf))

    def test_arrow_reply_uses_last_line_after_noise(self) -> None:
        buf = "tail off\nver\n  -> 1.18.0.3 (Build: 18 Oct 2026)\n"
        self.assertEqual(
            RepeaterSerial._extract_reply(buf),
            "1.18.0.3 (Build: 18 Oct 2026)",
        )

    def test_acl_dump_without_arrow(self) -> None:
        buf = f"get acl\nACL:\n03 {BEN}\n"
        reply = RepeaterSerial._extract_reply(buf)
        self.assertIsNotNone(reply)
        self.assertTrue(reply.startswith("ACL:"))
        rows = parse_serial_acl(reply)
        self.assertEqual(rows[0]["perm"], 3)
        self.assertEqual(rows[0]["key"], BEN)

    def test_empty_acl_without_arrow(self) -> None:
        buf = "get acl\nACL:\n"
        reply = RepeaterSerial._extract_reply(buf)
        self.assertEqual(reply, "ACL:")
        self.assertEqual(parse_serial_acl(reply), [])

    def test_no_reply(self) -> None:
        self.assertIsNone(RepeaterSerial._extract_reply("get acl\n"))
        self.assertIsNone(RepeaterSerial._reply_start("get acl\n"))


class FakeCli:
    def __init__(self, replies: dict[str, str | list[str]]) -> None:
        self.replies = replies
        self.sent: list[str] = []

    def cmd(self, command: str, **_kwargs: object) -> str:
        self.sent.append(command)
        val = self.replies[command]
        if isinstance(val, list):
            return val.pop(0)
        return val


class PathHashPolicyTests(unittest.TestCase):
    def test_already_two_byte(self) -> None:
        cli = FakeCli({"get path.hash.mode": "> 1"})
        self.assertFalse(apply_path_hash_policy(cli, {}, force=False))
        self.assertEqual(cli.sent, ["get path.hash.mode"])

    def test_sets_one_byte_to_two(self) -> None:
        cli = FakeCli(
            {
                "get path.hash.mode": ["> 0", "> 1"],
                "set path.hash.mode 1": "OK",
            }
        )
        self.assertTrue(apply_path_hash_policy(cli, {}, force=False))
        self.assertEqual(
            cli.sent,
            ["get path.hash.mode", "set path.hash.mode 1", "get path.hash.mode"],
        )

    def test_skips_unknown(self) -> None:
        cli = FakeCli({"get path.hash.mode": "UNKNOWN"})
        self.assertFalse(apply_path_hash_policy(cli, {}, force=False))
        self.assertEqual(cli.sent, ["get path.hash.mode"])


class AgcResetIntervalPolicyTests(unittest.TestCase):
    def test_trusts_set_reply_without_verify_get(self) -> None:
        cli = FakeCli(
            {
                "get agc.reset.interval": "> 0",
                "set agc.reset.interval 4": "OK - interval rounded to 4",
            }
        )
        self.assertTrue(apply_agc_reset_interval_policy(cli, {}, force=False))
        self.assertEqual(
            cli.sent,
            ["get agc.reset.interval", "set agc.reset.interval 4"],
        )

    def test_already_at_four(self) -> None:
        cli = FakeCli({"get agc.reset.interval": "> 4"})
        self.assertFalse(apply_agc_reset_interval_policy(cli, {}, force=False))
        self.assertEqual(cli.sent, ["get agc.reset.interval"])


class OtaAutofetchPolicyTests(unittest.TestCase):
    def test_parse_ota_config(self) -> None:
        raw = "ota config: autofetch=any autoinstall=off checkpoint=0 advert=0min hops=0 keys=0"
        self.assertEqual(parse_ota_autofetch(raw), "any")

    def test_already_off(self) -> None:
        cli = FakeCli({"ota config": "ota config: autofetch=off autoinstall=off"})
        self.assertFalse(apply_ota_autofetch_policy(cli, {}, force=False))
        self.assertEqual(cli.sent, ["ota config"])

    def test_sets_any_to_off(self) -> None:
        cli = FakeCli(
            {
                "ota config": [
                    "ota config: autofetch=any autoinstall=off",
                    "ota config: autofetch=off autoinstall=off",
                ],
                "ota config autofetch off": "OK autofetch updated (saved)",
            }
        )
        self.assertTrue(apply_ota_autofetch_policy(cli, {}, force=False))
        self.assertEqual(
            cli.sent,
            ["ota config", "ota config autofetch off", "ota config"],
        )

    def test_skips_unknown(self) -> None:
        cli = FakeCli({"ota config": "UNKNOWN COMMAND"})
        self.assertFalse(apply_ota_autofetch_policy(cli, {}, force=False))
        self.assertEqual(cli.sent, ["ota config"])


class UsbDropTests(unittest.TestCase):
    def test_missing_path_is_gone(self) -> None:
        self.assertTrue(wait_usb_gone("/dev/cu.usbmodem-envybot-missing", timeout=0.2))

    def test_existing_path_stays(self) -> None:
        self.assertFalse(wait_usb_gone("/dev/null", timeout=0.35))


class AntennaPromptTests(unittest.TestCase):
    def test_enter_continues(self) -> None:
        self.assertTrue(antenna_ready(""))
        self.assertTrue(antenna_ready("y"))
        self.assertTrue(antenna_ready("yes"))

    def test_skip(self) -> None:
        self.assertFalse(antenna_ready("s"))
        self.assertFalse(antenna_ready("skip"))
        self.assertFalse(antenna_ready("n"))


class EnsureOnboardUnitTests(unittest.TestCase):
    def test_new_pubkey_reserves_next_unit(self) -> None:
        doc: dict = {"next_unit": 51, "nodes": {}}
        nodes = doc["nodes"]
        key, node, created = ensure_onboard_unit(doc, nodes, "b" * 64, unit=None)
        self.assertTrue(created)
        self.assertEqual(key, "me0051")
        self.assertEqual(node["unit_id"], "ME0051")
        self.assertEqual(doc["next_unit"], 52)

    def test_existing_pubkey_reuses_row(self) -> None:
        doc = {
            "next_unit": 52,
            "nodes": {
                "me0003": {"unit_id": "ME0003", "identity_pubkey": "a" * 64},
            },
        }
        key, node, created = ensure_onboard_unit(doc, doc["nodes"], "a" * 64, unit=None)
        self.assertFalse(created)
        self.assertEqual(key, "me0003")
        self.assertIs(node, doc["nodes"]["me0003"])


class ResolveUnitTests(unittest.TestCase):
    def test_mismatch_on_unit_says_reonboard(self) -> None:
        nodes = {
            "me0057": {
                "unit_id": "ME0057",
                "identity_pubkey": "a" * 64,
            }
        }
        with self.assertRaises(SystemExit) as err:
            resolve_unit(nodes, "b" * 64, unit="me0057")
        self.assertIn("reonboard", str(err.exception))

    def test_refuses_decommissioned_pubkey(self) -> None:
        nodes = {
            "me0008": {
                "unit_id": "ME0008",
                "identity_pubkey": "a" * 64,
                "decommissioned": 1787904120,
            }
        }
        with self.assertRaises(SystemExit) as err:
            resolve_unit(nodes, "a" * 64, unit=None)
        self.assertIn("decommissioned", str(err.exception))


class FleetIdentityTests(unittest.TestCase):
    def test_mint_when_new_row(self) -> None:
        node = {"unit_id": "ME0051"}
        self.assertTrue(should_mint_fleet_identity(node, "c" * 64, created=True))

    def test_no_mint_when_pubkey_matches(self) -> None:
        node = {"identity_pubkey": "a" * 64}
        self.assertFalse(should_mint_fleet_identity(node, "a" * 64, created=False))

    def test_mint_when_row_has_no_identity(self) -> None:
        node = {"unit_id": "ME0057"}
        self.assertTrue(should_mint_fleet_identity(node, "c" * 64, created=False))

    def test_reonboard_allows_missing_secret(self) -> None:
        nodes = {"me0057": {"unit_id": "ME0057", "identity_pubkey": "a" * 64}}
        key, node = ensure_reonboard_unit(nodes, unit="me0057")
        self.assertEqual(key, "me0057")

    def test_reonboard_accepts_complete_row(self) -> None:
        nodes = {
            "me0057": {
                "unit_id": "ME0057",
                "identity_pubkey": "a" * 64,
                "identity_secret": "d" * 128,
            }
        }
        key, node = ensure_reonboard_unit(nodes, unit="me0057")
        self.assertEqual(key, "me0057")
        self.assertIs(node, nodes["me0057"])

    def test_reonboard_requires_unit_flag(self) -> None:
        with self.assertRaises(SystemExit) as err:
            ensure_reonboard_unit({}, unit=None)
        self.assertIn("requires --unit", str(err.exception))

    def test_restore_sends_set_prv_key(self) -> None:
        node = {
            "identity_pubkey": "a" * 64,
            "identity_secret": "d" * 128,
        }
        cli = FakeCli(
            {
                "get prv.key": "> " + "e" * 128,
                "set prv.key " + "d" * 128: "OK",
            }
        )
        self.assertTrue(restore_book_identity_on_usb(cli, node))
        self.assertIn("set prv.key " + "d" * 128, cli.sent)

    def test_restore_skips_when_already_matching(self) -> None:
        node = {
            "identity_pubkey": "a" * 64,
            "identity_secret": "d" * 128,
        }
        cli = FakeCli(
            {
                "get prv.key": "> " + "d" * 128,
                "get public.key": "> " + "a" * 64,
            }
        )
        self.assertFalse(restore_book_identity_on_usb(cli, node))

    def test_mint_sends_set_prv_key(self) -> None:
        secret = "f" * 128
        cli = FakeCli(
            {
                "get prv.key": "> " + "e" * 128,
                "set prv.key " + secret: "OK",
            }
        )
        self.assertTrue(mint_identity_on_usb(cli, secret))
        self.assertIn("set prv.key " + secret, cli.sent)


class AdminPasswordPolicyTests(unittest.TestCase):
    def test_always_writes_yaml_reuse(self) -> None:
        cli = FakeCli({"password AdminOneStrong1": "password now: AdminOneStrong1"})
        apply_admin_password(cli, "AdminOneStrong1")
        self.assertEqual(cli.sent, ["password AdminOneStrong1"])

    def test_rejects_mismatch_reply(self) -> None:
        cli = FakeCli({"password AdminOneStrong1": "password now: other"})
        with self.assertRaises(CliError):
            apply_admin_password(cli, "AdminOneStrong1")


class StampFleetReadyTests(unittest.TestCase):
    def test_written_private_row_is_not_apply_due(self) -> None:
        node = {
            "unit_id": "ME0051",
            "firmware_platform": "meshcore",
            "admin_password": "AdminOneStrong1",
            "guest_password": "GuestOneStrong1",
            "identity_pubkey": "aa" * 32,
        }
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            nodes_path = book / "nodes.yaml"
            write_nodes_doc(nodes_path, {"next_unit": 52, "nodes": {"me0051": node}})
            conn = open_history(book)
            self.assertTrue(apply_is_due(conn, "me0051", node, None))
            pid = stamp_fleet_ready(nodes_path, "me0051")
            self.assertIsNotNone(pid)
            self.assertFalse(apply_is_due(conn, "me0051", node, None))


class InitialRepeatTests(unittest.TestCase):
    def _result(self) -> dict:
        return {
            "pubkey": "bb" * 32,
            "secret": "cc" * 64,
            "admin_password": "AdminOneStrong1",
            "guest_password": "",
            "admin_changed": False,
            "guest_changed": False,
            "firmware_platform": "meshcore",
            "firmware_version": "1.17.1-ev1",
            "bootloader_version": "0.9.2-ev2",
            "base_hash": None,
            "node_clock": None,
            "neighbors": None,
        }

    def test_register_pins_repeat_off_when_unset(self) -> None:
        from envybot.nodes_doc import load_nodes_doc

        with tempfile.TemporaryDirectory() as tmp:
            nodes_path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(nodes_path, {"next_unit": 54, "nodes": {"me0053": {"unit_id": "ME0053"}}})
            register(nodes_path, self._result(), unit="me0053")
            node = load_nodes_doc(nodes_path)["nodes"]["me0053"]
            self.assertIs(node["repeat"], False)

    def test_register_keeps_explicit_repeat_on(self) -> None:
        from envybot.nodes_doc import load_nodes_doc

        with tempfile.TemporaryDirectory() as tmp:
            nodes_path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(
                nodes_path,
                {"next_unit": 54, "nodes": {"me0053": {"unit_id": "ME0053", "repeat": True}}},
            )
            register(nodes_path, self._result(), unit="me0053")
            node = load_nodes_doc(nodes_path)["nodes"]["me0053"]
            self.assertIs(node["repeat"], True)

    def test_register_writes_versions_board_and_onboard_stamp(self) -> None:
        from envybot.nodes_doc import load_nodes_doc

        with tempfile.TemporaryDirectory() as tmp:
            nodes_path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(nodes_path, {"next_unit": 54, "nodes": {}})
            register(
                nodes_path,
                self._result(),
                unit=None,
                flash_target="rak4631-repeater-slim",
            )
            nodes = load_nodes_doc(nodes_path)["nodes"]
            self.assertEqual(len(nodes), 1)
            node = next(iter(nodes.values()))
            self.assertEqual(node["firmware_version"], "1.17.1-ev1")
            self.assertEqual(node["bootloader_version"], "0.9.2-ev2")
            self.assertEqual(node["board"], "rak4631")
            self.assertIsInstance(node.get("onboarded_at"), int)
            self.assertIn("Onboard ", node.get("notes") or "")

    def test_register_second_pass_adds_neighbors(self) -> None:
        from envybot.nodes_doc import load_nodes_doc

        with tempfile.TemporaryDirectory() as tmp:
            nodes_path = Path(tmp) / "nodes.yaml"
            write_nodes_doc(nodes_path, {"next_unit": 54, "nodes": {"me0053": {"unit_id": "ME0053"}}})
            register(nodes_path, self._result(), unit="me0053")
            result = self._result()
            result["neighbors"] = [
                {"pubkey": "aa" * 32, "snr": 8.0, "secs_ago": 3},
            ]
            register(nodes_path, result, unit="me0053")
            node = load_nodes_doc(nodes_path)["nodes"]["me0053"]
            self.assertEqual(len(node["onboard_neighbors"]), 1)
            self.assertIn("neighbors ", node["notes"])
