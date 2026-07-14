"""Tests for type validation and coercion."""

from __future__ import annotations

import pytest

from tinydb.errors import TypeMismatchError
from tinydb.types import Tag, Value, coerce, types_comparable


class TestCoerceHappyPaths:
    def test_same_type_int(self):
        v = Value.int_(5)
        assert coerce(v, Tag.INT) is v  # exact instance returned

    def test_same_type_text(self):
        v = Value.text("hi")
        assert coerce(v, Tag.TEXT) is v

    def test_int_to_float_promotion(self):
        out = coerce(Value.int_(5), Tag.FLOAT)
        assert out.tag is Tag.FLOAT
        assert out.payload == 5.0

    def test_null_coerced_to_any(self):
        # NULL can go into any nullable column; the NOT NULL check is separate.
        # Use identity because NULL equality is UNKNOWN (None) per SQL semantics.
        null = Value.null()
        for t in (Tag.INT, Tag.FLOAT, Tag.TEXT, Tag.BOOL):
            assert coerce(null, t) is null


class TestCoerceRejections:
    def test_text_into_int(self):
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("5"), Tag.INT)

    def test_int_into_text(self):
        with pytest.raises(TypeMismatchError):
            coerce(Value.int_(5), Tag.TEXT)

    def test_float_into_text(self):
        with pytest.raises(TypeMismatchError):
            coerce(Value.float_(1.5), Tag.TEXT)

    def test_bool_into_int(self):
        # No BOOL <-> INT promotion.
        with pytest.raises(TypeMismatchError):
            coerce(Value.bool_(True), Tag.INT)

    def test_bool_into_float(self):
        with pytest.raises(TypeMismatchError):
            coerce(Value.bool_(False), Tag.FLOAT)


class TestCoerceFloatRejections:
    """FLOAT rejects inf / -inf / NaN at coerce time. finite floats pass."""

    @pytest.mark.parametrize(
        "value",
        [float("inf"), float("-inf"), float("nan")],
    )
    def test_non_finite_float_rejected(self, value: float) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.float_(value), Tag.FLOAT)

    def test_finite_float_ok(self) -> None:
        out = coerce(Value.float_(1.5), Tag.FLOAT)
        assert out.payload == 1.5

    def test_zero_float_ok(self) -> None:
        out = coerce(Value.float_(0.0), Tag.FLOAT)
        assert out.payload == 0.0


class TestTypesComparable:
    @pytest.mark.parametrize(
        "a,b",
        [
            (Tag.INT, Tag.INT),
            (Tag.FLOAT, Tag.FLOAT),
            (Tag.TEXT, Tag.TEXT),
            (Tag.BOOL, Tag.BOOL),
            (Tag.INT, Tag.FLOAT),
            (Tag.FLOAT, Tag.INT),
            (Tag.INT, Tag.NULL),
            (Tag.NULL, Tag.TEXT),
        ],
    )
    def test_comparable(self, a: Tag, b: Tag):
        assert types_comparable(a, b)

    @pytest.mark.parametrize(
        "a,b",
        [
            (Tag.INT, Tag.TEXT),
            (Tag.TEXT, Tag.INT),
            (Tag.INT, Tag.BOOL),
            (Tag.FLOAT, Tag.BOOL),
            (Tag.TEXT, Tag.BOOL),
            (Tag.BOOL, Tag.TEXT),
        ],
    )
    def test_not_comparable(self, a: Tag, b: Tag):
        assert not types_comparable(a, b)