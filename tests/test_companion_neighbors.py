"""Companion zero-hop NODE_DISCOVER ping on connect."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from meshcore import EventType

from envybot.radio import (
    CONTACT_TYPE_REPEATER,
    companion_neighbor_ping,
    discover_response_row,
    format_companion_neighbor_report,
    merge_discover_row,
    normalize_discover_tag,
)


class NormalizeDiscoverTagTests(unittest.TestCase):
    def test_int_tag_little_endian_hex(self) -> None:
        self.assertEqual(normalize_discover_tag(0x3039), "39300000")

    def test_hex_string_passthrough(self) -> None:
        self.assertEqual(normalize_discover_tag("39300000"), "39300000")


class DiscoverResponseRowTests(unittest.TestCase):
    def test_accepts_matching_repeater(self) -> None:
        row = discover_response_row(
            {
                "tag": "39300000",
                "node_type": CONTACT_TYPE_REPEATER,
                "pubkey": "aabbccdd" + "00" * 12,
                "SNR": -8.5,
            },
            expected_tag_hex="39300000",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["pubkey"], "aabbccdd" + "00" * 12)
        self.assertAlmostEqual(row["snr"], -8.5)

    def test_rejects_wrong_tag(self) -> None:
        self.assertIsNone(
            discover_response_row(
                {"tag": "deadbeef", "node_type": CONTACT_TYPE_REPEATER, "pubkey": "aa"},
                expected_tag_hex="39300000",
            )
        )

    def test_rejects_non_repeater(self) -> None:
        self.assertIsNone(
            discover_response_row(
                {"tag": "39300000", "node_type": 1, "pubkey": "aa"},
                expected_tag_hex="39300000",
            )
        )


class FormatCompanionNeighborReportTests(unittest.TestCase):
    def test_empty_warning(self) -> None:
        lines = format_companion_neighbor_report([])
        self.assertEqual(len(lines), 1)
        self.assertIn("none", lines[0].lower())

    def test_lists_rows(self) -> None:
        lines = format_companion_neighbor_report(
            [
                {"name": "Ophir", "pubkey": "aabbccdd", "snr": -8.5},
                {"name": "Slide", "pubkey": "11223344", "snr": -14.0},
            ]
        )
        self.assertIn("nearby (2)", lines[0])
        self.assertIn("Ophir", lines[1])
        self.assertIn("-8.5 dB", lines[1])


class MergeDiscoverRowTests(unittest.TestCase):
    def test_keeps_stronger_snr(self) -> None:
        client = MagicMock()
        client.get_contact_by_key_prefix.return_value = {"adv_name": "Ophir"}
        rows: dict[str, dict] = {}
        merge_discover_row(
            rows,
            {"pubkey": "aabbccdd" + "00" * 12, "snr": -14.0},
            client=client,
        )
        merge_discover_row(
            rows,
            {"pubkey": "aabbccdd" + "ff" * 12, "snr": -8.0},
            client=client,
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows["aabbccdd"]["snr"], -8.0)


class CompanionNeighborPingTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_and_prints_sorted(self) -> None:
        client = MagicMock()
        tag_int = 0x3039
        tag_hex = normalize_discover_tag(tag_int)
        ok = SimpleNamespace(
            type=EventType.OK,
            payload={"tag": tag_int},
            is_error=lambda: False,
        )
        client.commands.send_node_discover_req = AsyncMock(return_value=ok)

        captured: list = []

        def subscribe(_event_type: EventType, callback) -> SimpleNamespace:
            captured.append(callback)
            return SimpleNamespace(unsubscribe=lambda: None)

        client.subscribe = subscribe
        client.get_contact_by_key_prefix.side_effect = lambda prefix: (
            {"adv_name": "Alpha"} if prefix.startswith("aa") else {"adv_name": "Beta"}
        )

        async def run_ping() -> None:
            task = __import__("asyncio").create_task(
                companion_neighbor_ping(client, wait_s=0.05)
            )
            await __import__("asyncio").sleep(0)
            for cb in captured:
                cb(
                    SimpleNamespace(
                        payload={
                            "tag": tag_hex,
                            "node_type": CONTACT_TYPE_REPEATER,
                            "pubkey": "11223344" + "00" * 12,
                            "SNR": -14.0,
                        }
                    )
                )
                cb(
                    SimpleNamespace(
                        payload={
                            "tag": tag_hex,
                            "node_type": CONTACT_TYPE_REPEATER,
                            "pubkey": "aabbccdd" + "00" * 12,
                            "SNR": -8.0,
                        }
                    )
                )
            await task

        buf = io.StringIO()
        with redirect_stdout(buf):
            await run_ping()
        out = buf.getvalue()
        self.assertIn("nearby (2)", out)
        self.assertLess(out.index("Alpha"), out.index("Beta"))

    async def test_skips_unsupported_command(self) -> None:
        client = MagicMock()
        err = SimpleNamespace(
            type=EventType.ERROR,
            payload={"reason": "ERR_CODE_UNSUPPORTED_CMD"},
            is_error=lambda: True,
        )
        client.commands.send_node_discover_req = AsyncMock(return_value=err)
        client.subscribe = MagicMock(return_value=SimpleNamespace(unsubscribe=lambda: None))

        buf = io.StringIO()
        with redirect_stdout(buf):
            await companion_neighbor_ping(client, wait_s=0)
        self.assertIn("skipped", buf.getvalue())
        self.assertIn("UNSUPPORTED", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
