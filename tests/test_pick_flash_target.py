"""Interactive flash target selection when --target is omitted."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from envybot.flash_program import pick_flash_target, resolve_target


class PickFlashTargetTests(unittest.TestCase):
    def test_explicit_slug(self) -> None:
        spec = pick_flash_target("xiao-nrf52-repeater-slim")
        self.assertEqual(spec.slug, "xiao-nrf52-repeater-slim")

    def test_tty_prompts_and_returns_choice(self) -> None:
        rak = resolve_target("rak4631-repeater-slim")
        with patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", side_effect=["1"]
        ):
            spec = pick_flash_target(None)
        self.assertEqual(spec, rak)

    def test_non_tty_exits_with_list(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                pick_flash_target(None)
        msg = str(ctx.exception)
        self.assertIn("Pass --target", msg)
        self.assertIn("rak4631-repeater-slim", msg)


if __name__ == "__main__":
    unittest.main()
