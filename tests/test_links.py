import json
from pathlib import Path

from envybot.links import load_manual_links, read_link_status


def test_load_manual_links_tuple_and_record(tmp_path: Path) -> None:
    book = tmp_path / "book"
    book.mkdir()
    (book / "links.json").write_text(
        json.dumps(
            {
                "links": [
                    ["beta", "alpha"],
                    {"a": "x", "b": "y", "field_at": "2026-09-09"},
                ],
            }
        ),
        encoding="utf-8",
    )
    pairs = load_manual_links(book)
    assert pairs == [("alpha", "beta"), ("x", "y")]


def test_read_link_status_canonical_path(tmp_path: Path) -> None:
    book = tmp_path / "book"
    cache_dir = book / "links" / "alpha"
    cache_dir.mkdir(parents=True)
    payload = {"a": "alpha", "b": "beta", "kind": "two_way"}
    (cache_dir / "beta.json").write_text(json.dumps(payload), encoding="utf-8")
    assert read_link_status(book, "beta", "alpha") == payload
