"""Tests for reading both Anki source schemas into a normalized shape."""

import sqlite3

import pytest

import conftest as fx
from anki2sqlite import extract


def open_source(path):
    conn = sqlite3.connect(path)
    extract.prepare_connection(conn)
    return conn


@pytest.fixture(params=["legacy_db", "modern_db"])
def source(request):
    """Run the same logical assertions against both schemas."""
    path = request.getfixturevalue(request.param)
    conn = open_source(path)
    yield conn, request.param
    conn.close()


class TestMeta:
    def test_legacy(self, legacy_db):
        conn = open_source(legacy_db)
        meta = extract.read_meta(conn)
        assert meta.schema_version == 11
        assert meta.created_at == fx.CRT
        assert meta.rollover == 4

    def test_modern(self, modern_db):
        conn = open_source(modern_db)
        meta = extract.read_meta(conn)
        assert meta.schema_version == 18
        assert meta.created_at == fx.CRT
        assert meta.rollover == 5  # from the config table, not the default


class TestDecks:
    def test_names_normalized_and_filtered_flag(self, source):
        conn, _ = source
        decks = extract.read_decks(conn)
        assert {d: v.name for d, v in decks.items()} == fx.DECKS
        assert decks[fx.FILTERED_DECK_ID].is_filtered is True
        assert decks[101].is_filtered is False
        assert decks[101].name == "Spanish::Verbs"


class TestNoteTypes:
    def test_fields_templates_and_cloze(self, source):
        conn, _ = source
        note_types = extract.read_note_types(conn)
        basic = note_types[fx.BASIC_MID]
        assert basic.name == "Basic"
        assert basic.is_cloze is False
        assert basic.field_names == ["Front", "Back"]
        assert basic.template_names == ["Card 1", "Card 2"]
        cloze = note_types[fx.CLOZE_MID]
        assert cloze.name == "Cloze"
        assert cloze.is_cloze is True
        assert cloze.field_names == ["Text", "Back Extra"]
        assert cloze.template_names == ["Cloze"]


class TestRows:
    def test_notes(self, source):
        conn, _ = source
        notes = list(extract.iter_notes(conn))
        assert len(notes) == 2
        n1 = next(n for n in notes if n.id == fx.NOTE_1)
        assert n1.guid == "abcd1234"
        assert n1.mid == fx.BASIC_MID
        assert n1.tags == " spanish verbs "
        assert n1.flds == "hola<b>!</b>\x1fhello"
        assert n1.sfld == "hola!"

    def test_cards(self, source):
        conn, which = source
        cards = {c.id: c for c in extract.iter_cards(conn)}
        assert len(cards) == 4
        c1 = cards[fx.CARD_REVIEW]
        assert (c1.nid, c1.did, c1.ord) == (fx.NOTE_1, 101, 0)
        assert (c1.type, c1.queue, c1.due, c1.ivl, c1.factor) == (2, 2, 10, 15, 2500)
        assert (c1.reps, c1.lapses, c1.flags) == (7, 1, 1)
        if which == "modern_db":
            assert c1.data == '{"s":21.5,"d":6.1,"dr":0.9}'
        assert cards[fx.CARD_LEARNING].due == fx.LEARNING_DUE

    def test_reviews(self, source):
        conn, _ = source
        reviews = list(extract.iter_reviews(conn))
        assert len(reviews) == 3
        r1 = next(r for r in reviews if r.id == fx.REVIEW_1)
        assert (r1.cid, r1.ease, r1.ivl, r1.lastIvl, r1.factor, r1.time, r1.type) == (
            fx.CARD_REVIEW, 3, 15, 10, 2500, 4500, 1,
        )


class TestProtobufPeek:
    def test_notetype_kind(self):
        assert extract.notetype_is_cloze(b"\x08\x01") is True
        assert extract.notetype_is_cloze(b"") is False
        assert extract.notetype_is_cloze(b"\x1a\x03abc") is False
        # kind after another field still found by the field-skipping walk
        assert extract.notetype_is_cloze(b"\x1a\x03abc\x08\x01") is True
        assert extract.notetype_is_cloze(b"\xff\xff") is False  # garbage

    def test_deck_kind(self):
        assert extract.deck_is_filtered(b"\x12\x00") is True
        assert extract.deck_is_filtered(b"\x0a\x02\x08\x01") is False
        assert extract.deck_is_filtered(b"") is False


class TestUnicaseCollation:
    def test_order_by_on_unicase_column_works(self, tmp_path):
        """Real modern collections declare COLLATE unicase on name columns.
        A plain connection cannot even sort by them; prepare_connection fixes it."""
        path = tmp_path / "unicase.anki2"
        writer = sqlite3.connect(path)
        writer.create_collation("unicase", lambda a, b: (a > b) - (a < b))
        writer.execute("CREATE TABLE decks (id integer PRIMARY KEY, name text COLLATE unicase)")
        writer.execute("INSERT INTO decks VALUES (1, 'Default')")
        writer.commit()
        writer.close()

        conn = sqlite3.connect(path)
        with pytest.raises(sqlite3.OperationalError, match="unicase"):
            conn.execute("SELECT name FROM decks ORDER BY name").fetchall()

        extract.prepare_connection(conn)
        rows = conn.execute("SELECT name FROM decks ORDER BY name").fetchall()
        assert rows == [("Default",)]
