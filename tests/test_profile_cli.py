"""Profile book → CLI command mapping."""

from __future__ import annotations

import unittest

from envybot.profile_cli import (
    cli_commands_for_apply_field,
    cli_commands_for_apply_fields,
    profile_patch_keys_to_apply_fields,
)


class ProfilePatchKeysTests(unittest.TestCase):
    def test_maps_trust_to_acl(self) -> None:
        fields = profile_patch_keys_to_apply_fields(frozenset({"trust_admin", "hop_retry"}))
        self.assertEqual(fields, frozenset({"acl", "hop_retry"}))

    def test_skips_book_only(self) -> None:
        self.assertEqual(profile_patch_keys_to_apply_fields(frozenset({"paused", "alias"})), frozenset())


class ProfileCliCommandsTests(unittest.TestCase):
    def test_powersaving_bool(self) -> None:
        node = {"powersaving": True}
        cmds = cli_commands_for_apply_field(
            "powersaving",
            unit_key="me0001",
            node=node,
            sites={},
            doc={},
            keys={},
        )
        self.assertEqual(cmds, ["powersaving on"])

    def test_hop_retry(self) -> None:
        node = {"hop_retry": 3}
        cmds = cli_commands_for_apply_field(
            "hop_retry",
            unit_key="me0001",
            node=node,
            sites={},
            doc={},
            keys={},
        )
        self.assertEqual(cmds, ["set hop.retry 3"])

    def test_bundle_adds_clock_and_advert_for_name(self) -> None:
        node = {"name": "Bench", "advert_interval_min": 60}
        cmds = cli_commands_for_apply_fields(
            frozenset({"name"}),
            unit_key="me0001",
            node=node,
            sites={},
            doc={},
            keys={},
        )
        self.assertTrue(cmds[0].startswith("set name "))
        self.assertTrue(any(c.startswith("time ") for c in cmds))
        self.assertEqual(cmds[-1], "advert")


if __name__ == "__main__":
    unittest.main()
