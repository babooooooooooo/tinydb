"""Tests for binary Value serialization."""

from __future__ import annotations

import math

import pytest

from tinydb.types import Tag, Value, deserialize, serialize, size_on_disk


def _round_trip(v: Value) -> Value:
    data = serialize(v)
    out, new_offset = deserialize(data)
    assert new_offset == len(data), f"offset mismatch for {v!r}"
    return out


def _same_value(a: Value, b: Value) -> bool:
    """Structural equality that ignores SQL NULL semantics.

    Useful in tests where two values should be 'the same' regardless of
    whether SQL would consider them equal (NULL == NULL is UNKNOWN).
    """
    if a.tag is not b.tag:
        return False
    if a.tag is Tag.FLOAT:
        return math.isnan(a.payload) and math.isnan(b.payload) or a.payload == b.payload
    return a.payload == b.payload


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            Value.int_(0),
            Value.int_(1),
            Value.int_(-1),
            Value.int_(2**62),
            Value.int_(-(2**62)),
            Value.int_(42),
            Value.float_(0.0),
            Value.float_(-0.0),
            Value.float_(3.141592653589793),
            Value.float_(1e-300),
            Value.float_(1e300),
            Value.bool_(True),
            Value.bool_(False),
            Value.null(),
            Value.text(""),
            Value.text("hello"),
            Value.text("a" * 1000),
            Value.text("你好,世界"),  # multi-byte UTF-8
            Value.text("🇨🇳"),        # multi-codepoint emoji
            # New tags (commit C3).
            Value.varchar("hi"),
            Value.varchar(""),
            Value.varchar("a" * 1000),
            Value.char("x"),
            Value.char(""),
            Value.date(0),
            Value.date(20_000),
            Value.date(-1),  # pre-epoch (technically allowed for storage)
            Value.time(0),
            Value.time(86_399),  # 23:59:59
            Value.timestamp(0),
            Value.timestamp(1_700_000_000),
            Value.decimal("3.14"),
            Value.decimal("-0.001"),
            Value.smallint(0),
            Value.smallint(32_767),
            Value.smallint(-32_768),
            Value.bigint(0),
            Value.bigint(1 << 40),
            Value.bigint(-(1 << 40)),
        ],
    )
    def test_round_trip(self, value: Value):
        out = _round_trip(value)
        assert out.tag is value.tag
        assert _same_value(out, value), f"round-trip mismatch: {out!r} vs {value!r}"


class TestSizeOnDisk:
    def test_int_is_9_bytes(self):
        assert size_on_disk(Value.int_(0)) == 1 + 8

    def test_float_is_9_bytes(self):
        assert size_on_disk(Value.float_(0.0)) == 1 + 8

    def test_bool_is_2_bytes(self):
        assert size_on_disk(Value.bool_(True)) == 2

    def test_null_is_1_byte(self):
        assert size_on_disk(Value.null()) == 1

    def test_text_overhead_5_bytes(self):
        assert size_on_disk(Value.text("abc")) == 1 + 4 + 3

    def test_size_matches_actual_serialization(self):
        v = Value.text("hello, 世界")
        assert size_on_disk(v) == len(serialize(v))

    def test_varchar_overhead_5_bytes(self):
        assert size_on_disk(Value.varchar("abc")) == 1 + 4 + 3

    def test_char_overhead_5_bytes(self):
        assert size_on_disk(Value.char("abc")) == 1 + 4 + 3

    def test_decimal_overhead_5_bytes(self):
        assert size_on_disk(Value.decimal("3.14")) == 1 + 4 + 4

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: Value.date(0),
            lambda: Value.time(0),
            lambda: Value.timestamp(0),
            lambda: Value.smallint(0),
            lambda: Value.bigint(0),
        ],
    )
    def test_int64_backed_is_9_bytes(self, factory):
        assert size_on_disk(factory()) == 1 + 8


class TestTagBytes:
    """Verify the first byte of each serialized value matches the tag enum."""

    def test_int_tag(self):
        assert serialize(Value.int_(0))[0] == 0

    def test_float_tag(self):
        assert serialize(Value.float_(0.0))[0] == 1

    def test_text_tag(self):
        assert serialize(Value.text(""))[0] == 2

    def test_bool_tag(self):
        assert serialize(Value.bool_(True))[0] == 3

    def test_null_tag(self):
        assert serialize(Value.null())[0] == 4

    def test_varchar_tag(self):
        assert serialize(Value.varchar("x"))[0] == 5

    def test_char_tag(self):
        assert serialize(Value.char("x"))[0] == 6

    def test_date_tag(self):
        assert serialize(Value.date(0))[0] == 7

    def test_time_tag(self):
        assert serialize(Value.time(0))[0] == 8

    def test_timestamp_tag(self):
        assert serialize(Value.timestamp(0))[0] == 9

    def test_decimal_tag(self):
        assert serialize(Value.decimal("3.14"))[0] == 10

    def test_smallint_tag(self):
        assert serialize(Value.smallint(0))[0] == 11

    def test_bigint_tag(self):
        assert serialize(Value.bigint(0))[0] == 12


class TestOffsets:
    def test_concatenated_deserialize(self):
        # Pack several values into one buffer and walk through offsets.
        v1 = Value.int_(42)
        v2 = Value.text("hi")
        v3 = Value.null()
        v4 = Value.bool_(True)
        buf = serialize(v1) + serialize(v2) + serialize(v3) + serialize(v4)

        off = 0
        out1, off = deserialize(buf, off)
        out2, off = deserialize(buf, off)
        out3, off = deserialize(buf, off)
        out4, off = deserialize(buf, off)
        assert off == len(buf)
        assert _same_value(out1, v1)
        assert _same_value(out2, v2)
        assert _same_value(out3, v3)
        assert _same_value(out4, v4)

    def test_underrun_on_truncated_buffer(self):
        with pytest.raises(ValueError):
            deserialize(b"\x00")  # INT tag but no payload

    def test_unknown_tag_raises(self):
        with pytest.raises(ValueError):
            deserialize(b"\xff")