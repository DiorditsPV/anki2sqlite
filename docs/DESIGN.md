# Design notes

## Goal

Turn any Anki collection into a SQLite database a person (or a pandas
script) can query without knowing Anki internals. Universality over
completeness: both historical source schemas, zero required dependencies,
no assumptions about the user's decks.

## Architecture

```
input file (.anki2 / .anki21 / .apkg / .colpkg)
   │  sources.py   – temp copy, zip member choice, optional zstd
   ▼
sqlite3.Connection (read-only temp copy)
   │  extract.py   – dual-schema readers -> normalized rows
   │  transform.py – pure decode helpers (unit-tested rules)
   ▼
build.py          – output DDL, batched inserts, views, meta
   ▼
analytics.db
```

- `transform.py` holds every decoding rule as a small pure function; the
  rules are the project's actual knowledge, so they get the densest tests.
- `extract.py` dispatches per table (`decks`/`notetypes` table exists?)
  rather than per version number, which covers transitional schemas.
- `build.py` streams rows in batches, so million-review collections do not
  balloon memory.

## Anki source formats

Two generations matter:

| | schema 11 ("legacy") | schema 15-18 ("modern") |
|---|---|---|
| Used by | Anki < 2.1.28, most shared `.apkg` | Anki 2.1.28+ collections, `.anki21b` exports |
| Decks / notetypes | JSON blobs in the single `col` row | separate `decks`, `notetypes`, `fields`, `templates` tables |
| Deck name separator | `::` | `\x1f` (unit separator) |
| Cloze flag | `model.type == 1` in JSON | protobuf `NotetypeConfig.kind == 1` |
| Filtered deck flag | `deck.dyn == 1` in JSON | protobuf `DeckKind` oneof (field 2 = filtered) |
| Rollover hour | `conf.rollover` JSON key | `config` table row, JSON-encoded value |

`notes`, `cards` and `revlog` are column-compatible across both.

The two protobuf facts we need (notetype kind, deck filtered) sit in the
first fields of their messages, so a ~30-line varint field walk replaces a
protobuf dependency. Unknown/garbage blobs degrade to the safe default
(standard notetype, normal deck). Modern collections also declare a custom
`unicase` collation on name columns; the reader registers a casefold
stand-in so those columns stay sortable.

Zip archives are tried newest-member-first: `collection.anki21b`
(zstd-compressed) > `collection.anki21` > `collection.anki2`. zstd support
is an optional extra; hitting an `.anki21b` without it raises an error that
says exactly what to install.

## Decoding rules

| Anki encoding | Output |
|---|---|
| epoch ms ids (notes, cards, revlog) | `created_at` / `reviewed_at` local strings; id kept as PK |
| `factor` permille (2500) | `ease_factor` 2.5; 0 → NULL |
| `ivl` > 0 days, < 0 seconds | `interval_days` REAL; 0 → NULL |
| `type` 0-3 | `card_state`: new / learning / review / relearning |
| `queue` -3..4 | `queue` label + `is_suspended` / `is_buried` flags |
| `due` (queue-dependent) | review → `due_date` date; learning → datetime; new → `new_position`; suspended/buried resolved via card type; implausible values → NULL (raw kept) |
| `odid` / `odue` (filtered decks) | `original_deck_id` + `original_due_date` (decoded with the card type's natural queue) |
| `flds` `\x1f`-joined | `notes.fields` JSON object + `note_fields` long rows (`value_html` + stripped `value_text`) |
| `tags` space-padded string | `notes.tags` JSON array + `note_tags` long rows |
| `revlog.ease` 1-4 (0 = manual) | `rating` + `rating_label` (NULL for manual) |
| `revlog.type` 0-5 | `review_kind`: learning / review / relearning / filtered / manual / rescheduled |
| `cards.data` JSON (`s`, `d`, `dr`, `pos`, `cd`) | `fsrs_stability`, `fsrs_difficulty`, `fsrs_desired_retention`, `new_position` fallback, `custom_data` |

Suspicious values degrade to NULL instead of raising; the `raw_*` columns
preserve the original for forensic queries.

## Decisions

- **Timestamps as naive local strings.** SQLite's date functions treat
  ISO strings with offsets as UTC, which silently shifts "day" boundaries.
  Naive strings in a user-chosen timezone (recorded in `meta`) keep
  `date(reviewed_at)` intuitive. `v_daily_reviews` additionally shifts by
  the collection's rollover hour, so its days match Anki's stats screen.
- **Never touch the source in place.** Input is copied (or extracted) to a
  temp dir before opening — pointing the tool at a live collection while
  Anki runs cannot corrupt anything.
- **EAV + JSON for note fields.** Notetypes have arbitrary fields, so a
  wide table is impossible. The long `note_fields` table makes per-field
  queries easy, and `notes.fields` (JSON object) supports
  `json_extract(fields, '$.Front')` one-liners.
- **No `anki` package dependency.** The official library would parse
  everything with full fidelity, but drags in rust wheels + protobuf and
  couples the tool to Anki's release cadence. Analytics needs names,
  values and dates — all reachable with stdlib.

## Testing

Synthetic fixture collections are built for **both** source schemas from
the same logical content (deck hierarchy, filtered deck, standard + cloze
notetypes, review/suspended/new/learning cards, FSRS data, manual revlog
entries), and the whole pipeline is asserted end-to-end against each.
Decoding rules also have direct unit tests. Nothing in the repo derives
from any real collection.

## Possible future work

- media table (filenames from `.apkg` media maps)
- deck options / scheduler config via full protobuf decode (optional extra)
- `--merge` mode to append several collections into one database
