import json
from pathlib import Path

from envybot.links import load_manual_links, read_link_status


def test_load_manual_links_from_pair_files(tmp_path: Path) -> None:
    book = tmp_path
    manual_dir = book / "links" / "alpha"
    manual_dir.mkdir(parents=True)
    (manual_dir / "beta.json").write_text(
        json.dumps(
            {
                "a": "alpha",
                "b": "beta",
                "manual": True,
                "kind": "none",
            }
        ),
        encoding="utf-8",
    )
    rf_dir = book / "links" / "hub"
    rf_dir.mkdir(parents=True)
    (rf_dir / "peer.json").write_text(
        json.dumps({"a": "hub", "b": "peer", "manual": False, "kind": "two_way"}),
        encoding="utf-8",
    )
    pairs = load_manual_links(book)
    assert pairs == [("alpha", "beta")]


def test_read_link_status_canonical_path(tmp_path: Path) -> None:
    book = tmp_path
    path = book / "links" / "alpha" / "beta.json"
    path.parent.mkdir(parents=True)
    payload = {"a": "alpha", "b": "beta", "kind": "two_way"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_link_status(book, "beta", "alpha") == payload
