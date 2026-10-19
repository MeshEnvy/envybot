"""USB role detection for seed."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from envybot.seed_usb import detect_usb_role


class SeedUsbRoleTests(unittest.TestCase):
    @patch("envybot.seed_usb.probe_repeater_text_cli", return_value=False)
    @patch("envybot.seed_usb._probe_companion_serial", new_callable=AsyncMock, return_value=True)
    def test_detect_companion_first(self, _comp: AsyncMock, _rep: object) -> None:
        self.assertEqual(detect_usb_role("/dev/x", baud=115200), "companion")

    @patch("envybot.seed_usb.probe_repeater_text_cli", return_value=True)
    @patch("envybot.seed_usb._probe_companion_serial", new_callable=AsyncMock, return_value=False)
    def test_detect_repeater_fallback(self, _comp: AsyncMock, _rep: object) -> None:
        self.assertEqual(detect_usb_role("/dev/x", baud=115200), "repeater")


if __name__ == "__main__":
    unittest.main()
