"""Command-line interface: `anki2sqlite input.apkg -o analytics.db`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from . import __version__, convert
from .sources import MissingDependencyError

KNOWN_SUFFIXES = {".anki2", ".anki21", ".anki21b", ".apkg", ".colpkg"}


def default_output(src: Path) -> Path:
    stem = src.stem if src.suffix.lower() in KNOWN_SUFFIXES else src.name
    return src.with_name(f"{stem}.analytics.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anki2sqlite",
        description=(
            "Convert an Anki collection (.anki2/.anki21) or export "
            "(.apkg/.colpkg) into a clean SQLite database for analytics."
        ),
    )
    parser.add_argument("input", help="collection file or Anki export archive")
    parser.add_argument(
        "-o", "--output",
        help="output database path (default: <input>.analytics.db)",
    )
    parser.add_argument(
        "--timezone", default="UTC", metavar="IANA_NAME",
        help="timezone for all timestamps, e.g. Europe/Moscow (default: UTC)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite the output file if it exists",
    )
    parser.add_argument(
        "--no-views", action="store_true",
        help="do not create the v_* convenience views",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress summary output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = Path(args.input)
    dst = Path(args.output) if args.output else default_output(src)

    try:
        result = convert(
            src,
            dst,
            timezone=args.timezone,
            views=not args.no_views,
            overwrite=args.force,
        )
    except FileExistsError:
        print(f"error: {dst} already exists — pass --force to overwrite", file=sys.stderr)
        return 1
    except ZoneInfoNotFoundError:
        print(f"error: unknown timezone {args.timezone!r}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, MissingDependencyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        source_kind = "legacy (schema 11)" if result.schema_version == 11 else (
            f"modern (schema {result.schema_version})"
        )
        print(f"{src.name}  [{source_kind}]  ->  {result.output_path}")
        for name in ("decks", "note_types", "notes", "cards", "reviews"):
            print(f"  {name:<11} {result.counts[name]:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
