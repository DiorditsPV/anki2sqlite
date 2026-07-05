"""Tests for input handling (.anki2/.apkg/.colpkg/zstd) and the public convert() API."""

import hashlib
import zipfile

import pytest

import conftest as fx
from anki2sqlite import ConvertResult, convert, sources


class TestConvertApi:
    def test_raw_collection_file(self, legacy_db, tmp_path):
        out = tmp_path / "analytics.db"
        result = convert(legacy_db, out)
        assert isinstance(result, ConvertResult)
        assert result.output_path == out
        assert result.schema_version == 11
        assert result.counts["notes"] == 3
        assert out.exists()

    def test_apkg(self, legacy_db, tmp_path):
        apkg = fx.make_apkg(tmp_path / "deck.apkg", legacy_db)
        result = convert(apkg, tmp_path / "out.db")
        assert result.counts == {
            "decks": 5, "note_types": 2, "notes": 3, "cards": 5, "reviews": 3,
        }

    def test_source_file_recorded_in_meta(self, legacy_db, tmp_path):
        import sqlite3

        out = tmp_path / "out.db"
        convert(legacy_db, out)
        meta = dict(sqlite3.connect(out).execute("SELECT key, value FROM meta"))
        assert meta["source_file"] == "legacy.anki2"

    def test_source_file_not_modified(self, modern_db, tmp_path):
        digest_before = hashlib.sha256(modern_db.read_bytes()).hexdigest()
        convert(modern_db, tmp_path / "out.db")
        assert hashlib.sha256(modern_db.read_bytes()).hexdigest() == digest_before

    def test_missing_input(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert(tmp_path / "nope.apkg", tmp_path / "out.db")

    def test_not_a_collection(self, tmp_path):
        junk = tmp_path / "junk.anki2"
        junk.write_text("hello world")
        with pytest.raises(ValueError, match="SQLite"):
            convert(junk, tmp_path / "out.db")

    def test_sqlite_but_not_anki(self, tmp_path):
        import sqlite3

        db = tmp_path / "random.anki2"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE foo (bar)")
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="Anki collection"):
            convert(db, tmp_path / "out.db")

    def test_corrupt_sqlite_with_valid_header(self, tmp_path):
        import os

        db = tmp_path / "corrupt.anki2"
        db.write_bytes(b"SQLite format 3\x00" + os.urandom(400))
        with pytest.raises(ValueError, match="Anki collection"):
            convert(db, tmp_path / "out.db")

    def test_corrupt_zip_member(self, legacy_db, tmp_path):
        apkg = fx.make_apkg(tmp_path / "deck.apkg", legacy_db)
        blob = bytearray(apkg.read_bytes())
        blob[60] ^= 0xFF  # flip a byte inside the stored member data
        apkg.write_bytes(blob)
        with pytest.raises(ValueError, match="(?i)corrupt|bad"):
            convert(apkg, tmp_path / "out.db")

    def test_overwrite_flag_passthrough(self, legacy_db, tmp_path):
        out = tmp_path / "out.db"
        convert(legacy_db, out)
        with pytest.raises(FileExistsError):
            convert(legacy_db, out)
        result = convert(legacy_db, out, overwrite=True)
        assert result.counts["notes"] == 3


class TestZipMemberPriority:
    def test_prefers_anki21_over_anki2(self, legacy_db, modern_db, tmp_path):
        apkg = tmp_path / "both.apkg"
        with zipfile.ZipFile(apkg, "w") as zf:
            zf.write(legacy_db, "collection.anki2")
            zf.write(modern_db, "collection.anki21")
            zf.writestr("media", "{}")
        result = convert(apkg, tmp_path / "out.db")
        assert result.schema_version == 18  # came from the anki21 member

    def test_zip_without_collection(self, tmp_path):
        bad = tmp_path / "bad.apkg"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("media", "{}")
        with pytest.raises(ValueError, match="collection"):
            convert(bad, tmp_path / "out.db")


class TestZstd:
    def test_anki21b_streaming_frame_roundtrip(self, modern_db, tmp_path):
        """Anki's exporter writes zstd frames WITHOUT the content size in the
        header; the reader must handle those, not just one-shot frames."""
        import io

        zstandard = pytest.importorskip("zstandard")
        buf = io.BytesIO()
        with zstandard.ZstdCompressor().stream_writer(buf, closefd=False) as writer:
            writer.write(modern_db.read_bytes())
        compressed = buf.getvalue()
        params = zstandard.get_frame_parameters(compressed)
        assert params.content_size == zstandard.CONTENTSIZE_UNKNOWN

        apkg = tmp_path / "new-format.colpkg"
        with zipfile.ZipFile(apkg, "w") as zf:
            zf.writestr("collection.anki21b", compressed)
            zf.writestr("meta", b"\x08\x03")
        result = convert(apkg, tmp_path / "out.db")
        assert result.schema_version == 18
        assert result.counts["notes"] == 3

    def test_anki21b_without_zstandard_gives_hint(self, modern_db, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_zstd(name, *args, **kwargs):
            if name == "zstandard":
                raise ImportError("No module named 'zstandard'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_zstd)
        apkg = tmp_path / "new-format.apkg"
        with zipfile.ZipFile(apkg, "w") as zf:
            zf.writestr("collection.anki21b", b"\x28\xb5\x2f\xfd whatever")
        with pytest.raises(sources.MissingDependencyError, match=r"anki2sqlite\[zstd\]"):
            convert(apkg, tmp_path / "out.db")
