"""Open any supported Anki export as a readable SQLite connection.

Supported inputs:

- raw collection files: `collection.anki2` / `.anki21` (SQLite)
- `.apkg` deck exports and `.colpkg` collection backups (zip archives)

Inside archives, members are tried newest-first: `collection.anki21b`
(zstd-compressed, Anki 2.1.50+) > `collection.anki21` > `collection.anki2`.

The original file is never opened directly: it is copied (or extracted) into
a temporary directory first, so a live, Anki-locked collection stays safe.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import extract

SQLITE_HEADER = b"SQLite format 3\x00"

# Newest format first.
ZIP_MEMBERS = ("collection.anki21b", "collection.anki21", "collection.anki2")


class MissingDependencyError(RuntimeError):
    """An optional dependency is required for this input format."""


def _decompress_zstd(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError as exc:
        raise MissingDependencyError(
            "this file uses Anki's newer zstd-compressed format; "
            "install the optional dependency with: pip install anki2sqlite[zstd]"
        ) from exc
    return zstandard.ZstdDecompressor().decompress(data)


def _materialize(src: Path, workdir: Path) -> Path:
    """Produce a plain SQLite file inside workdir from any supported input."""
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            names = set(zf.namelist())
            member = next((m for m in ZIP_MEMBERS if m in names), None)
            if member is None:
                raise ValueError(
                    f"{src.name}: no collection member found in archive "
                    f"(expected one of {', '.join(ZIP_MEMBERS)})"
                )
            data = zf.read(member)
        if member.endswith(".anki21b"):
            data = _decompress_zstd(data)
        out = workdir / "collection.sqlite"
        out.write_bytes(data)
        return out

    with open(src, "rb") as fh:
        header = fh.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise ValueError(
            f"{src.name}: not a SQLite collection file and not a zip archive"
        )
    out = workdir / "collection.sqlite"
    shutil.copyfile(src, out)
    return out


@contextmanager
def open_collection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a ready-to-read connection to a temp copy of the collection."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"input file not found: {src}")
    with tempfile.TemporaryDirectory(prefix="anki2sqlite-") as tmp:
        db_path = _materialize(src, Path(tmp))
        conn = sqlite3.connect(db_path)
        extract.prepare_connection(conn)
        try:
            yield conn
        finally:
            conn.close()
