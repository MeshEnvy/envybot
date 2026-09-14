"""Interactive companion selection when several transports are visible."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from envybot.radio import CompanionCandidate, companion_candidate_display, pick_companion


def _cand(label: str) -> CompanionCandidate:
    return CompanionCandidate(transport="ble", label=label, ble_address=label)


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


if __name__ == "__main__":
    unittest.main()
