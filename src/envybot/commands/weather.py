"""Open-Meteo weather cache for fleet history."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from envybot.history import open_history
from envybot.weather import run_weather_backfill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser(
        "backfill",
        help="One-time Open-Meteo cache fill for bound sites and history span",
    )
    backfill.add_argument(
        "--force",
        action="store_true",
        help="Run even if weather_backfill_at meta stamp exists",
    )
    backfill.add_argument(
        "--book",
        type=Path,
        default=Path("."),
        help="Fleet book directory (default: cwd)",
    )

    args = parser.parse_args(argv)
    if args.command != "backfill":
        parser.print_help()
        return 2

    book = args.book.resolve()
    conn = open_history(book)
    try:
        result = run_weather_backfill(conn, book, force=args.force)
    finally:
        conn.close()

    if result.get("skipped"):
        reason = result.get("reason", "unknown")
        if reason == "already done":
            print("weather backfill: already done (use --force to rerun)")
            return 0
        print(f"weather backfill: skipped ({reason})", file=sys.stderr)
        return 1

    print(
        f"weather backfill: {result.get('sites', 0)} site(s), "
        f"{result.get('hours_inserted', 0)} hour row(s) cached"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
