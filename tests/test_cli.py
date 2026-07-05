"""Tests for the command-line interface."""

import sqlite3
import subprocess
import sys

import pytest

import conftest as fx
from anki2sqlite import __version__, cli


class TestHappyPath:
    def test_convert_with_explicit_output(self, legacy_db, tmp_path, capsys):
        out = tmp_path / "analytics.db"
        code = cli.main([str(legacy_db), "-o", str(out)])
        assert code == 0
        assert out.exists()
        stdout = capsys.readouterr().out
        assert "notes" in stdout and "2" in stdout
        assert str(out) in stdout

    def test_default_output_name(self, legacy_db, tmp_path, capsys):
        apkg = fx.make_apkg(tmp_path / "mydeck.apkg", legacy_db)
        code = cli.main([str(apkg)])
        assert code == 0
        assert (tmp_path / "mydeck.analytics.db").exists()

    def test_quiet(self, legacy_db, tmp_path, capsys):
        code = cli.main([str(legacy_db), "-o", str(tmp_path / "o.db"), "--quiet"])
        assert code == 0
        assert capsys.readouterr().out == ""

    def test_no_views(self, legacy_db, tmp_path):
        out = tmp_path / "o.db"
        assert cli.main([str(legacy_db), "-o", str(out), "--no-views"]) == 0
        db = sqlite3.connect(out)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall() == []

    def test_timezone(self, legacy_db, tmp_path):
        out = tmp_path / "o.db"
        assert cli.main([str(legacy_db), "-o", str(out), "--timezone", "Europe/Moscow"]) == 0
        db = sqlite3.connect(out)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        assert meta["timezone"] == "Europe/Moscow"

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestErrors:
    def test_missing_input(self, tmp_path, capsys):
        code = cli.main([str(tmp_path / "nope.apkg")])
        assert code == 1
        assert "not found" in capsys.readouterr().err

    def test_existing_output_needs_force(self, legacy_db, tmp_path, capsys):
        out = tmp_path / "o.db"
        assert cli.main([str(legacy_db), "-o", str(out)]) == 0
        assert cli.main([str(legacy_db), "-o", str(out)]) == 1
        assert "--force" in capsys.readouterr().err
        assert cli.main([str(legacy_db), "-o", str(out), "--force"]) == 0

    def test_bad_timezone(self, legacy_db, tmp_path, capsys):
        code = cli.main([str(legacy_db), "-o", str(tmp_path / "o.db"), "--timezone", "Mars/Olympus"])
        assert code == 1
        assert "timezone" in capsys.readouterr().err.lower()

    def test_garbage_input(self, tmp_path, capsys):
        junk = tmp_path / "junk.anki2"
        junk.write_text("nope")
        assert cli.main([str(junk)]) == 1
        assert "SQLite" in capsys.readouterr().err


class TestEntryPoint:
    def test_console_script(self):
        import os
        import shutil
        import sysconfig
        from pathlib import Path

        # POSIX venvs put scripts next to the interpreter; Windows uses Scripts\.
        search = os.pathsep.join(
            [sysconfig.get_path("scripts"), str(Path(sys.executable).parent)]
        )
        exe = shutil.which("anki2sqlite", path=search)
        assert exe is not None
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True)
        assert proc.returncode == 0
        assert __version__ in proc.stdout

    def test_python_dash_m(self):
        proc = subprocess.run(
            [sys.executable, "-m", "anki2sqlite", "--version"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0
        assert __version__ in proc.stdout
