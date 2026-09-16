"""Interactive companion selection when several transports are visible."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from envybot.radio import (
    CompanionCandidate,
    companion_candidate_display,
    pick_companion,
    select_companion_by_hint,
)


def _cand(
    label: str,
    *,
    pubkey_hex: str | None = None,
    keys_person: str | None = None,
) -> CompanionCandidate:
    return CompanionCandidate(
        transport="ble",
        label=label,
        ble_address=label,
        pubkey_hex=pubkey_hex,
        keys_person=keys_person,
    )


class CompanionDisplayTests(unittest.TestCase):
    def test_shows_pubkey_and_keys_person(self) -> None:
        cand = CompanionCandidate(
            transport="ble",
            label="BLE MeshCore-benvy (aa:bb)",
            pubkey_hex="abc123" + "0" * 58,
            keys_person="ben",
        )
        text = companion_candidate_display(cand)
        self.assertIn("pubkey abc123", text)
        self.assertIn("(ben)", text)


class PickCompanionTests(unittest.TestCase):
    def test_single_returns_without_prompt(self) -> None:
        cand = _cand("BLE tag-a (aa:bb)")
        self.assertIs(pick_companion([cand]), cand)

    def test_tty_prompts_and_returns_choice(self) -> None:
        a, b = _cand("BLE tag-a (aa:bb)"), _cand("BLE tag-b (cc:dd)")
        with patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", side_effect=["2"]
        ):
            self.assertIs(pick_companion([a, b]), b)

    def test_non_tty_exits_with_list(self) -> None:
        a, b = _cand("BLE tag-a (aa:bb)"), _cand("BLE tag-b (cc:dd)")
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                pick_companion([a, b])
        self.assertEqual(ctx.exception.code, 2)

    def test_hint_pubkey_prefix(self) -> None:
        a = _cand("BLE a", pubkey_hex="3355e0fc" + "0" * 56)
        b = _cand("BLE b", pubkey_hex="aabbccdd" + "0" * 56)
        self.assertIs(pick_companion([a, b], hint="3355"), a)

    def test_hint_keys_person_slug(self) -> None:
        a = _cand("BLE a", pubkey_hex="3355" + "0" * 60, keys_person="ben")
        b = _cand("BLE b", pubkey_hex="aabb" + "0" * 60, keys_person="yuki")
        self.assertIs(pick_companion([a, b], hint="ben"), a)

    def test_hint_ambiguous_pubkey_exits(self) -> None:
        a = _cand("BLE a", pubkey_hex="3355aa" + "0" * 58)
        b = _cand("BLE b", pubkey_hex="3355bb" + "0" * 58)
        with self.assertRaises(SystemExit) as ctx:
            select_companion_by_hint([a, b], "3355")
        self.assertEqual(ctx.exception.code, 2)

    def test_hint_unknown_pubkey_exits(self) -> None:
        a = _cand("BLE a", pubkey_hex="3355" + "0" * 60)
        with self.assertRaises(SystemExit) as ctx:
            select_companion_by_hint([a], "dead")
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
