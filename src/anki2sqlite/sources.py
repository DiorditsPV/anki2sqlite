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


def _import_zstandard():
    try:
        import zstandard
    except ImportError as exc:
        raise MissingDependencyError(
            "this file uses Anki's newer zstd-compressed format; "
            "install the optional dependency with: pip install anki2sqlite[zstd]"
        ) from exc
    return zstandard


def _extract_member(zf: zipfile.ZipFile, member: str, out: Path) -> None:
    """Stream a zip member to disk, decompressing zstd for .anki21b.

    Anki's exporter writes zstd frames without the content size in the frame
    header, so one-shot decompression APIs would fail; streaming handles both.
    """
    with zf.open(member) as src_fh, open(out, "wb") as dst_fh:
        if member.endswith(".anki21b"):
            zstandard = _import_zstandard()
            zstandard.ZstdDecompressor().copy_stream(src_fh, dst_fh)
        else:
            shutil.copyfileobj(src_fh, dst_fh)


def _materialize(src: Path, workdir: Path) -> Path:
    """Produce a plain SQLite file inside workdir from any supported input."""
    if zipfile.is_zipfile(src):
        out = workdir / "collection.sqlite"
        try:
            with zipfile.ZipFile(src) as zf:
                names = set(zf.namelist())
                member = next((m for m in ZIP_MEMBERS if m in names), None)
                if member is None:
                    raise ValueError(
                        f"{src.name}: no collection member found in archive "
                        f"(expected one of {', '.join(ZIP_MEMBERS)})"
                    )
                _extract_member(zf, member, out)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{src.name}: corrupt archive ({exc})") from exc
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
