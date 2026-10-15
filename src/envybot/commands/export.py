"""Export book data for trusted administrators (CSV to stdout)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from envybot.book import nodes_path
from envybot.book_export import iter_site_export_rows, parse_tags_arg, write_csv
from envybot.nodes_doc import load_nodes_doc, load_sites_for_book


def main(book: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sites_p = sub.add_parser("sites", help="CSV of sites.yaml rows plus bound node credentials")
    sites_p.add_argument(
        "--tags",
        action="append",
        default=None,
        metavar="TAG",
        help="Only sites whose tags include every listed tag (comma-separated, repeatable)",
    )

    args = parser.parse_args(argv)
    if args.command != "sites":
        parser.print_help()
        return 2

    np = nodes_path(book)
    if not np.is_file():
        print(f"export sites: no nodes.yaml at {np}", file=sys.stderr)
        return 1

    doc = load_nodes_doc(np)
    sites = load_sites_for_book(np)
    required = parse_tags_arg(args.tags)
    rows = iter_site_export_rows(doc, sites, required_tags=required)
    write_csv(rows, sys.stdout)
    print(f"export sites: {len(rows)} row(s)", file=sys.stderr)
    return 0
