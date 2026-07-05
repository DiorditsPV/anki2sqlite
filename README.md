# anki2sqlite

Convert an Anki collection into a clean, self-documenting SQLite database
built for analytics and ad-hoc SQL.

Anki's own storage is hostile to analysis: note fields are joined with
`\x1f` separators, timestamps mix epoch seconds and milliseconds, card
state lives in magic integer enums, ease is stored in permille, intervals
switch between days and seconds depending on sign, and deck/notetype
metadata hides in JSON or protobuf blobs. `anki2sqlite` decodes all of it
into plain tables with readable names, real dates, and ready-made views —
while keeping every raw value alongside, so nothing is lost.

```
$ anki2sqlite collection.anki2 -o anki.db
collection.anki2  [modern (schema 18)]  ->  anki.db
  decks              5
  note_types        10
  notes           5959
  cards           5962
  reviews        24505
```

## Install

```bash
pip install git+https://github.com/DiorditsPV/anki2sqlite
# with support for Anki's newer zstd-compressed exports (.colpkg / .apkg from 2.1.50+):
pip install "anki2sqlite[zstd] @ git+https://github.com/DiorditsPV/anki2sqlite"
```

No runtime dependencies beyond the standard library (`zstandard` is an
optional extra). Python 3.10+.

## Usage

```bash
anki2sqlite INPUT [-o OUTPUT] [--timezone Europe/Moscow] [--force] [--no-views] [--quiet]
```

`INPUT` can be:

- `collection.anki2` / `collection.anki21` — a collection file (copy it out of
  your Anki profile folder, or point at it directly: the file is only ever
  read from a temporary copy, never opened in place)
- `deck.apkg` — a deck export
- `backup.colpkg` — a full collection export/backup

Both of Anki's on-disk generations are supported: the legacy schema 11
(JSON blobs, what most shared `.apkg` files contain) and the modern
schema 15+ (separate tables, protobuf blobs, used by Anki 2.1.28+).

From Python:

```python
from anki2sqlite import convert

result = convert("backup.colpkg", "anki.db", timezone="Europe/Moscow")
print(result.counts)  # {'decks': 5, 'note_types': 10, 'notes': 5959, ...}
```

## What you get

| Table | One row per | Highlights |
|---|---|---|
| `decks` | deck | `full_name` (`Spanish::Verbs`), `parent_id`, `level`, `is_filtered` |
| `note_types` | notetype | `is_cloze` |
| `note_type_fields`, `card_templates` | field / template | ordered names per notetype |
| `notes` | note | `created_at`, `tags` (JSON array), `fields` (JSON object), `sort_field` |
| `note_fields` | note field | long format; `value_html` raw + `value_text` stripped |
| `note_tags` | note tag | long format for easy tag joins |
| `cards` | card | `card_state`, `queue`, `due_date`, `interval_days`, `ease_factor` (2.5, not 2500), `is_suspended`, FSRS stability/difficulty, plus `raw_*` originals |
| `reviews` | review log entry | `reviewed_at`, `rating_label` (again/hard/good/easy), `review_kind`, intervals in days, `duration_ms`, plus `raw_*` originals |
| `meta` | key/value | source schema version, timezone, rollover hour, row counts |

Views (skip with `--no-views`):

- **`v_cards`** — cards joined with deck, note and notetype: the default table to query
- **`v_reviews`** — reviews with card/deck/note context
- **`v_daily_reviews`** — per-day counts, minutes, again/hard/good/easy, pass rate; day boundaries respect your collection's rollover hour, like Anki's stats screen
- **`v_deck_stats`** — per-deck composition, average interval and ease

The schema is self-documenting: `sqlite3 anki.db '.schema cards'` shows a
commented definition of every column.

## Example queries

Daily workload and success rate:

```sql
SELECT day, reviews, minutes, pass_rate FROM v_daily_reviews ORDER BY day DESC LIMIT 14;
```

Your 20 leeches (most-lapsed cards):

```sql
SELECT note, deck, lapses, ease_factor
FROM v_cards WHERE card_state = 'review'
ORDER BY lapses DESC LIMIT 20;
```

True retention for mature cards (interval ≥ 21 days before the review):

```sql
SELECT ROUND(AVG(rating > 1), 4) AS mature_retention
FROM reviews
WHERE review_kind = 'review' AND previous_interval_days >= 21;
```

Performance by tag:

```sql
SELECT t.tag,
       COUNT(*) AS reviews,
       ROUND(AVG(r.rating > 1), 3) AS pass_rate
FROM reviews r
JOIN cards c   ON c.card_id = r.card_id
JOIN note_tags t ON t.note_id = c.note_id
WHERE r.rating > 0
GROUP BY t.tag ORDER BY reviews DESC;
```

Average time to answer, by hour of day:

```sql
SELECT strftime('%H', reviewed_at) AS hour,
       COUNT(*) AS reviews,
       ROUND(AVG(duration_ms) / 1000.0, 1) AS avg_seconds
FROM reviews WHERE rating > 0
GROUP BY hour ORDER BY hour;
```

Search note text without HTML noise:

```sql
SELECT note_id, field_name, value_text
FROM note_fields
WHERE value_text LIKE '%ubiquitous%';
```

## Conventions and caveats

- **Timestamps** are naive local strings (`YYYY-MM-DD HH:MM:SS`) in the
  timezone you pass with `--timezone` (default UTC); the choice is recorded
  in `meta`. Raw epoch values survive in `notes.note_id`, `cards.card_id`
  and `reviews.review_id` (Anki uses creation epoch-milliseconds as ids).
- **`v_daily_reviews`** shifts day boundaries by the collection's rollover
  hour (default 4:00), matching Anki's own definition of "a day".
- **Every decoded card/review column keeps its raw sibling** (`raw_due`,
  `raw_ivl`, `raw_factor`, ...) so you can always drop back to Anki's
  original encoding.
- **`due_date`** is a date for review cards and a datetime for learning
  cards; for cards currently in a filtered deck it reflects the filtered
  position, with the original deck in `original_deck_id`.
- **Not converted:** media files, deleted-item tombstones (`graves`), and
  scheduler configuration (deck options) — they carry little analytical
  value. Old Anki 1.x `.anki` files are not supported.

## Development

```bash
git clone https://github.com/DiorditsPV/anki2sqlite && cd anki2sqlite
python -m venv .venv && .venv/bin/pip install -e '.[test,zstd]'
.venv/bin/pytest
```

Tests run against synthetic fixture collections in both source schemas —
no personal data involved. See [docs/DESIGN.md](docs/DESIGN.md) for
architecture notes and format details.

## License

MIT
