"""Unit tests for the pure decode helpers in anki2sqlite.transform."""

from zoneinfo import ZoneInfo

import pytest

from anki2sqlite import transform

UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")

# 2020-09-13 12:26:40 UTC
EPOCH = 1_600_000_000


class TestTimestamps:
    def test_seconds_to_utc_string(self):
        assert transform.format_timestamp(EPOCH, UTC) == "2020-09-13 12:26:40"

    def test_seconds_respects_timezone(self):
        assert transform.format_timestamp(EPOCH, MSK) == "2020-09-13 15:26:40"

    def test_milliseconds_to_string(self):
        assert transform.format_timestamp_ms(EPOCH * 1000, UTC) == "2020-09-13 12:26:40"

    def test_none_passthrough(self):
        assert transform.format_timestamp(None, UTC) is None
        assert transform.format_timestamp_ms(None, UTC) is None


class TestEaseFactor:
    def test_permille_to_float(self):
        assert transform.decode_ease_factor(2500) == 2.5

    def test_zero_is_none(self):
        assert transform.decode_ease_factor(0) is None


class TestIntervalDays:
    def test_positive_is_days(self):
        assert transform.decode_interval_days(10) == 10.0

    def test_negative_is_seconds(self):
        assert transform.decode_interval_days(-43200) == 0.5

    def test_zero_is_none(self):
        assert transform.decode_interval_days(0) is None


class TestDecodeDue:
    # crt = 2020-01-01 00:00:00 UTC (collection creation, day start)
    CRT = 1_577_836_800

    def test_review_card_due_is_date(self):
        due_date, pos = transform.decode_due(2, 2, 10, self.CRT, UTC)
        assert due_date == "2020-01-11"
        assert pos is None

    def test_day_learning_queue_due_is_date(self):
        due_date, pos = transform.decode_due(1, 3, 1, self.CRT, UTC)
        assert due_date == "2020-01-02"
        assert pos is None

    def test_learning_card_due_is_datetime(self):
        due_date, pos = transform.decode_due(1, 1, EPOCH, self.CRT, UTC)
        assert due_date == "2020-09-13 12:26:40"
        assert pos is None

    def test_new_card_due_is_position(self):
        due_date, pos = transform.decode_due(0, 0, 42, self.CRT, UTC)
        assert due_date is None
        assert pos == 42

    def test_suspended_review_card_uses_card_type(self):
        due_date, pos = transform.decode_due(2, -1, 10, self.CRT, UTC)
        assert due_date == "2020-01-11"
        assert pos is None

    def test_suspended_new_card_is_position(self):
        due_date, pos = transform.decode_due(0, -1, 7, self.CRT, UTC)
        assert due_date is None
        assert pos == 7

    def test_implausible_day_count_is_none(self):
        due_date, pos = transform.decode_due(2, 2, 999_999_999, self.CRT, UTC)
        assert due_date is None
        assert pos is None


class TestLabels:
    def test_card_state(self):
        assert transform.card_state_label(0) == "new"
        assert transform.card_state_label(1) == "learning"
        assert transform.card_state_label(2) == "review"
        assert transform.card_state_label(3) == "relearning"
        assert transform.card_state_label(99) == "99"

    def test_queue(self):
        assert transform.queue_label(0) == "new"
        assert transform.queue_label(1) == "learning"
        assert transform.queue_label(2) == "review"
        assert transform.queue_label(3) == "day_learning"
        assert transform.queue_label(4) == "preview"
        assert transform.queue_label(-1) == "suspended"
        assert transform.queue_label(-2) == "buried_by_scheduler"
        assert transform.queue_label(-3) == "buried_by_user"

    def test_rating(self):
        assert transform.rating_label(1) == "again"
        assert transform.rating_label(2) == "hard"
        assert transform.rating_label(3) == "good"
        assert transform.rating_label(4) == "easy"
        assert transform.rating_label(0) is None

    def test_review_kind(self):
        assert transform.review_kind_label(0) == "learning"
        assert transform.review_kind_label(1) == "review"
        assert transform.review_kind_label(2) == "relearning"
        assert transform.review_kind_label(3) == "filtered"
        assert transform.review_kind_label(4) == "manual"
        assert transform.review_kind_label(5) == "rescheduled"


class TestTags:
    def test_split_and_dedupe_preserving_order(self):
        assert transform.split_tags(" spanish  verbs spanish ") == ["spanish", "verbs"]

    def test_empty(self):
        assert transform.split_tags("") == []
        assert transform.split_tags("   ") == []


class TestFields:
    def test_named_by_notetype(self):
        rows = transform.split_fields("front\x1fback", ["Front", "Back"])
        assert rows == [(0, "Front", "front"), (1, "Back", "back")]

    def test_extra_values_get_fallback_names(self):
        rows = transform.split_fields("a\x1fb\x1fc", ["Front", "Back"])
        assert rows == [(0, "Front", "a"), (1, "Back", "b"), (2, "field_2", "c")]

    def test_fewer_values_than_names(self):
        rows = transform.split_fields("only", ["Front", "Back"])
        assert rows == [(0, "Front", "only")]


class TestStripHtml:
    def test_tags_removed_entities_unescaped(self):
        assert transform.strip_html("<b>Hello</b> world &amp; more") == "Hello world & more"

    def test_img_removed_and_whitespace_collapsed(self):
        assert transform.strip_html('one<br><img src="x.jpg">  two') == "one two"

    def test_escaped_angle_brackets_survive(self):
        assert transform.strip_html("&lt;tag&gt;") == "<tag>"

    def test_empty(self):
        assert transform.strip_html("") == ""


class TestDeckTree:
    def test_hierarchy_with_parents(self):
        nodes = transform.build_deck_tree(
            {1: "Default", 10: "Spanish", 11: "Spanish::Verbs", 12: "Spanish::Verbs::Irregular"}
        )
        assert nodes[1] == transform.DeckNode("Default", "Default", None, 1)
        assert nodes[10] == transform.DeckNode("Spanish", "Spanish", None, 1)
        assert nodes[11] == transform.DeckNode("Verbs", "Spanish::Verbs", 10, 2)
        assert nodes[12] == transform.DeckNode("Irregular", "Spanish::Verbs::Irregular", 11, 3)

    def test_missing_parent_is_none_but_level_kept(self):
        nodes = transform.build_deck_tree({5: "A::B::C"})
        assert nodes[5] == transform.DeckNode("C", "A::B::C", None, 3)

    def test_normalize_modern_separator(self):
        assert transform.normalize_deck_name("Spanish\x1fVerbs") == "Spanish::Verbs"
        assert transform.normalize_deck_name("Plain") == "Plain"


class TestCardData:
    def test_fsrs_fields_parsed(self):
        parsed = transform.parse_card_data('{"pos":42,"s":12.5,"d":6.1,"dr":0.9,"cd":"{\\"k\\":1}"}')
        assert parsed == {
            "position": 42,
            "stability": 12.5,
            "difficulty": 6.1,
            "desired_retention": 0.9,
            "custom_data": '{"k":1}',
        }

    def test_empty_and_garbage(self):
        assert transform.parse_card_data("") == {}
        assert transform.parse_card_data("{}") == {}
        assert transform.parse_card_data("not json") == {}
