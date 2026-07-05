"""anki2sqlite: convert Anki collections into an analytics-friendly SQLite database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.0"


@dataclass(frozen=True)
class ConvertResult:
    output_path: Path
    counts: dict[str, int]  # rows written per logical entity
    schema_version: int  # source collection's schema version (11 = legacy)


def convert(
    src: str | Path,
    dst: str | Path,
    *,
    timezone: str = "UTC",
    views: bool = True,
    overwrite: bool = False,
) -> ConvertResult:
    """Convert an Anki collection or export into an analytics SQLite database.

    src: .anki2/.anki21 collection file, or .apkg/.colpkg archive.
    dst: path for the database to create.
    timezone: IANA name; timestamps are written as naive local time in it.
    views: also create the v_* convenience views.
    overwrite: replace dst if it already exists.
    """
    from . import build, extract, sources

    with sources.open_collection(src) as conn:
        schema_version = extract.read_meta(conn).schema_version
        counts = build.build_database(
            conn, dst, timezone=timezone, views=views, overwrite=overwrite
        )
    return ConvertResult(Path(dst), counts, schema_version)
