"""Duty-cycle policy: MeshCore 1.15+ CLI vs pre-1.15 airtime factor."""

from __future__ import annotations

import unittest

from envybot.radio import (
    DUTYCYCLE_CLI_SINCE,
    FLEET_DUTYCYCLE_PCT,
    airtime_factor_for_dutycycle,
    firmware_has_dutycycle_cli,
    parse_firmware_core,
)


class FirmwareCoreTests(unittest.TestCase):
    def test_meshcore_hash_suffix(self) -> None:
        self.assertEqual(parse_firmware_core("v1.14.1-467959c"), (1, 14, 1))

    def test_envyos(self) -> None:
        self.assertEqual(parse_firmware_core("v0.1.3"), (0, 1, 3))

    def test_dotted_build(self) -> None:
        self.assertEqual(parse_firmware_core("1.16.0.1"), (1, 16, 0, 1))

    def test_unparseable(self) -> None:
        self.assertIsNone(parse_firmware_core("ShortTurbo"))
        self.assertIsNone(parse_firmware_core(None))


class DutycycleCliGateTests(unittest.TestCase):
    def test_pre_1_15_meshcore(self) -> None:
        self.assertFalse(firmware_has_dutycycle_cli("v1.14.1-467959c"))
        self.assertFalse(firmware_has_dutycycle_cli("v1.14.0"))

    def test_1_15_and_later(self) -> None:
        self.assertTrue(firmware_has_dutycycle_cli("v1.15.0-dee3e26"))
        self.assertTrue(firmware_has_dutycycle_cli("v1.16.0-07a3ca9"))
        self.assertTrue(firmware_has_dutycycle_cli("1.16.0.1"))

    def test_envyos_has_cli(self) -> None:
        self.assertTrue(firmware_has_dutycycle_cli("v0.1.0"))
        self.assertTrue(firmware_has_dutycycle_cli("v0.1.3"))

    def test_unknown(self) -> None:
        self.assertIsNone(firmware_has_dutycycle_cli("ShortTurbo"))
        self.assertIsNone(firmware_has_dutycycle_cli(None))

    def test_gate_tuple(self) -> None:
        self.assertEqual(DUTYCYCLE_CLI_SINCE, (1, 15))


class AirtimeFactorTests(unittest.TestCase):
    def test_full_duty_is_zero_af(self) -> None:
        self.assertEqual(airtime_factor_for_dutycycle(FLEET_DUTYCYCLE_PCT), 0.0)

    def test_default_fifty(self) -> None:
        self.assertEqual(airtime_factor_for_dutycycle(50.0), 1.0)


if __name__ == "__main__":
    unittest.main()
