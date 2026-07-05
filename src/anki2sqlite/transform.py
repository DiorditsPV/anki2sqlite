"""Pure helpers that decode Anki's raw storage conventions into readable values.

Anki stores timestamps as epoch seconds or milliseconds, ease factors in
permille (2500 = 250%), intervals as days when positive and seconds when
negative, and card due values whose meaning depends on the queue. Everything
here is a small, side-effect-free function so the rules stay testable.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

# Field values inside notes.flds are joined with the ASCII unit separator.
FIELD_SEPARATOR = "\x1f"

# ~50 years; review "due" values are day offsets from collection creation,
# anything past this is treated as corrupt rather than converted to a date.
MAX_PLAUSIBLE_DUE_DAYS = 18_250

CARD_STATES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}

QUEUES = {
    0: "new",
    1: "learning",
    2: "review",
    3: "day_learning",
    4: "preview",
    -1: "suspended",
    -2: "buried_by_scheduler",
    -3: "buried_by_user",
}

RATINGS = {1: "again", 2: "hard", 3: "good", 4: "easy"}

REVIEW_KINDS = {
    0: "learning",
    1: "review",
    2: "relearning",
    3: "filtered",
    4: "manual",
    5: "rescheduled",
}

_TAG_RE = re.compile(r"<[^>]*>")


def format_timestamp(epoch_seconds: int | float | None, tz: ZoneInfo) -> str | None:
    """Epoch seconds -> naive local 'YYYY-MM-DD HH:MM:SS' in the given timezone."""
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz).strftime("%Y-%m-%d %H:%M:%S")


def format_timestamp_ms(epoch_ms: int | None, tz: ZoneInfo) -> str | None:
    if epoch_ms is None:
        return None
    return format_timestamp(epoch_ms / 1000, tz)


def format_date(epoch_seconds: int | float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz).strftime("%Y-%m-%d")


def decode_ease_factor(factor: int | None) -> float | None:
    """Permille ease (2500) -> multiplier (2.5). 0 means "not set" -> None."""
    if not factor:
        return None
    return factor / 1000


def decode_interval_days(ivl: int | None) -> float | None:
    """Anki intervals: positive = days, negative = seconds, 0 = unset."""
    if not ivl:
        return None
    if ivl < 0:
        return -ivl / 86_400
    return float(ivl)


def decode_due(
    card_type: int, queue: int, due: int, crt: int, tz: ZoneInfo
) -> tuple[str | None, int | None]:
    """Resolve cards.due into (due_date, new_position).

    The meaning of `due` depends on the queue: day offset from collection
    creation for review/day-learning cards, an epoch timestamp for
    (re)learning cards, and a sort position for new cards. Suspended and
    buried cards (queue < 0) fall back to the card type to pick the rule.
    """
    if queue == 0 or (queue < 0 and card_type == 0):
        return None, due

    day_based = queue in (2, 3) or (queue < 0 and card_type == 2)
    if day_based:
        if 0 <= due <= MAX_PLAUSIBLE_DUE_DAYS:
            return format_date(crt + due * 86_400, tz), None
        return None, None

    if queue in (1, 4) or (queue < 0 and card_type in (1, 3)):
        if due > 1_000_000_000:  # epoch seconds
            return format_timestamp(due, tz), None
        if 0 <= due <= MAX_PLAUSIBLE_DUE_DAYS:  # day-learning stored as day offset
            return format_date(crt + due * 86_400, tz), None
        return None, None

    return None, None


def card_state_label(card_type: int) -> str:
    return CARD_STATES.get(card_type, str(card_type))


def queue_label(queue: int) -> str:
    return QUEUES.get(queue, str(queue))


def rating_label(ease: int) -> str | None:
    return RATINGS.get(ease)


def review_kind_label(review_type: int) -> str:
    return REVIEW_KINDS.get(review_type, str(review_type))


def split_tags(tags: str) -> list[str]:
    """Space-separated tag blob -> unique tags, original order preserved."""
    return list(dict.fromkeys(tags.split()))


def split_fields(flds: str, field_names: list[str]) -> list[tuple[int, str, str]]:
    """notes.flds -> [(ord, field_name, value)].

    Values beyond the notetype's field list (possible after notetype edits)
    get a positional fallback name.
    """
    values = flds.split(FIELD_SEPARATOR)
    rows = []
    for ord_, value in enumerate(values):
        name = field_names[ord_] if ord_ < len(field_names) else f"field_{ord_}"
        rows.append((ord_, name, value))
    return rows


def strip_html(value: str) -> str:
    """Best-effort plain text: drop tags, unescape entities, collapse whitespace."""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return " ".join(text.split())


def normalize_deck_name(name: str) -> str:
    """Modern schema separates deck levels with \\x1f; normalize to '::'."""
    return name.replace(FIELD_SEPARATOR, "::")


class DeckNode(NamedTuple):
    name: str  # leaf segment
    full_name: str
    parent_id: int | None
    level: int


def build_deck_tree(names: dict[int, str]) -> dict[int, DeckNode]:
    """{deck_id: 'A::B'} -> {deck_id: DeckNode} with parents resolved by prefix."""
    by_full_name = {full: deck_id for deck_id, full in names.items()}
    nodes = {}
    for deck_id, full in names.items():
        segments = full.split("::")
        parent_id = None
        if len(segments) > 1:
            parent_id = by_full_name.get("::".join(segments[:-1]))
        nodes[deck_id] = DeckNode(segments[-1], full, parent_id, len(segments))
    return nodes


def parse_card_data(data: str) -> dict:
    """cards.data JSON (modern Anki) -> flat dict of the analytics-relevant bits.

    Known keys: pos (original new-card position), s/d (FSRS stability and
    difficulty), dr (desired retention), cd (user custom data, kept verbatim).
    """
    if not data:
        return {}
    try:
        raw = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    mapping = {
        "pos": "position",
        "s": "stability",
        "d": "difficulty",
        "dr": "desired_retention",
        "cd": "custom_data",
    }
    return {out: raw[key] for key, out in mapping.items() if key in raw}
