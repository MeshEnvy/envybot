"""Tests for envybot export sites CSV."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from pathlib import Path

from envybot.book_export import (
    SITE_EXPORT_COLUMNS,
    iter_site_export_rows,
    parse_tags_arg,
    site_matches_tags,
    write_csv,
)
from envybot.commands import export as export_cmd


class TagFilterTests(unittest.TestCase):
    def test_parse_tags_comma(self) -> None:
        self.assertEqual(parse_tags_arg(["installed,silver-triangle"]), frozenset({"installed", "silver-triangle"}))

    def test_and_semantics(self) -> None:
        site = {"tags": ["installed", "silver-triangle"]}
        self.assertTrue(site_matches_tags(site, frozenset({"installed"})))
        self.assertTrue(site_matches_tags(site, frozenset({"installed", "silver-triangle"})))
        self.assertFalse(site_matches_tags(site, frozenset({"installed", "missing"})))


class SiteExportRowsTests(unittest.TestCase):
    def test_bound_and_unbound(self) -> None:
        doc = {
            "nodes": {
                "me0001": {
                    "unit_id": "ME0001",
                    "admin_password": "sekrit",
                    "identity_pubkey": "a" * 64,
                },
            },
        }
        sites = {
            "ophir-hill": {
                "name": "Ophir",
                "node": "me0001",
                "loc": [39.31, -119.67],
                "tags": ["installed"],
                "advert_name": "Ophir",
            },
            "eip-pin": {
                "name": "Fernley HS",
                "loc": [39.58, -119.24],
                "tags": ["installed", "eip"],
            },
            "planned": {
                "name": "Future",
                "tags": ["planned"],
            },
        }
        rows = iter_site_export_rows(doc, sites, required_tags=frozenset({"installed"}))
        self.assertEqual(len(rows), 2)
        by_site = {r["site"]: r for r in rows}
        self.assertEqual(by_site["ophir-hill"]["admin_password"], "sekrit")
        self.assertEqual(by_site["ophir-hill"]["unit_id"], "ME0001")
        self.assertEqual(by_site["eip-pin"]["admin_password"], "")

    def test_skips_decommissioned_and_meshtastic(self) -> None:
        doc = {
            "nodes": {
                "me0001": {"unit_id": "ME0001", "decommissioned": 1787943600},
                "me0002": {"unit_id": "ME0002", "firmware_platform": "meshtastic"},
            },
        }
        sites = {
            "old": {"name": "Old", "node": "me0001", "tags": ["installed"]},
            "mt": {"name": "MT", "node": "me0002", "tags": ["installed"]},
            "ok": {"name": "Ok", "node": "me0003", "tags": ["installed"]},
        }
        doc["nodes"]["me0003"] = {"unit_id": "ME0003", "admin_password": "x"}
        rows = iter_site_export_rows(doc, sites, required_tags=frozenset())
        self.assertEqual([r["site"] for r in rows], ["ok"])

    def test_csv_roundtrip(self) -> None:
        doc = {"nodes": {}}
        sites = {"x": {"name": "X", "tags": ["installed"]}}
        buf = io.StringIO()
        write_csv(iter_site_export_rows(doc, sites, required_tags=frozenset()), buf)
        buf.seek(0)
        reader = csv.DictReader(buf)
        self.assertEqual(reader.fieldnames, list(SITE_EXPORT_COLUMNS))
        row = next(reader)
        self.assertEqual(row["site"], "x")
        self.assertIsNone(next(reader, None))


class ExportCommandTests(unittest.TestCase):
    def test_cli_tags_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            (book / "nodes.yaml").write_text(
                "nodes:\n  me0001:\n    unit_id: ME0001\n    admin_password: pw\n",
                encoding="utf-8",
            )
            (book / "sites.yaml").write_text(
                "sites:\n"
                "  a:\n    name: A\n    tags: [installed]\n    node: me0001\n"
                "  b:\n    name: B\n    tags: [planned]\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            err = io.StringIO()
            import sys

            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                rc = export_cmd.main(book, ["sites", "--tags=installed"])
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            self.assertEqual(rc, 0)
            self.assertIn("1 row(s)", err.getvalue())
            reader = csv.DictReader(io.StringIO(out.getvalue()))
            rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["site"], "a")
            self.assertEqual(rows[0]["admin_password"], "pw")


if __name__ == "__main__":
    unittest.main()
