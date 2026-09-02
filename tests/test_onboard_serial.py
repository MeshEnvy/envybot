"""Repeater USB CLI framing. No radio."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.apply import apply_is_due
from envybot.commands.onboard import (
    RepeaterSerial,
    antenna_ready,
    apply_path_hash_policy,
    resolve_unit,
    stamp_fleet_ready,
    wait_usb_gone,
)
from envybot.history import open_history
from envybot.keys_doc import parse_serial_acl
from envybot.nodes_doc import write_nodes_doc

BEN = "aa" * 32


class ExtractReplyTests(unittest.TestCase):
    def test_arrow_reply(self) -> None:
        buf = "ver\n  -> 1.17.1.1 (Build: 30Aug2026)\n"
        self.assertEqual(
            RepeaterSerial._extract_reply(buf),
            "1.17.1.1 (Build: 30Aug2026)",
        )
        self.assertIsNotNone(RepeaterSerial._reply_start(buf))

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
        self.assertFalse(apply_path_hash_policy(cli, force=False))
        self.assertEqual(cli.sent, ["get path.hash.mode"])

    def test_sets_one_byte_to_two(self) -> None:
        cli = FakeCli(
            {
                "get path.hash.mode": ["> 0", "> 1"],
                "set path.hash.mode 1": "OK",
            }
        )
        self.assertTrue(apply_path_hash_policy(cli, force=False))
        self.assertEqual(
            cli.sent,
            ["get path.hash.mode", "set path.hash.mode 1", "get path.hash.mode"],
        )

    def test_skips_unknown(self) -> None:
        cli = FakeCli({"get path.hash.mode": "UNKNOWN"})
        self.assertFalse(apply_path_hash_policy(cli, force=False))
        self.assertEqual(cli.sent, ["get path.hash.mode"])


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


class ResolveUnitTests(unittest.TestCase):
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
