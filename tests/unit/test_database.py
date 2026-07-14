"""Tests for top-level Database helpers (currently ``_format_value``)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from tinydb.database import _format_value
from tinydb.types import Value


class TestFormatScalar:
    def test_int(self) -> None:
        assert _format_value(Value.int_(42)) == "42"

    def test_int_zero(self) -> None:
        assert _format_value(Value.int_(0)) == "0"

    def test_float_whole_renders_as_int(self) -> None:
        # 1.0 has no fractional part → render as "1" (matches pre-C10 behavior).
        assert _format_value(Value.float_(1.0)) == "1"

    def test_float_fractional(self) -> None:
        assert _format_value(Value.float_(3.14)) == "3.14"

    def test_text(self) -> None:
        # TEXT payload is rendered WITHOUT surrounding quotes.
        assert _format_value(Value.text("hello")) == "hello"

    def test_text_empty(self) -> None:
        assert _format_value(Value.text("")) == ""

    def test_bool_true(self) -> None:
        assert _format_value(Value.bool_(True)) == "TRUE"

    def test_bool_false(self) -> None:
        assert _format_value(Value.bool_(False)) == "FALSE"

    def test_null_renders_as_empty_string(self) -> None:
        # NULL is the only tag that intentionally maps to the empty string.
        assert _format_value(Value.null()) == ""


class TestFormatNewScalars:
    """Renders added in C10: VARCHAR/CHAR/DATE/TIME/TIMESTAMP/DECIMAL/SMALLINT/BIGINT."""

    def test_varchar_renders_without_quotes(self) -> None:
        assert _format_value(Value.varchar("alice")) == "alice"

    def test_varchar_empty(self) -> None:
        assert _format_value(Value.varchar("")) == ""

    def test_char_preserves_trailing_spaces(self) -> None:
        # CRITICAL: CHAR(N) is right-padded at coerce time; do not strip here.
        v = Value.char("AB   ")  # simulate post-coerce padded payload
        assert v.payload == "AB   "
        assert _format_value(v) == "AB   "

    def test_decimal_renders_without_quotes(self) -> None:
        assert _format_value(Value.decimal("3.14")) == "3.14"

    def test_decimal_canonical_string(self) -> None:
        # DECIMAL payload is already a canonical string; renderer must not
        # re-format it (e.g. no scientific notation, no leading zeros).
        assert _format_value(Value.decimal("0.00")) == "0.00"

    def test_smallint(self) -> None:
        assert _format_value(Value.smallint(42)) == "42"

    def test_smallint_negative(self) -> None:
        assert _format_value(Value.smallint(-32768)) == "-32768"

    def test_bigint(self) -> None:
        assert _format_value(Value.bigint(1 << 40)) == str(1 << 40)

    def test_bigint_int64_max(self) -> None:
        v = Value.bigint(2**63 - 1)
        assert _format_value(v) == str(2**63 - 1)

    def test_date_iso_round_trip(self) -> None:
        # Inverse of C8: payload is days-since-epoch, render is YYYY-MM-DD.
        days = (date(2025, 1, 15) - date(1970, 1, 1)).days
        assert _format_value(Value.date(days)) == "2025-01-15"

    def test_time_iso_round_trip(self) -> None:
        # Inverse of C8: payload is seconds-of-day.
        secs = 13 * 3600 + 45 * 60 + 30
        assert _format_value(Value.time(secs)) == "13:45:30"

    def test_time_with_zero_seconds(self) -> None:
        assert _format_value(Value.time(0)) == "00:00:00"

    def test_timestamp_iso_round_trip(self) -> None:
        # Inverse of C8: payload is epoch seconds (UTC).
        ts = datetime(2025, 1, 15, 13, 45, 30, tzinfo=timezone.utc)
        assert _format_value(Value.timestamp(int(ts.timestamp()))) == "2025-01-15T13:45:30+00:00"

    def test_timestamp_epoch_zero_is_1970_01_01(self) -> None:
        assert _format_value(Value.timestamp(0)) == "1970-01-01T00:00:00+00:00"


class TestFormatValueImports:
    """Smoke: the renderer accepts a Value carrying each new tag without
    raising. Behavior is exercised in TestFormatNewScalars and TestFormatScalar.
    """

    @pytest.mark.parametrize(
        "value",
        [
            Value.varchar("x"),
            Value.char("x"),
            Value.decimal("0"),
            Value.date(0),
            Value.time(0),
            Value.timestamp(0),
            Value.smallint(0),
            Value.bigint(0),
        ],
        ids=["varchar", "char", "decimal", "date", "time", "timestamp", "smallint", "bigint"],
    )
    def test_no_tag_raises(self, value: Value) -> None:
        # Renderer must handle every new tag; the _format_value call returning
        # a string is the contract (it may be empty for NULL but must not
        # raise an AttributeError or KeyError).
        result = _format_value(value)
        assert isinstance(result, str)
