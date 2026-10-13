"""Range shootout parsing and table. No radio."""

from __future__ import annotations

import unittest

import time

from envybot.commands.range import (
    DEFAULT_GAP_S,
    BoardConfig,
    Hit,
    collapse_mac_pairs,
    collapse_round,
    configs_differ,
    labels_from_doc,
    parse_discover_lines,
    ping_sides,
    radios_differ,
    RoundSnap,
    parse_stats_json,
    render_board,
    render_rounds,
    render_table,
    score_rows,
    wait_until,
)


CAPTURE = """
  -> OK
discover AABBCCDD rx=-4.0 in=-6.5
discover 11223344 rx=1.5 in=-2
noise neighbors line aabbccdd:3:-16
discover aabbccdd rx=-4.4 in=-6.1 rssi=-98
"""


class ParseTests(unittest.TestCase):
    def test_lines(self) -> None:
        hits = parse_discover_lines(CAPTURE)
        self.assertEqual(
            hits,
            [
                Hit("aabbccdd", -4.0, -6.5),
                Hit("11223344", 1.5, -2.0),
                Hit("aabbccdd", -4.4, -6.1, rssi=-98.0),
            ],
        )

    def test_collapse_averages_one_round(self) -> None:
        got = collapse_round(parse_discover_lines(CAPTURE))
        self.assertEqual(got["11223344"], Hit("11223344", 1.5, -2.0))
        self.assertAlmostEqual(got["aabbccdd"].rx, -4.2)
        self.assertAlmostEqual(got["aabbccdd"].inbound, -6.3)
        self.assertEqual(got["11223344"].rssi, None)
        self.assertEqual(got["aabbccdd"].rssi, -98.0)

    def test_stats_json(self) -> None:
        raw = '  -> {"noise_floor":-110,"last_rssi":-97,"last_snr":-4.25,"tx_air_secs":1,"rx_air_secs":2}'
        got = parse_stats_json(raw)
        self.assertEqual(got["noise_floor"], -110)
        self.assertEqual(got["last_rssi"], -97)

    def test_gap_matches_remote_budget(self) -> None:
        self.assertEqual(DEFAULT_GAP_S * 4, 120)

    def test_sides_alternate(self) -> None:
        self.assertEqual(ping_sides(3), ["A", "B", "A", "B", "A", "B"])

    def test_wait_until_honors_stop(self) -> None:
        state = {"stop": False}

        def stop() -> bool:
            return state["stop"]

        started = time.monotonic()
        state["stop"] = True
        self.assertTrue(wait_until(started + 5, stop))
        self.assertLess(time.monotonic() - started, 1.0)


class TableTests(unittest.TestCase):
    def test_means_misses_and_alignment(self) -> None:
        rounds_a = [
            {"aabbccdd": Hit("aabbccdd", -4.0, -6.0)},
            {"aabbccdd": Hit("aabbccdd", -4.4, -6.0)},
            {"aabbccdd": Hit("aabbccdd", -4.2, -6.0)},
        ]
        rounds_b = [
            {"aabbccdd": Hit("aabbccdd", -12.0, -5.5)},
            {},
            {"aabbccdd": Hit("aabbccdd", -12.0, -5.5)},
        ]
        rows = score_rows(rounds_a, rounds_b, {"aabbccdd": "Poito"})
        text = render_table(rows, "Xiao", "RAK")
        self.assertIn("Poito (aabb)", text)
        # A heard them at -4 dB, B at -12. Inbound rounds to a tie. A wins the row.
        self.assertIn("*-4/-6", text)
        self.assertIn("-12/-6", text)
        self.assertIn("*Xiao", text)
        self.assertNotIn("*RAK", text)
        both = score_rows(
            [{"11223344": Hit("11223344", 5.2, -4.4)}],
            [{"11223344": Hit("11223344", 8.1, 2.2)}],
            {},
        )
        starred = render_table(both, "Xiao", "RAK")
        self.assertIn("5/-4", starred)
        self.assertIn("*8/2", starred)
        self.assertIn("*RAK", starred)
        table_lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len({len(line) for line in table_lines[-2:]}), 1)
        snaps_a = [RoundSnap(rnd, -110, 4100) for rnd in rounds_a]
        snaps_b = [RoundSnap(rnd, -102, 3700) for rnd in rounds_b]
        detail = render_rounds(snaps_a, snaps_b, "Xiao", "RAK", {"aabbccdd": "Poito"})
        self.assertIn("1  Xiao  noise -110 dBm  battery 4100 mV", detail)
        self.assertIn("1  RAK  noise -102 dBm  battery 3700 mV", detail)
        self.assertIn("Poito (aabb)  rx=-4.0  in=-6.0", detail)
        self.assertIn("2  RAK  noise -102 dBm  battery 3700 mV", detail)
        self.assertIn("(no replies)", detail)

    def test_only_one_side(self) -> None:
        rows = score_rows(
            [{"abcdabcd": Hit("abcdabcd", -3.0, -1.0)}],
            [{}],
            {},
        )
        text = render_table(rows, "Xiao", "RAK")
        self.assertIn("*-3/-1", text)
        self.assertIn("*Xiao", text)

    def test_radio_mismatch(self) -> None:
        self.assertFalse(radios_differ("> 910.525,62.5,7,5", "910.525,62.5,7,5"))
        self.assertTrue(radios_differ("> 910.525,62.5,7,5", "> 910.525,62.5,8,5"))

    def test_board_settings_block(self) -> None:
        board = BoardConfig(
            name="ME0053",
            port="/dev/cu.usbmodem1101",
            radio="910.525,62.5,7,5",
            tx="22",
            rxgain="on",
            fem_rx="n/a",
            fem_tx="n/a",
        )
        other = BoardConfig(
            name="ME0054",
            port="/dev/cu.usbmodem101",
            radio="910.525,62.5,7,5",
            tx="20",
            rxgain="on",
            fem_rx="n/a",
            fem_tx="n/a",
        )
        text = render_board(board)
        self.assertIn("ME0053  /dev/cu.usbmodem1101", text)
        self.assertIn("tx      22 dBm", text)
        self.assertIn("rxgain  on", text)
        self.assertIn("fem rx  n/a", text)
        self.assertTrue(configs_differ(board, other))
        same = BoardConfig(**{**board.__dict__, "name": "ME0054", "port": other.port})
        self.assertFalse(configs_differ(board, same))

    def test_labels_and_mac_pairs(self) -> None:
        labels = labels_from_doc(
            {
                "nodes": {
                    "me0001": {
                        "identity_pubkey": "aabbccdd" + "11" * 28,
                        "alias": "Poito",
                    }
                }
            }
        )
        self.assertEqual(labels["aabbccdd"], "Poito")
        ports = collapse_mac_pairs(
            ["/dev/tty.usbmodem1", "/dev/cu.usbmodem1", "/dev/cu.usbmodem2"]
        )
        self.assertEqual(ports, ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"])


if __name__ == "__main__":
    unittest.main()
