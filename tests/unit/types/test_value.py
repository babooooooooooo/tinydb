"""Tests for the Value representation and 3-valued comparison semantics."""

from __future__ import annotations

import pytest

from tinydb.types import Tag, Value


class TestFactories:
    def test_int(self):
        v = Value.int_(42)
        assert v.tag is Tag.INT
        assert v.payload == 42

    def test_int_coerces_non_int(self):
        # Constructor promotes to int
        v = Value.int_(42.0)
        assert v.tag is Tag.INT
        assert v.payload == 42 and isinstance(v.payload, int)

    def test_float(self):
        v = Value.float_(3.14)
        assert v.tag is Tag.FLOAT
        assert v.payload == 3.14

    def test_text(self):
        v = Value.text("hello")
        assert v.tag is Tag.TEXT
        assert v.payload == "hello"

    def test_bool_true(self):
        v = Value.bool_(True)
        assert v.tag is Tag.BOOL
        assert v.payload is True

    def test_bool_false(self):
        v = Value.bool_(False)
        assert v.tag is Tag.BOOL
        assert v.payload is False

    def test_null(self):
        v = Value.null()
        assert v.tag is Tag.NULL
        assert v.payload is None
        assert v.is_null

    def test_repr_for_null(self):
        assert repr(Value.null()) == "NULL"

    def test_repr_for_value(self):
        assert repr(Value.int_(5)) == "INT(5)"


class TestEquality:
    def test_same_int(self):
        assert Value.int_(5) == Value.int_(5)

    def test_int_vs_float_promotion(self):
        # 1 == 1.0 by numeric promotion
        assert Value.int_(1) == Value.float_(1.0)

    def test_different_int(self):
        assert Value.int_(1) != Value.int_(2)

    def test_cross_type_text_vs_int(self):
        assert Value.text("5") != Value.int_(5)

    def test_same_text(self):
        assert Value.text("abc") == Value.text("abc")

    def test_same_bool(self):
        assert Value.bool_(True) == Value.bool_(True)
        assert Value.bool_(False) == Value.bool_(False)

    def test_bool_not_equal_to_int(self):
        # True != 1 (no promotion for BOOL)
        assert Value.bool_(True) != Value.int_(1)

    def test_null_not_equal_to_anything(self):
        assert (Value.null() == Value.null()) is None  # UNKNOWN
        assert (Value.null() == Value.int_(5)) is None
        assert (Value.int_(5) == Value.null()) is None


class TestOrdering:
    def test_int_lt(self):
        assert Value.int_(1) < Value.int_(2)

    def test_int_vs_float_promotion_lt(self):
        assert Value.int_(1) < Value.float_(1.5)
        assert Value.float_(0.5) < Value.int_(1)

    def test_text_lex(self):
        assert Value.text("alice") < Value.text("bob")
        assert Value.text("a") < Value.text("aa")
        # Codepoint order (uppercase before lowercase in ASCII)
        assert Value.text("A") < Value.text("a")

    def test_text_unicode(self):
        assert Value.text("你好") < Value.text("再见")

    def test_bool_false_lt_true(self):
        assert Value.bool_(False) < Value.bool_(True)
        assert not (Value.bool_(True) < Value.bool_(False))

    def test_cross_type_unknown(self):
        # Comparing TEXT and INT (non-numeric) is UNKNOWN
        assert (Value.text("5") < Value.int_(5)) is None

    def test_null_lt_is_unknown(self):
        assert (Value.null() < Value.int_(5)) is None
        assert (Value.int_(5) < Value.null()) is None

    def test_null_gt_is_unknown(self):
        assert (Value.null() > Value.int_(5)) is None

    def test_le_and_ge(self):
        assert Value.int_(5) <= Value.int_(5)
        assert Value.int_(5) >= Value.int_(5)
        assert Value.int_(6) > Value.int_(5)
        assert Value.int_(5) < Value.int_(6)


class TestHashAndIdentity:
    def test_hashable(self):
        # Frozen dataclass should hash without error
        s = {Value.int_(1), Value.int_(2), Value.int_(1)}
        assert len(s) == 2

    def test_immutable(self):
        v = Value.int_(5)
        with pytest.raises(Exception):
            v.payload = 10  # type: ignore[misc]


class TestComparisonWithNonValue:
    def test_eq_with_non_value(self):
        # Returning NotImplemented lets Python fall back to the other operand.
        assert (Value.int_(5) == 5) is False
        # 5 == Value.int_(5) is also False because int.__eq__ returns NotImplemented
        # and Value.__eq__ returns False (since other is not Value).
        assert (Value.int_(5) == 5) is False