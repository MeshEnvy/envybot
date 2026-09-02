"""channels.yaml load, grant resolve, slot planner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

from envybot.channels_doc import (
    PUBLIC_FIRMWARE_NAME,
    PUBLIC_GROUP_PSK,
    ChannelDef,
    ChannelSlot,
    ChannelsError,
    load_channels,
    plan_channel_ops,
    resolve_person_channels,
    secrets_match,
    slot_is_empty,
)

SLPT_KEY = bytes.fromhex("e30372f63beb7bbc4bc7bbf06589e6ab")
MESH_KEY = bytes.fromhex("e63afec724cc4e21f0e167ad67fd4019")


def _catalog_yaml() -> str:
    return """
public:
  people: everyone
SLPT:
  key: e30372f63beb7bbc4bc7bbf06589e6ab
  people: [ben]
MeshEnvy:
  key: e63afec724cc4e21f0e167ad67fd4019
  people: [ben]
911:
  key: bf1b29b3cde13edbcf4c2e9c15137bd9
  people: [ben, bill]
"""


class LoadChannelsTests(unittest.TestCase):
    def test_load_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.yaml"
            path.write_text(_catalog_yaml(), encoding="utf-8")
            catalog = load_channels(path)
            self.assertIn("public", catalog)
            self.assertIn("SLPT", catalog)
            _people, slpt = catalog["SLPT"]
            self.assertEqual(slpt.secret, SLPT_KEY)

    def test_missing_key_on_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.yaml"
            path.write_text("foo:\n  people: [ben]\n", encoding="utf-8")
            with self.assertRaises(ChannelsError):
                load_channels(path)


class ResolvePersonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        path = Path(cls._tmp.name) / "channels.yaml"
        path.write_text(_catalog_yaml(), encoding="utf-8")
        cls.catalog = load_channels(path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_everyone_skips_public(self) -> None:
        names = {c.yaml_name for c in resolve_person_channels(self.catalog, None)}
        self.assertEqual(names, set())

    def test_ben_gets_private_set(self) -> None:
        names = {c.yaml_name for c in resolve_person_channels(self.catalog, "ben")}
        self.assertEqual(names, {"SLPT", "MeshEnvy", "911"})

    def test_bill_gets_911_not_slpt(self) -> None:
        names = {c.yaml_name for c in resolve_person_channels(self.catalog, "bill")}
        self.assertEqual(names, {"911"})


class PlanChannelOpsTests(unittest.TestCase):
    def test_skip_when_name_and_key_match(self) -> None:
        want = [
            ChannelDef("SLPT", "SLPT", SLPT_KEY, False),
        ]
        heard = [ChannelSlot(1, "SLPT", SLPT_KEY)]
        ops, failures = plan_channel_ops(want, heard)
        self.assertEqual(ops, [])
        self.assertEqual(failures, [])

    def test_update_wrong_key_on_existing_name(self) -> None:
        want = [ChannelDef("SLPT", "SLPT", SLPT_KEY, False)]
        heard = [ChannelSlot(2, "SLPT", b"\x01" * 16)]
        ops, failures = plan_channel_ops(want, heard)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].idx, 2)
        self.assertEqual(failures, [])

    def test_add_to_first_empty_not_slot_zero(self) -> None:
        want = [ChannelDef("SLPT", "SLPT", SLPT_KEY, False)]
        heard = [
            ChannelSlot(0, PUBLIC_FIRMWARE_NAME, PUBLIC_GROUP_PSK),
            ChannelSlot(1, "", b"\x00" * 16),
            ChannelSlot(2, "Other", b"\x02" * 16),
        ]
        ops, failures = plan_channel_ops(want, heard)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].idx, 1)
        self.assertEqual(failures, [])

    def test_public_never_added_or_updated(self) -> None:
        want = [
            ChannelDef("public", PUBLIC_FIRMWARE_NAME, PUBLIC_GROUP_PSK, True),
        ]
        heard = [
            ChannelSlot(0, "", b"\x00" * 16),
            ChannelSlot(1, "", b"\x00" * 16),
        ]
        ops, failures = plan_channel_ops(want, heard)
        self.assertEqual(ops, [])
        self.assertEqual(failures, [])

        heard_stock = [ChannelSlot(0, PUBLIC_FIRMWARE_NAME, PUBLIC_GROUP_PSK)]
        ops, failures = plan_channel_ops(want, heard_stock)
        self.assertEqual(ops, [])
        self.assertEqual(failures, [])

    def test_table_full_is_failure_not_wipe(self) -> None:
        want = [ChannelDef("SLPT", "SLPT", SLPT_KEY, False)]
        heard = [
            ChannelSlot(0, PUBLIC_FIRMWARE_NAME, PUBLIC_GROUP_PSK),
            ChannelSlot(1, "A", b"\x01" * 16),
            ChannelSlot(2, "B", b"\x02" * 16),
        ]
        ops, failures = plan_channel_ops(want, heard)
        self.assertEqual(ops, [])
        self.assertEqual(failures, ["SLPT"])

    def test_extras_left_alone(self) -> None:
        want = [ChannelDef("SLPT", "SLPT", SLPT_KEY, False)]
        heard = [
            ChannelSlot(0, PUBLIC_FIRMWARE_NAME, PUBLIC_GROUP_PSK),
            ChannelSlot(1, "Personal", b"\x09" * 16),
            ChannelSlot(2, "", b"\x00" * 16),
        ]
        ops, _ = plan_channel_ops(want, heard)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].idx, 2)


class SlotHelperTests(unittest.TestCase):
    def test_empty_slot(self) -> None:
        self.assertTrue(slot_is_empty(ChannelSlot(3, "", b"\x00" * 16)))
        self.assertFalse(slot_is_empty(ChannelSlot(3, "x", b"\x00" * 16)))

    def test_secrets_match_truncates(self) -> None:
        self.assertTrue(secrets_match(SLPT_KEY, SLPT_KEY + b"\x00" * 8))


if __name__ == "__main__":
    unittest.main()
