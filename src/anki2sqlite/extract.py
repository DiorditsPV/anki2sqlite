"""Read an Anki collection (either source schema) into a normalized shape.

Anki has stored collections in two markedly different SQLite layouts:

- schema 11 ("legacy"): decks, notetypes and config are JSON blobs inside the
  single `col` row. This is also what most shared `.apkg` files contain.
- schema 15-18 ("modern", Anki 2.1.28+): separate `decks`, `notetypes`,
  `fields`, `templates` and `config` tables, with per-row protobuf blobs.

Everything analytics needs from the protobuf blobs (the notetype kind and
whether a deck is filtered) is recoverable with a tiny protobuf field walk,
so this module has no protobuf dependency. Dispatch is per-table (does the
`decks`/`notetypes` table exist?) rather than by version number, which also
covers the transitional schema versions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterator, NamedTuple

from .transform import normalize_deck_name

DEFAULT_ROLLOVER = 4


def prepare_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Register the `unicase` collation modern Anki declares on name columns.

    Without it, any comparison or ORDER BY touching those columns raises
    "no such collation sequence". A casefold comparison is a close enough
    stand-in for read-only access.
    """

    def unicase(a: str, b: str) -> int:
        a, b = a.casefold(), b.casefold()
        return (a > b) - (a < b)

    conn.create_collation("unicase", unicase)
    return conn


@dataclass(frozen=True)
class Meta:
    schema_version: int
    created_at: int  # epoch seconds, start of the collection's first day
    rollover: int  # hour of day when Anki starts a new "day"


@dataclass(frozen=True)
class SourceDeck:
    name: str  # '::'-separated full name
    is_filtered: bool


@dataclass(frozen=True)
class SourceNoteType:
    name: str
    is_cloze: bool
    field_names: list[str]
    template_names: list[str]


class NoteRow(NamedTuple):
    id: int
    guid: str
    mid: int
    mod: int
    tags: str
    flds: str
    sfld: object  # declared INTEGER but usually holds text
    flags: int
    data: str


class CardRow(NamedTuple):
    id: int
    nid: int
    did: int
    ord: int
    mod: int
    type: int
    queue: int
    due: int
    ivl: int
    factor: int
    reps: int
    lapses: int
    odue: int
    odid: int
    flags: int
    data: str


class ReviewRow(NamedTuple):
    id: int
    cid: int
    ease: int
    ivl: int
    lastIvl: int
    factor: int
    time: int
    type: int


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# --- tiny protobuf field walk -------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _iter_proto_fields(buf: bytes) -> Iterator[tuple[int, int, int | None]]:
    """Yield (field_number, wire_type, varint_value_or_None), skipping payloads."""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _read_varint(buf, pos)
            yield field_num, wire_type, value
        elif wire_type == 1:  # 64-bit
            pos += 8
            yield field_num, wire_type, None
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            pos += length
            yield field_num, wire_type, None
        elif wire_type == 5:  # 32-bit
            pos += 4
            yield field_num, wire_type, None
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        if pos > len(buf):
            raise ValueError("truncated field payload")


def notetype_is_cloze(config: bytes) -> bool:
    """NotetypeConfig field 1 (varint `kind`): 1 = cloze, absent/0 = standard."""
    try:
        for field_num, wire_type, value in _iter_proto_fields(bytes(config)):
            if field_num == 1 and wire_type == 0:
                return value == 1
    except (ValueError, TypeError):
        pass
    return False


def deck_is_filtered(kind: bytes) -> bool:
    """DeckKind oneof: field 1 = normal, field 2 = filtered."""
    try:
        for field_num, _, _ in _iter_proto_fields(bytes(kind)):
            if field_num == 1:
                return False
            if field_num == 2:
                return True
    except (ValueError, TypeError):
        pass
    return False


# --- readers ------------------------------------------------------------------


def read_meta(conn: sqlite3.Connection) -> Meta:
    try:
        row = conn.execute("SELECT ver, crt, conf FROM col").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"not a valid Anki collection ({exc})") from exc
    if row is None:
        raise ValueError("not a valid Anki collection (empty col table)")
    ver, crt, conf_json = row

    rollover = None
    if _has_table(conn, "config"):
        row = conn.execute("SELECT val FROM config WHERE KEY='rollover'").fetchone()
        if row is not None:
            try:
                rollover = int(json.loads(bytes(row[0])))
            except (ValueError, TypeError):
                rollover = None
    if rollover is None:
        try:
            rollover = int(json.loads(conf_json).get("rollover", DEFAULT_ROLLOVER))
        except (ValueError, TypeError, AttributeError):
            rollover = DEFAULT_ROLLOVER

    return Meta(schema_version=ver, created_at=crt, rollover=rollover)


def read_decks(conn: sqlite3.Connection) -> dict[int, SourceDeck]:
    decks: dict[int, SourceDeck] = {}
    if _has_table(conn, "decks"):
        for deck_id, name, kind in conn.execute("SELECT id, name, kind FROM decks"):
            decks[deck_id] = SourceDeck(
                name=normalize_deck_name(name), is_filtered=deck_is_filtered(kind)
            )
    else:
        (decks_json,) = conn.execute("SELECT decks FROM col").fetchone()
        for key, deck in json.loads(decks_json).items():
            decks[int(key)] = SourceDeck(
                name=normalize_deck_name(deck["name"]),
                is_filtered=bool(deck.get("dyn", 0)),
            )
    return decks


def _ordered_names(items: list[dict]) -> list[str]:
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: pair[1].get("ord", pair[0]))
    return [item["name"] for _, item in indexed]


def read_note_types(conn: sqlite3.Connection) -> dict[int, SourceNoteType]:
    note_types: dict[int, SourceNoteType] = {}
    if _has_table(conn, "notetypes"):
        fields: dict[int, list[str]] = {}
        for ntid, name in conn.execute("SELECT ntid, name FROM fields ORDER BY ntid, ord"):
            fields.setdefault(ntid, []).append(name)
        templates: dict[int, list[str]] = {}
        for ntid, name in conn.execute("SELECT ntid, name FROM templates ORDER BY ntid, ord"):
            templates.setdefault(ntid, []).append(name)
        for ntid, name, config in conn.execute("SELECT id, name, config FROM notetypes"):
            note_types[ntid] = SourceNoteType(
                name=name,
                is_cloze=notetype_is_cloze(config),
                field_names=fields.get(ntid, []),
                template_names=templates.get(ntid, []),
            )
    else:
        (models_json,) = conn.execute("SELECT models FROM col").fetchone()
        for key, model in json.loads(models_json).items():
            note_types[int(key)] = SourceNoteType(
                name=model["name"],
                is_cloze=model.get("type", 0) == 1,
                field_names=_ordered_names(model.get("flds", [])),
                template_names=_ordered_names(model.get("tmpls", [])),
            )
    return note_types


def iter_notes(conn: sqlite3.Connection) -> Iterator[NoteRow]:
    cur = conn.execute(
        "SELECT id, guid, mid, mod, tags, flds, sfld, flags, data FROM notes"
    )
    yield from map(NoteRow._make, cur)


def iter_cards(conn: sqlite3.Connection) -> Iterator[CardRow]:
    cur = conn.execute(
        "SELECT id, nid, did, ord, mod, type, queue, due, ivl, factor,"
        " reps, lapses, odue, odid, flags, data FROM cards"
    )
    yield from map(CardRow._make, cur)


def iter_reviews(conn: sqlite3.Connection) -> Iterator[ReviewRow]:
    cur = conn.execute(
        "SELECT id, cid, ease, ivl, lastIvl, factor, time, type FROM revlog"
    )
    yield from map(ReviewRow._make, cur)
