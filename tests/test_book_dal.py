"""Central book load + advert name validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envybot.book import BookError
from envybot.book_dal import load_book, validate_book
from envybot.nodes_doc import sync_book, write_nodes_doc


DOC = {
    "public_advert": {
        "name_suffix": " {lora.sh}",
        "location_accuracy_mi": 1.5,
        "location_salt": "test-salt",
    }
}


class BookDalTests(unittest.TestCase):
    def _write_book(
        self,
        book: Path,
        *,
        advert_name: str = "Ophir",
        suffix: str | None = None,
    ) -> None:
        (book / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
        (book / "sites.yaml").write_text(
            f"sites:\n  test-site:\n    node: me0001\n"
            f"    advert_name: {advert_name}\n"
            f"    loc: [39.5, -119.8]\n",
            encoding="utf-8",
        )
        doc = {
            **DOC,
            "nodes": {
                "me0001": {
                    "unit_id": "ME0001",
                    "identity_pubkey": "a" * 64,
                }
            },
        }
        if suffix is not None:
            doc["public_advert"] = {**DOC["public_advert"], "name_suffix": suffix}
        write_nodes_doc(book / "nodes.yaml", doc)

    def test_load_book_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            self._write_book(book, advert_name="Ophir")
            loaded = load_book(book / "nodes.yaml")
            self.assertEqual(loaded.nodes["me0001"]["unit_id"], "ME0001")
            self.assertIn("test-site", loaded.sites)

    def test_rejects_long_name_with_gps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            self._write_book(book, advert_name="Bare Mountain E")
            with self.assertRaises(BookError) as ctx:
                load_book(book / "nodes.yaml")
            self.assertIn("max 23", str(ctx.exception))
            self.assertIn("Bare Mountain E {lora.sh}", str(ctx.exception))

    def test_suffix_change_can_break_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            self._write_book(book, advert_name="Ophir")
            nodes_path = book / "nodes.yaml"
            load_book(nodes_path)
            (book / "sites.yaml").write_text(
                "sites:\n  test-site:\n    node: me0001\n"
                "    advert_name: Ophir Peak\n    loc: [39.5, -119.8]\n",
                encoding="utf-8",
            )
            doc = {
                **DOC,
                "public_advert": {
                    **DOC["public_advert"],
                    "name_suffix": " {meshenvy.org}",
                },
                "nodes": {"me0001": {"unit_id": "ME0001", "identity_pubkey": "a" * 64}},
            }
            write_nodes_doc(nodes_path, doc)
            with self.assertRaises(BookError):
                load_book(nodes_path)

    def test_sync_book_rejects_invalid_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            self._write_book(book, advert_name="Ophir")
            nodes_path = book / "nodes.yaml"
            doc = {"nodes": {"me0001": {"unit_id": "ME0001"}}}
            nodes = doc["nodes"]
            sites: dict = {}
            keys: dict = {}
            load_book(nodes_path)
            (book / "sites.yaml").write_text(
                "sites:\n  test-site:\n    node: me0001\n"
                "    advert_name: Bare Mountain E\n    loc: [39.5, -119.8]\n",
                encoding="utf-8",
            )
            nodes_path.touch()
            with self.assertRaises(BookError):
                sync_book(nodes_path, doc, nodes, sites, keys)

    def test_unbound_bench_skips_advert_name_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp)
            (book / "keys.yaml").write_text("people: {}\n", encoding="utf-8")
            (book / "sites.yaml").write_text("sites: {}\n", encoding="utf-8")
            write_nodes_doc(
                book / "nodes.yaml",
                {"nodes": {"me0099": {"unit_id": "ME0099", "identity_pubkey": "b" * 64}}},
            )
            errors = validate_book(
                {"nodes": {"me0099": {"unit_id": "ME0099"}}},
                {"me0099": {"unit_id": "ME0099"}},
                {},
            )
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
