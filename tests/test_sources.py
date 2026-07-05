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
        assert result.counts["notes"] == 2
        assert out.exists()

    def test_apkg(self, legacy_db, tmp_path):
        apkg = fx.make_apkg(tmp_path / "deck.apkg", legacy_db)
        result = convert(apkg, tmp_path / "out.db")
        assert result.counts == {
            "decks": 4, "note_types": 2, "notes": 2, "cards": 4, "reviews": 3,
        }

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

    def test_overwrite_flag_passthrough(self, legacy_db, tmp_path):
        out = tmp_path / "out.db"
        convert(legacy_db, out)
        with pytest.raises(FileExistsError):
            convert(legacy_db, out)
        result = convert(legacy_db, out, overwrite=True)
        assert result.counts["notes"] == 2


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
    def test_anki21b_roundtrip(self, modern_db, tmp_path):
        zstandard = pytest.importorskip("zstandard")
        apkg = tmp_path / "new-format.colpkg"
        compressed = zstandard.ZstdCompressor().compress(modern_db.read_bytes())
        with zipfile.ZipFile(apkg, "w") as zf:
            zf.writestr("collection.anki21b", compressed)
            zf.writestr("meta", b"\x08\x03")
        result = convert(apkg, tmp_path / "out.db")
        assert result.schema_version == 18
        assert result.counts["notes"] == 2

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
