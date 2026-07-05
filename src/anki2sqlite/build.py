"""Write the analytics database: readable schema, decoded values, ready views.

Design rules:

- Every decoded column keeps its raw sibling (`raw_*`) so nothing is lost.
- Timestamps are naive local strings in the requested timezone; the timezone
  itself is recorded in `meta`.
- The DDL carries `--` comments; SQLite preserves them in sqlite_master, so
  the output database documents itself (`.schema cards`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import extract, transform

BATCH_SIZE = 10_000

SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE decks (
    deck_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,     -- leaf segment, e.g. 'Verbs'
    full_name   TEXT NOT NULL,     -- full path, e.g. 'Spanish::Verbs'
    parent_id   INTEGER,           -- NULL for top-level decks
    level       INTEGER NOT NULL,  -- 1 = top-level
    is_filtered INTEGER NOT NULL   -- 1 for filtered ("cram") decks
);

CREATE TABLE note_types (
    note_type_id INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    is_cloze     INTEGER NOT NULL
);

CREATE TABLE note_type_fields (
    note_type_id INTEGER NOT NULL,
    ord          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (note_type_id, ord)
);

CREATE TABLE card_templates (
    note_type_id INTEGER NOT NULL,
    ord          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (note_type_id, ord)
);

CREATE TABLE notes (
    note_id      INTEGER PRIMARY KEY,
    guid         TEXT,
    note_type_id INTEGER,           -- references note_types
    created_at   TEXT,
    modified_at  TEXT,
    tags         TEXT,              -- JSON array, e.g. '["spanish","verbs"]'
    fields       TEXT,              -- JSON object {field name: raw value}
    sort_field   TEXT               -- Anki's sort field, usually the "front"
);

CREATE TABLE note_fields (          -- long format: one row per note field
    note_id    INTEGER NOT NULL,
    ord        INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    value_html TEXT,                -- raw value as Anki stores it
    value_text TEXT,                -- best-effort plain text (tags stripped)
    PRIMARY KEY (note_id, ord)
);

CREATE TABLE note_tags (            -- long format: one row per note tag
    note_id INTEGER NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);

CREATE TABLE cards (
    card_id          INTEGER PRIMARY KEY,
    note_id          INTEGER NOT NULL,  -- references notes
    deck_id          INTEGER NOT NULL,  -- references decks
    template_ord     INTEGER NOT NULL,
    template_name    TEXT,
    created_at       TEXT,
    modified_at      TEXT,
    card_state       TEXT,     -- new | learning | review | relearning
    queue            TEXT,     -- scheduling queue incl. suspended/buried
    is_suspended     INTEGER NOT NULL,
    is_buried        INTEGER NOT NULL,
    due_date         TEXT,     -- date for review cards, datetime for learning
    new_position     INTEGER,  -- ordering position for new cards
    interval_days    REAL,     -- current interval (fractional if < 1 day)
    ease_factor      REAL,     -- e.g. 2.5 (Anki stores 2500)
    reps             INTEGER NOT NULL,
    lapses           INTEGER NOT NULL,
    flag             INTEGER NOT NULL,  -- 0 none, 1-7 the colored flags
    original_deck_id INTEGER,  -- set while the card sits in a filtered deck
    fsrs_stability   REAL,     -- FSRS memory state, when present
    fsrs_difficulty  REAL,
    fsrs_desired_retention REAL,
    custom_data      TEXT,     -- user plugin data, verbatim
    raw_type   INTEGER NOT NULL,
    raw_queue  INTEGER NOT NULL,
    raw_due    INTEGER NOT NULL,
    raw_ivl    INTEGER NOT NULL,
    raw_factor INTEGER NOT NULL
);

CREATE TABLE reviews (
    review_id   INTEGER PRIMARY KEY,   -- also the review's epoch-ms timestamp
    card_id     INTEGER NOT NULL,      -- references cards
    reviewed_at TEXT,
    rating      INTEGER NOT NULL,      -- 1-4 answer button, 0 for manual entries
    rating_label TEXT,                 -- again | hard | good | easy
    review_kind TEXT,                  -- learning | review | relearning | filtered | manual | rescheduled
    interval_days          REAL,       -- interval granted by this review
    previous_interval_days REAL,
    ease_factor            REAL,
    duration_ms            INTEGER,    -- time spent answering (capped by Anki)
    raw_ease     INTEGER NOT NULL,
    raw_ivl      INTEGER NOT NULL,
    raw_last_ivl INTEGER NOT NULL,
    raw_factor   INTEGER NOT NULL,
    raw_type     INTEGER NOT NULL
);

CREATE INDEX idx_notes_note_type ON notes (note_type_id);
CREATE INDEX idx_note_fields_name ON note_fields (field_name);
CREATE INDEX idx_note_tags_tag ON note_tags (tag);
CREATE INDEX idx_cards_note ON cards (note_id);
CREATE INDEX idx_cards_deck ON cards (deck_id);
CREATE INDEX idx_reviews_card ON reviews (card_id);
CREATE INDEX idx_reviews_time ON reviews (reviewed_at);
"""

VIEWS = """
-- One row per card with everything joined in: the default table to query.
CREATE VIEW v_cards AS
SELECT
    c.card_id,
    d.full_name AS deck,
    nt.name AS note_type,
    c.template_name,
    n.sort_field AS note,
    c.card_state,
    c.queue,
    c.due_date,
    c.interval_days,
    c.ease_factor,
    c.reps,
    c.lapses,
    c.is_suspended,
    c.is_buried,
    c.flag,
    c.fsrs_stability,
    c.fsrs_difficulty,
    n.tags,
    c.created_at,
    c.note_id,
    c.deck_id
FROM cards c
LEFT JOIN decks d USING (deck_id)
LEFT JOIN notes n USING (note_id)
LEFT JOIN note_types nt USING (note_type_id);

-- One row per review with card/deck/note context.
CREATE VIEW v_reviews AS
SELECT
    r.review_id,
    r.reviewed_at,
    r.rating,
    r.rating_label,
    r.review_kind,
    r.interval_days,
    r.previous_interval_days,
    r.ease_factor,
    r.duration_ms,
    d.full_name AS deck,
    nt.name AS note_type,
    n.sort_field AS note,
    r.card_id
FROM reviews r
LEFT JOIN cards c USING (card_id)
LEFT JOIN decks d USING (deck_id)
LEFT JOIN notes n USING (note_id)
LEFT JOIN note_types nt USING (note_type_id);

-- Daily study stats. Days roll over at the collection's rollover hour
-- ({rollover}:00 here), like Anki's own statistics. Manual/rescheduled
-- entries (rating 0) are excluded.
CREATE VIEW v_daily_reviews AS
SELECT
    date(datetime(reviewed_at, '-{rollover} hours')) AS day,
    COUNT(*) AS reviews,
    COUNT(DISTINCT card_id) AS unique_cards,
    ROUND(SUM(duration_ms) / 60000.0, 2) AS minutes,
    SUM(rating = 1) AS again,
    SUM(rating = 2) AS hard,
    SUM(rating = 3) AS good,
    SUM(rating = 4) AS easy,
    ROUND(AVG(rating > 1), 4) AS pass_rate
FROM reviews
WHERE rating > 0
GROUP BY day
ORDER BY day;

-- Per-deck composition and difficulty snapshot.
CREATE VIEW v_deck_stats AS
SELECT
    d.deck_id,
    d.full_name AS deck,
    COUNT(c.card_id) AS cards,
    COALESCE(SUM(c.card_state = 'new'), 0) AS new_cards,
    COALESCE(SUM(c.card_state IN ('learning', 'relearning')), 0) AS learning_cards,
    COALESCE(SUM(c.card_state = 'review'), 0) AS review_cards,
    COALESCE(SUM(c.is_suspended), 0) AS suspended_cards,
    ROUND(AVG(CASE WHEN c.card_state = 'review' THEN c.interval_days END), 1) AS avg_interval_days,
    ROUND(AVG(c.ease_factor), 3) AS avg_ease
FROM decks d
LEFT JOIN cards c USING (deck_id)
GROUP BY d.deck_id
ORDER BY d.full_name;
"""


def _batched_insert(dst: sqlite3.Connection, sql: str, rows_iter):
    batch = []
    total = 0
    for row in rows_iter:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            dst.executemany(sql, batch)
            total += len(batch)
            batch.clear()
    if batch:
        dst.executemany(sql, batch)
        total += len(batch)
    return total


def build_database(
    source: sqlite3.Connection,
    dst_path: str | Path,
    *,
    timezone: str = "UTC",
    views: bool = True,
    overwrite: bool = False,
) -> dict[str, int]:
    """Read an opened Anki collection and write the analytics DB to dst_path.

    Returns row counts per logical entity. Raises FileExistsError unless
    overwrite=True when dst_path already exists.
    """
    dst_path = Path(dst_path)
    if dst_path.exists():
        if not overwrite:
            raise FileExistsError(f"{dst_path} already exists (use overwrite/--force)")
        dst_path.unlink()

    tz = ZoneInfo(timezone)
    meta = extract.read_meta(source)
    decks = extract.read_decks(source)
    note_types = extract.read_note_types(source)

    dst = sqlite3.connect(dst_path)
    try:
        dst.executescript(SCHEMA)
        counts = {
            "decks": _write_decks(dst, decks),
            "note_types": _write_note_types(dst, note_types),
            "notes": _write_notes(dst, source, note_types, tz),
            "cards": _write_cards(dst, source, note_types, meta, tz),
            "reviews": _write_reviews(dst, source, tz),
        }
        if views:
            dst.executescript(VIEWS.replace("{rollover}", str(meta.rollover)))
        _write_meta(dst, meta, counts, timezone, tz)
        dst.commit()
    finally:
        dst.close()
    return counts


def _write_meta(dst, meta, counts, timezone_name, tz):
    from . import __version__

    rows = [
        ("anki2sqlite_version", __version__),
        ("converted_at", datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")),
        ("timezone", timezone_name),
        ("source_schema_version", str(meta.schema_version)),
        ("collection_created_at", transform.format_timestamp(meta.created_at, tz)),
        ("rollover_hour", str(meta.rollover)),
    ]
    rows += [(f"count_{name}", str(n)) for name, n in sorted(counts.items())]
    dst.executemany("INSERT INTO meta VALUES (?, ?)", rows)


def _write_decks(dst, decks):
    tree = transform.build_deck_tree({did: d.name for did, d in decks.items()})
    rows = [
        (did, node.name, node.full_name, node.parent_id, node.level,
         int(decks[did].is_filtered))
        for did, node in tree.items()
    ]
    dst.executemany("INSERT INTO decks VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def _write_note_types(dst, note_types):
    dst.executemany(
        "INSERT INTO note_types VALUES (?,?,?)",
        [(ntid, nt.name, int(nt.is_cloze)) for ntid, nt in note_types.items()],
    )
    field_rows = [
        (ntid, ord_, name)
        for ntid, nt in note_types.items()
        for ord_, name in enumerate(nt.field_names)
    ]
    dst.executemany("INSERT INTO note_type_fields VALUES (?,?,?)", field_rows)
    template_rows = [
        (ntid, ord_, name)
        for ntid, nt in note_types.items()
        for ord_, name in enumerate(nt.template_names)
    ]
    dst.executemany("INSERT INTO card_templates VALUES (?,?,?)", template_rows)
    return len(note_types)


def _write_notes(dst, source, note_types, tz):
    field_rows = []
    tag_rows = []

    def note_rows():
        for n in extract.iter_notes(source):
            nt = note_types.get(n.mid)
            fields = transform.split_fields(n.flds, nt.field_names if nt else [])
            tags = transform.split_tags(n.tags)
            for ord_, name, value in fields:
                field_rows.append((n.id, ord_, name, value, transform.strip_html(value)))
            for tag in tags:
                tag_rows.append((n.id, tag))
            yield (
                n.id,
                n.guid,
                n.mid,
                transform.format_timestamp_ms(n.id, tz),
                transform.format_timestamp(n.mod, tz),
                json.dumps(tags, ensure_ascii=False),
                json.dumps({name: value for _, name, value in fields}, ensure_ascii=False),
                str(n.sfld),
            )
            if len(field_rows) >= BATCH_SIZE:
                dst.executemany("INSERT INTO note_fields VALUES (?,?,?,?,?)", field_rows)
                field_rows.clear()
            if len(tag_rows) >= BATCH_SIZE:
                dst.executemany("INSERT OR IGNORE INTO note_tags VALUES (?,?)", tag_rows)
                tag_rows.clear()

    total = _batched_insert(dst, "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)", note_rows())
    if field_rows:
        dst.executemany("INSERT INTO note_fields VALUES (?,?,?,?,?)", field_rows)
    if tag_rows:
        dst.executemany("INSERT OR IGNORE INTO note_tags VALUES (?,?)", tag_rows)
    return total


def _template_name(note_type, ord_):
    if note_type is None:
        return None
    names = note_type.template_names
    if note_type.is_cloze:
        # Cloze cards share one template; ord is the cloze number instead.
        return names[0] if names else None
    if 0 <= ord_ < len(names):
        return names[ord_]
    return None


def _write_cards(dst, source, note_types, meta, tz):
    note_type_by_note = dict(
        source.execute("SELECT id, mid FROM notes")
    )

    def card_rows():
        for c in extract.iter_cards(source):
            nt = note_types.get(note_type_by_note.get(c.nid))
            data = transform.parse_card_data(c.data)
            due_date, position = transform.decode_due(c.type, c.queue, c.due, meta.created_at, tz)
            if position is None and "position" in data:
                position = data["position"]
            yield (
                c.id,
                c.nid,
                c.did,
                c.ord,
                _template_name(nt, c.ord),
                transform.format_timestamp_ms(c.id, tz),
                transform.format_timestamp(c.mod, tz),
                transform.card_state_label(c.type),
                transform.queue_label(c.queue),
                int(c.queue == -1),
                int(c.queue in (-2, -3)),
                due_date,
                position,
                transform.decode_interval_days(c.ivl),
                transform.decode_ease_factor(c.factor),
                c.reps,
                c.lapses,
                c.flags & 0b111,
                c.odid or None,
                data.get("stability"),
                data.get("difficulty"),
                data.get("desired_retention"),
                data.get("custom_data"),
                c.type,
                c.queue,
                c.due,
                c.ivl,
                c.factor,
            )

    sql = "INSERT INTO cards VALUES (" + ",".join("?" * 28) + ")"
    return _batched_insert(dst, sql, card_rows())


def _write_reviews(dst, source, tz):
    def review_rows():
        for r in extract.iter_reviews(source):
            yield (
                r.id,
                r.cid,
                transform.format_timestamp_ms(r.id, tz),
                r.ease,
                transform.rating_label(r.ease),
                transform.review_kind_label(r.type),
                transform.decode_interval_days(r.ivl),
                transform.decode_interval_days(r.lastIvl),
                transform.decode_ease_factor(r.factor),
                r.time,
                r.ease,
                r.ivl,
                r.lastIvl,
                r.factor,
                r.type,
            )

    sql = "INSERT INTO reviews VALUES (" + ",".join("?" * 15) + ")"
    return _batched_insert(dst, sql, review_rows())
