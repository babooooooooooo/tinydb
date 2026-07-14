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


class TestCoerceParameterized:
    """VARCHAR/CHAR enforce length/precision against ColumnMeta.params.
    DECIMAL enforces precision/scale on string payload.

    coerces accept a third declared_params: tuple[int, ...] = () argument.
    VARCHAR(50) column -> params=(50,); CHAR(4) -> params=(4,);
    DECIMAL(10,2) -> params=(10, 2). VARCHAR/CHAR return a Value tagged as
    the declared tag (Tag.VARCHAR/CHAR) with the validated string payload;
    DECIMAL returns Tag.DECIMAL with a canonical str payload.
    """

    def test_varchar_within_length_ok(self) -> None:
        out = coerce(Value.text("hi"), Tag.VARCHAR, (50,))
        assert out.tag is Tag.VARCHAR
        assert out.payload == "hi"

    def test_varchar_at_boundary_ok(self) -> None:
        out = coerce(Value.text("a" * 50), Tag.VARCHAR, (50,))
        assert out.payload == "a" * 50

    def test_varchar_exceeds_length_raises(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("x" * 51), Tag.VARCHAR, (50,))

    def test_varchar_zero_params_rejected(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("ok"), Tag.VARCHAR, ())

    def test_char_pads_short(self) -> None:
        out = coerce(Value.text("ab"), Tag.CHAR, (5,))
        assert out.tag is Tag.CHAR
        assert out.payload == "ab   "
        assert len(out.payload) == 5

    def test_char_at_boundary_ok(self) -> None:
        out = coerce(Value.text("abcde"), Tag.CHAR, (5,))
        assert out.payload == "abcde"

    def test_char_rejects_long(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("abcdef"), Tag.CHAR, (5,))

    def test_char_zero_params_rejected(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("ok"), Tag.CHAR, ())

    def test_decimal_valid(self) -> None:
        out = coerce(Value.text("3.14"), Tag.DECIMAL, (4, 2))
        assert out.tag is Tag.DECIMAL
        assert out.payload == "3.14"

    def test_decimal_too_many_digits_raises(self) -> None:
        # 4 digits total at scale 2 -> ok. 5 -> too many.
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("123.45"), Tag.DECIMAL, (4, 2))

    def test_decimal_bad_scale_raises(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("3.141"), Tag.DECIMAL, (4, 2))

    def test_decimal_invalid_format_raises(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("abc"), Tag.DECIMAL, (4, 2))

    def test_decimal_negative_within_precision(self) -> None:
        out = coerce(Value.text("-9.99"), Tag.DECIMAL, (4, 2))
        assert out.payload == "-9.99"

    def test_decimal_requires_two_params(self) -> None:
        with pytest.raises(TypeMismatchError):
            coerce(Value.text("3.14"), Tag.DECIMAL, (4,))

    def test_null_into_varchar_ok(self) -> None:
        null = Value.null()
        assert coerce(null, Tag.VARCHAR, (10,)) is null

    def test_null_into_char_ok(self) -> None:
        null = Value.null()
        assert coerce(null, Tag.CHAR, (5,)) is null

    def test_null_into_decimal_ok(self) -> None:
        null = Value.null()
        assert coerce(null, Tag.DECIMAL, (10, 2)) is null


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