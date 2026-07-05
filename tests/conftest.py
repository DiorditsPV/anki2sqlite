"""Synthetic Anki collections for tests.

Two builders produce the same logical collection in the two on-disk formats
anki2sqlite supports:

- schema 11 ("legacy"): decks/notetypes as JSON blobs in the `col` row; the
  format inside most shared `.apkg` files.
- schema 18 ("modern"): separate decks/notetypes/fields/templates/config
  tables with protobuf blobs; what Anki 2.1.28+ uses on disk.

The logical content covers the decoding paths that matter: deck hierarchy,
a filtered deck, standard + cloze notetypes, tags, HTML in fields, review /
suspended / new / learning cards, FSRS-style card data, and several review
log kinds.
"""

import json
import sqlite3
import zipfile

import pytest

# 2020-01-01 00:00:00 UTC — collection creation (day start).
CRT = 1_577_836_800

BASIC_MID = 1001
CLOZE_MID = 1002

NOTE_1 = 1_580_000_000_001  # Basic, tags, HTML in field
NOTE_2 = 1_580_000_000_002  # Cloze

CARD_REVIEW = 1_580_000_001_001  # note 1 ord 0: review card in Spanish::Verbs
CARD_SUSPENDED = 1_580_000_001_002  # note 1 ord 1: suspended review card
CARD_NEW = 1_580_000_001_003  # note 2 ord 0: new card, position 5
CARD_LEARNING = 1_580_000_001_004  # note 2 ord 1: learning card, due = epoch

REVIEW_1 = 1_590_000_000_001  # good review of CARD_REVIEW
REVIEW_2 = 1_590_000_000_002  # "again" learning step, negative interval
REVIEW_3 = 1_590_000_000_003  # manual entry, ease 0

LEARNING_DUE = 1_600_000_300  # epoch seconds

DECKS = {
    1: "Default",
    100: "Spanish",
    101: "Spanish::Verbs",
    200: "Cram",  # filtered
}
FILTERED_DECK_ID = 200

COMMON_DDL = """
CREATE TABLE col (
    id integer PRIMARY KEY, crt integer NOT NULL, mod integer NOT NULL,
    scm integer NOT NULL, ver integer NOT NULL, dty integer NOT NULL,
    usn integer NOT NULL, ls integer NOT NULL, conf text NOT NULL,
    models text NOT NULL, decks text NOT NULL, dconf text NOT NULL,
    tags text NOT NULL
);
CREATE TABLE notes (
    id integer PRIMARY KEY, guid text NOT NULL, mid integer NOT NULL,
    mod integer NOT NULL, usn integer NOT NULL, tags text NOT NULL,
    flds text NOT NULL, sfld integer NOT NULL, csum integer NOT NULL,
    flags integer NOT NULL, data text NOT NULL
);
CREATE TABLE cards (
    id integer PRIMARY KEY, nid integer NOT NULL, did integer NOT NULL,
    ord integer NOT NULL, mod integer NOT NULL, usn integer NOT NULL,
    type integer NOT NULL, queue integer NOT NULL, due integer NOT NULL,
    ivl integer NOT NULL, factor integer NOT NULL, reps integer NOT NULL,
    lapses integer NOT NULL, left integer NOT NULL, odue integer NOT NULL,
    odid integer NOT NULL, flags integer NOT NULL, data text NOT NULL
);
CREATE TABLE revlog (
    id integer PRIMARY KEY, cid integer NOT NULL, usn integer NOT NULL,
    ease integer NOT NULL, ivl integer NOT NULL, lastIvl integer NOT NULL,
    factor integer NOT NULL, time integer NOT NULL, type integer NOT NULL
);
CREATE TABLE graves (usn integer NOT NULL, oid integer NOT NULL, type integer NOT NULL);
"""

MODERN_DDL = """
CREATE TABLE decks (
    id integer PRIMARY KEY NOT NULL, name text NOT NULL, mtime_secs integer NOT NULL,
    usn integer NOT NULL, common blob NOT NULL, kind blob NOT NULL
);
CREATE TABLE notetypes (
    id integer NOT NULL PRIMARY KEY, name text NOT NULL, mtime_secs integer NOT NULL,
    usn integer NOT NULL, config blob NOT NULL
);
CREATE TABLE fields (
    ntid integer NOT NULL, ord integer NOT NULL, name text NOT NULL,
    config blob NOT NULL, PRIMARY KEY (ntid, ord)
) WITHOUT ROWID;
CREATE TABLE templates (
    ntid integer NOT NULL, ord integer NOT NULL, name text NOT NULL,
    mtime_secs integer NOT NULL, usn integer NOT NULL, config blob NOT NULL,
    PRIMARY KEY (ntid, ord)
) WITHOUT ROWID;
CREATE TABLE deck_config (
    id integer PRIMARY KEY NOT NULL, name text NOT NULL, mtime_secs integer NOT NULL,
    usn integer NOT NULL, config blob NOT NULL
);
CREATE TABLE config (
    KEY text NOT NULL PRIMARY KEY, usn integer NOT NULL,
    mtime_secs integer NOT NULL, val blob NOT NULL
) WITHOUT ROWID;
CREATE TABLE tags (
    tag text NOT NULL PRIMARY KEY, usn integer NOT NULL,
    collapsed boolean NOT NULL, config blob NULL
) WITHOUT ROWID;
"""


def _insert_shared_rows(conn, *, review_card_data="", new_card_data=""):
    """notes / cards / revlog are byte-identical across both schemas."""
    conn.executemany(
        "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                NOTE_1, "abcd1234", BASIC_MID, CRT + 86_400, -1,
                " spanish verbs ", "hola<b>!</b>\x1fhello", "hola!", 0, 0, "",
            ),
            (
                NOTE_2, "efgh5678", CLOZE_MID, CRT + 90_000, -1,
                "", "{{c1::gato}} y {{c2::perro}}\x1fnota", "gato y perro", 0, 0, "",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # id, nid, did, ord, mod, usn, type, queue, due, ivl, factor,
            # reps, lapses, left, odue, odid, flags, data
            (CARD_REVIEW, NOTE_1, 101, 0, CRT + 100_000, -1, 2, 2, 10, 15, 2500,
             7, 1, 0, 0, 0, 1, review_card_data),
            (CARD_SUSPENDED, NOTE_1, 101, 1, CRT + 100_000, -1, 2, -1, 12, 20, 2300,
             3, 0, 0, 0, 0, 0, ""),
            (CARD_NEW, NOTE_2, 100, 0, CRT + 100_000, -1, 0, 0, 5, 0, 0,
             0, 0, 0, 0, 0, 0, new_card_data),
            (CARD_LEARNING, NOTE_2, 1, 1, CRT + 100_000, -1, 1, 1, LEARNING_DUE,
             -600, 2500, 1, 0, 1001, 0, 0, 0, ""),
        ],
    )
    conn.executemany(
        "INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # id, cid, usn, ease, ivl, lastIvl, factor, time, type
            (REVIEW_1, CARD_REVIEW, -1, 3, 15, 10, 2500, 4500, 1),
            (REVIEW_2, CARD_REVIEW, -1, 1, -600, -60, 0, 30_000, 0),
            (REVIEW_3, CARD_LEARNING, -1, 0, 3, 0, 0, 0, 4),
        ],
    )


def build_legacy_collection(path):
    """Schema 11: decks/models/conf live as JSON inside the single col row."""
    models = {
        str(BASIC_MID): {
            "id": BASIC_MID,
            "name": "Basic",
            "type": 0,
            "flds": [{"name": "Front", "ord": 0}, {"name": "Back", "ord": 1}],
            "tmpls": [{"name": "Card 1", "ord": 0}, {"name": "Card 2", "ord": 1}],
            "sortf": 0,
        },
        str(CLOZE_MID): {
            "id": CLOZE_MID,
            "name": "Cloze",
            "type": 1,
            "flds": [{"name": "Text", "ord": 0}, {"name": "Back Extra", "ord": 1}],
            "tmpls": [{"name": "Cloze", "ord": 0}],
            "sortf": 0,
        },
    }
    decks = {
        str(did): {"id": did, "name": name, "dyn": 1 if did == FILTERED_DECK_ID else 0}
        for did, name in DECKS.items()
    }
    conf = {"rollover": 4, "curDeck": 1}

    conn = sqlite3.connect(path)
    conn.executescript(COMMON_DDL)
    conn.execute(
        "INSERT INTO col VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, CRT, CRT * 1000, CRT * 1000, 11, 0, 0, 0,
         json.dumps(conf), json.dumps(models), json.dumps(decks), "{}", "{}"),
    )
    _insert_shared_rows(conn)
    conn.commit()
    conn.close()
    return path


def build_modern_collection(path):
    """Schema 18: separate tables, \\x1f deck separators, protobuf-encoded kinds."""
    conn = sqlite3.connect(path)
    conn.executescript(COMMON_DDL)
    conn.executescript(MODERN_DDL)
    conn.execute(
        "INSERT INTO col VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, CRT, CRT * 1000, CRT * 1000, 18, 0, 0, 0, "{}", "{}", "{}", "{}", "{}"),
    )
    normal_kind = b"\x0a\x02\x08\x01"  # DeckKind{normal{...}}
    filtered_kind = b"\x12\x00"  # DeckKind{filtered{}}
    conn.executemany(
        "INSERT INTO decks VALUES (?,?,?,?,?,?)",
        [
            (did, name.replace("::", "\x1f"), CRT, -1, b"",
             filtered_kind if did == FILTERED_DECK_ID else normal_kind)
            for did, name in DECKS.items()
        ],
    )
    standard_config = b"\x1a\x03abc"  # css only; kind field absent -> standard
    cloze_config = b"\x08\x01\x1a\x03abc"  # kind = 1 (cloze) + css
    conn.executemany(
        "INSERT INTO notetypes VALUES (?,?,?,?,?)",
        [
            (BASIC_MID, "Basic", CRT, -1, standard_config),
            (CLOZE_MID, "Cloze", CRT, -1, cloze_config),
        ],
    )
    conn.executemany(
        "INSERT INTO fields VALUES (?,?,?,?)",
        [
            (BASIC_MID, 0, "Front", b""),
            (BASIC_MID, 1, "Back", b""),
            (CLOZE_MID, 0, "Text", b""),
            (CLOZE_MID, 1, "Back Extra", b""),
        ],
    )
    conn.executemany(
        "INSERT INTO templates VALUES (?,?,?,?,?,?)",
        [
            (BASIC_MID, 0, "Card 1", CRT, -1, b""),
            (BASIC_MID, 1, "Card 2", CRT, -1, b""),
            (CLOZE_MID, 0, "Cloze", CRT, -1, b""),
        ],
    )
    conn.execute("INSERT INTO deck_config VALUES (?,?,?,?,?)", (1, "Default", CRT, -1, b""))
    conn.execute("INSERT INTO config VALUES (?,?,?,?)", ("rollover", -1, CRT, b"5"))
    conn.executemany(
        "INSERT INTO tags VALUES (?,?,?,?)",
        [("spanish", -1, 0, None), ("verbs", -1, 0, None)],
    )
    _insert_shared_rows(
        conn,
        review_card_data='{"s":21.5,"d":6.1,"dr":0.9}',
        new_card_data='{"pos":5}',
    )
    conn.commit()
    conn.close()
    return path


def make_apkg(zip_path, db_path, member="collection.anki2"):
    """Wrap a collection file in a minimal .apkg/.colpkg zip."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(db_path, member)
        zf.writestr("media", "{}")
    return zip_path


@pytest.fixture
def legacy_db(tmp_path):
    return build_legacy_collection(tmp_path / "legacy.anki2")


@pytest.fixture
def modern_db(tmp_path):
    return build_modern_collection(tmp_path / "modern.anki2")
