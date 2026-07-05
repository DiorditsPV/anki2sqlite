"""End-to-end tests: fixture collection -> analytics database."""

import json
import sqlite3

import pytest

import conftest as fx
from anki2sqlite import build, extract


def convert_fixture(src_path, tmp_path, **kwargs):
    conn = sqlite3.connect(src_path)
    extract.prepare_connection(conn)
    out = tmp_path / "out.db"
    counts = build.build_database(conn, out, **kwargs)
    conn.close()
    dst = sqlite3.connect(out)
    dst.row_factory = sqlite3.Row
    return dst, counts


@pytest.fixture(params=["legacy_db", "modern_db"])
def built(request, tmp_path):
    src = request.getfixturevalue(request.param)
    dst, counts = convert_fixture(src, tmp_path)
    yield dst, counts, request.param
    dst.close()


def one(db, sql, *args):
    return db.execute(sql, args).fetchone()


class TestStructure:
    def test_all_tables_present(self, built):
        db, _, _ = built
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "meta", "decks", "note_types", "note_type_fields", "card_templates",
            "notes", "note_fields", "note_tags", "cards", "reviews",
        } <= tables

    def test_counts_returned(self, built):
        _, counts, _ = built
        assert counts == {"decks": 5, "note_types": 2, "notes": 3, "cards": 5, "reviews": 3}

    def test_meta(self, built):
        db, _, which = built
        meta = dict(db.execute("SELECT key, value FROM meta"))
        assert meta["source_schema_version"] == ("11" if which == "legacy_db" else "18")
        assert meta["rollover_hour"] == ("4" if which == "legacy_db" else "5")
        assert meta["timezone"] == "UTC"
        assert meta["collection_created_at"] == "2020-01-01 00:00:00"
        assert "anki2sqlite_version" in meta
        assert "converted_at" in meta


class TestDecks:
    def test_hierarchy(self, built):
        db, _, _ = built
        row = one(db, "SELECT * FROM decks WHERE deck_id=101")
        assert row["name"] == "Verbs"
        assert row["full_name"] == "Spanish::Verbs"
        assert row["parent_id"] == 100
        assert row["level"] == 2
        assert row["is_filtered"] == 0

    def test_filtered_flag(self, built):
        db, _, _ = built
        assert one(db, "SELECT is_filtered FROM decks WHERE deck_id=?", fx.FILTERED_DECK_ID)[0] == 1


class TestNoteTypes:
    def test_note_types_and_fields(self, built):
        db, _, _ = built
        assert one(db, "SELECT is_cloze FROM note_types WHERE note_type_id=?", fx.BASIC_MID)[0] == 0
        assert one(db, "SELECT is_cloze FROM note_types WHERE note_type_id=?", fx.CLOZE_MID)[0] == 1
        fields = db.execute(
            "SELECT ord, name FROM note_type_fields WHERE note_type_id=? ORDER BY ord", (fx.BASIC_MID,)
        ).fetchall()
        assert [tuple(r) for r in fields] == [(0, "Front"), (1, "Back")]
        templates = db.execute(
            "SELECT ord, name FROM card_templates WHERE note_type_id=? ORDER BY ord", (fx.BASIC_MID,)
        ).fetchall()
        assert [tuple(r) for r in templates] == [(0, "Card 1"), (1, "Card 2")]


class TestNotes:
    def test_note_row(self, built):
        db, _, _ = built
        n = one(db, "SELECT * FROM notes WHERE note_id=?", fx.NOTE_1)
        assert n["guid"] == "abcd1234"
        assert n["note_type_id"] == fx.BASIC_MID
        assert n["created_at"] == "2020-01-26 00:53:20"
        assert n["modified_at"] == "2020-01-02 00:00:00"
        assert json.loads(n["tags"]) == ["spanish", "verbs"]
        assert json.loads(n["fields"]) == {"Front": "hola<b>!</b>", "Back": "hello"}
        assert n["sort_field"] == "hola!"

    def test_note_fields_long_format(self, built):
        db, _, _ = built
        rows = db.execute(
            "SELECT ord, field_name, value_html, value_text FROM note_fields "
            "WHERE note_id=? ORDER BY ord", (fx.NOTE_1,),
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (0, "Front", "hola<b>!</b>", "hola !"),
            (1, "Back", "hello", "hello"),
        ]

    def test_note_tags_long_format(self, built):
        db, _, _ = built
        rows = db.execute(
            "SELECT tag FROM note_tags WHERE note_id=? ORDER BY tag", (fx.NOTE_1,)
        ).fetchall()
        assert [r[0] for r in rows] == ["spanish", "verbs"]


class TestCards:
    def test_review_card(self, built):
        db, _, which = built
        c = one(db, "SELECT * FROM cards WHERE card_id=?", fx.CARD_REVIEW)
        assert c["note_id"] == fx.NOTE_1
        assert c["deck_id"] == 101
        assert c["template_name"] == "Card 1"
        assert c["card_state"] == "review"
        assert c["queue"] == "review"
        assert c["due_date"] == "2020-01-11"
        assert c["interval_days"] == 15.0
        assert c["ease_factor"] == 2.5
        assert (c["reps"], c["lapses"], c["flag"]) == (7, 1, 1)
        assert (c["is_suspended"], c["is_buried"]) == (0, 0)
        assert c["raw_due"] == 10
        if which == "modern_db":
            assert c["fsrs_stability"] == 21.5
            assert c["fsrs_difficulty"] == 6.1
            assert c["fsrs_desired_retention"] == 0.9

    def test_suspended_card(self, built):
        db, _, _ = built
        c = one(db, "SELECT * FROM cards WHERE card_id=?", fx.CARD_SUSPENDED)
        assert c["is_suspended"] == 1
        assert c["queue"] == "suspended"
        assert c["card_state"] == "review"
        assert c["due_date"] == "2020-01-13"

    def test_new_card(self, built):
        db, _, _ = built
        c = one(db, "SELECT * FROM cards WHERE card_id=?", fx.CARD_NEW)
        assert c["card_state"] == "new"
        assert c["due_date"] is None
        assert c["new_position"] == 5
        assert c["interval_days"] is None
        assert c["ease_factor"] is None

    def test_learning_card_cloze_template(self, built):
        db, _, _ = built
        c = one(db, "SELECT * FROM cards WHERE card_id=?", fx.CARD_LEARNING)
        assert c["card_state"] == "learning"
        assert c["due_date"] == "2020-09-13 12:31:40"
        assert c["interval_days"] == pytest.approx(600 / 86_400)
        assert c["template_name"] == "Cloze"  # cloze ords all use the single template
        assert c["original_deck_id"] is None
        assert c["original_due_date"] is None
        assert c["raw_odue"] == 0

    def test_card_in_filtered_deck_keeps_original_due(self, built):
        db, _, _ = built
        c = one(db, "SELECT * FROM cards WHERE card_id=?", fx.CARD_FILTERED)
        assert c["deck_id"] == fx.FILTERED_DECK_ID
        assert c["original_deck_id"] == 101
        assert c["due_date"] == "2020-01-04"  # position inside the filtered deck
        assert c["original_due_date"] == "2020-01-12"  # home-deck due, from odue
        assert c["raw_odue"] == 11


class TestReviews:
    def test_good_review(self, built):
        db, _, _ = built
        r = one(db, "SELECT * FROM reviews WHERE review_id=?", fx.REVIEW_1)
        assert r["card_id"] == fx.CARD_REVIEW
        assert r["reviewed_at"] == "2020-05-20 18:40:00"
        assert (r["rating"], r["rating_label"]) == (3, "good")
        assert r["review_kind"] == "review"
        assert r["interval_days"] == 15.0
        assert r["previous_interval_days"] == 10.0
        assert r["ease_factor"] == 2.5
        assert r["duration_ms"] == 4500

    def test_learning_step_negative_interval(self, built):
        db, _, _ = built
        r = one(db, "SELECT * FROM reviews WHERE review_id=?", fx.REVIEW_2)
        assert (r["rating"], r["rating_label"]) == (1, "again")
        assert r["review_kind"] == "learning"
        assert r["interval_days"] == pytest.approx(600 / 86_400)
        assert r["previous_interval_days"] == pytest.approx(60 / 86_400)
        assert r["ease_factor"] is None

    def test_manual_entry(self, built):
        db, _, _ = built
        r = one(db, "SELECT * FROM reviews WHERE review_id=?", fx.REVIEW_3)
        assert r["rating"] == 0
        assert r["rating_label"] is None
        assert r["review_kind"] == "manual"


class TestViews:
    def test_views_exist(self, built):
        db, _, _ = built
        views = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        assert views == {"v_cards", "v_reviews", "v_daily_reviews", "v_deck_stats"}

    def test_v_cards(self, built):
        db, _, _ = built
        rows = db.execute("SELECT * FROM v_cards ORDER BY card_id").fetchall()
        assert len(rows) == 5
        assert rows[0]["deck"] == "Spanish::Verbs"
        assert rows[0]["note_type"] == "Basic"
        assert rows[0]["note"] == "hola!"

    def test_v_daily_reviews_rollover_aware(self, built):
        db, _, which = built
        rows = db.execute("SELECT * FROM v_daily_reviews").fetchall()
        # manual entry (rating 0) excluded; both answers on 2020-05-20 UTC,
        # still 2020-05-20 after subtracting the rollover hour (4 or 5).
        assert len(rows) == 1
        day = rows[0]
        assert day["day"] == "2020-05-20"
        assert day["reviews"] == 2
        assert day["again"] == 1
        assert day["good"] == 1
        assert day["pass_rate"] == 0.5
        assert day["minutes"] == pytest.approx(34_500 / 60_000, abs=0.01)

    def test_v_deck_stats(self, built):
        db, _, _ = built
        row = one(db, "SELECT * FROM v_deck_stats WHERE deck='Spanish::Verbs'")
        assert row["cards"] == 2
        assert row["review_cards"] == 2
        assert row["suspended_cards"] == 1
        cram = one(db, "SELECT * FROM v_deck_stats WHERE deck='Cram'")
        assert cram["cards"] == 1
        empty = one(db, "SELECT * FROM v_deck_stats WHERE deck=?", fx.EMPTY_DECK_NAME)
        assert empty["cards"] == 0
        assert empty["new_cards"] == 0

    def test_no_views_option(self, legacy_db, tmp_path):
        db, _ = convert_fixture(legacy_db, tmp_path, views=False)
        views = db.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
        assert views == []


class TestTimezone:
    def test_moscow(self, legacy_db, tmp_path):
        db, _ = convert_fixture(legacy_db, tmp_path, timezone="Europe/Moscow")
        n = one(db, "SELECT created_at FROM notes WHERE note_id=?", fx.NOTE_1)
        assert n["created_at"] == "2020-01-26 03:53:20"
        meta = dict(db.execute("SELECT key, value FROM meta"))
        assert meta["timezone"] == "Europe/Moscow"


class TestRobustness:
    def test_orphan_card_and_unknown_notetype(self, legacy_db, tmp_path):
        src = sqlite3.connect(legacy_db)
        src.execute(
            "INSERT INTO cards VALUES (999, 888, 101, 0, 0, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '')"
        )  # card whose note does not exist
        src.execute(
            "INSERT INTO notes VALUES (777, 'g', 424242, 0, -1, '', 'a\x1fb', 'a', 0, 0, '')"
        )  # note with unknown notetype
        src.commit()
        src.close()

        db, counts = convert_fixture(legacy_db, tmp_path)
        assert counts["cards"] == 6
        assert counts["notes"] == 4
        n = one(db, "SELECT fields FROM notes WHERE note_id=777")
        assert json.loads(n["fields"]) == {"field_0": "a", "field_1": "b"}

    def test_overwrite_protection(self, legacy_db, tmp_path):
        out = tmp_path / "out.db"
        out.write_text("existing")
        conn = sqlite3.connect(legacy_db)
        extract.prepare_connection(conn)
        with pytest.raises(FileExistsError):
            build.build_database(conn, out)
        build.build_database(conn, out, overwrite=True)
        conn.close()
        db = sqlite3.connect(out)
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 3

    def test_failed_build_leaves_no_partial_file(self, legacy_db, tmp_path):
        src = sqlite3.connect(legacy_db)
        extract.prepare_connection(src)
        src.execute("DROP TABLE revlog")
        out = tmp_path / "o.db"
        with pytest.raises(sqlite3.OperationalError):
            build.build_database(src, out)
        src.close()
        assert not out.exists()

    def test_source_name_recorded_in_meta(self, legacy_db, tmp_path):
        conn = sqlite3.connect(legacy_db)
        extract.prepare_connection(conn)
        out = tmp_path / "o.db"
        build.build_database(conn, out, source_name="legacy.anki2")
        conn.close()
        db = sqlite3.connect(out)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        assert meta["source_file"] == "legacy.anki2"
