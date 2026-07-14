"""Type validation and coercion at write time.

``coerce(literal, declared)`` returns a Value suitable for storage in a
column declared as ``declared``. NULL is always allowed (the NULL/NOT NULL
constraint is checked separately by the executor). INT may be promoted to
FLOAT for storage; all other cross-type writes raise ``TypeMismatchError``.

FLOAT additionally rejects ``inf``, ``-inf``, and NaN at coerce time
(SQL has no representation for non-finite floats).
"""

from __future__ import annotations

import math
import re

from tinydb.errors import TypeMismatchError
from tinydb.types.value import Tag, Value


def coerce(
    literal: Value,
    declared: Tag,
    declared_params: tuple[int, ...] = (),
) -> Value:
    """Coerce ``literal`` to ``declared`` type or raise ``TypeMismatchError``.

    Rules:
    - NULL literal → NULL (NOT NULL is checked elsewhere).
    - Same tag → return as-is; for FLOAT also reject non-finite literals.
    - INT into FLOAT → promote.
    - TEXT into VARCHAR(50) → must fit in N; Tag becomes VARCHAR.
    - TEXT into CHAR(5)    → pad to N (short) / reject (long); Tag becomes CHAR.
    - TEXT into DECIMAL(p, s) → must validate as digits; Tag becomes DECIMAL.
    - Any other mismatch → ``TypeMismatchError``.
    """
    if literal.tag is Tag.NULL:
        return literal
    if literal.tag is declared:
        if declared is Tag.FLOAT and not math.isfinite(literal.payload):
            raise TypeMismatchError(
                f"FLOAT value must be finite; got {literal.payload!r}"
            )
        return literal
    if literal.tag is Tag.INT and declared is Tag.FLOAT:
        return Value.float_(float(literal.payload))
    # Cross-tag coercions (literal payload must be coercible into declared).
    if literal.tag is Tag.TEXT:
        if declared is Tag.VARCHAR:
            if len(declared_params) != 1:
                raise TypeMismatchError(
                    f"VARCHAR expects a single length parameter; got {declared_params!r}"
                )
            (n,) = declared_params
            text = literal.payload
            if not isinstance(text, str) or len(text.encode("utf-8")) > n:
                raise TypeMismatchError(
                    f"VARCHAR({n}) value {text!r} exceeds length limit"
                )
            return Value.varchar(text)
        if declared is Tag.CHAR:
            if len(declared_params) != 1:
                raise TypeMismatchError(
                    f"CHAR expects a single length parameter; got {declared_params!r}"
                )
            (n,) = declared_params
            text = literal.payload
            if not isinstance(text, str):
                raise TypeMismatchError(f"CHAR requires string payload")
            byte_len = len(text.encode("utf-8"))
            if byte_len > n:
                raise TypeMismatchError(
                    f"CHAR({n}) value {text!r} exceeds length limit"
                )
            return Value.char(text.ljust(n))
        if declared is Tag.DECIMAL:
            if len(declared_params) != 2:
                raise TypeMismatchError(
                    f"DECIMAL expects (precision, scale); got {declared_params!r}"
                )
            precision, scale = declared_params
            text = literal.payload
            if not _is_valid_decimal(text, precision, scale):
                raise TypeMismatchError(
                    f"DECIMAL({precision},{scale}) value {text!r} is invalid"
                )
            return Value.decimal(text)
    raise TypeMismatchError(
        f"value {literal!r} of type {literal.tag.name} "
        f"cannot be stored in column of type {declared.name}"
    )


# Matches optional sign, integer digits, optional fractional part; no
# exponent, no leading/trailing whitespace. Splitting yields (int_part,
# frac_part) where one may be empty.
_DECIMAL_RE = re.compile(r"^([+-]?)(\d*)(?:\.(\d*))?$")


def _is_valid_decimal(text: str, precision: int, scale: int) -> bool:
    """True iff ``text`` is a valid DECIMAL(precision, scale) literal."""
    if not isinstance(text, str):
        return False
    m = _DECIMAL_RE.match(text)
    if m is None:
        return False
    sign, int_part, frac_part = m.groups()
    int_part = int_part or "0"
    frac_part = frac_part or ""
    # Leading-zero canonicalization: "-0" -> "0"; "01" -> not allowed.
    if len(int_part) > 1 and int_part[0] == "0":
        return False
    if sign == "-" and int_part == "0" and not any(c != "0" for c in frac_part):
        # "-0.00" counts as zero; reject only if value is genuinely negative.
        return False
    if len(frac_part) != scale:
        return False
    int_digits = int_part.lstrip("0") if int_part != "0" else ""
    int_digit_count = len(int_digits)
    if int_digit_count + scale > precision:
        return False
    return True


def types_comparable(a: Tag, b: Tag) -> bool:
    """Return True if values of tags ``a`` and ``b`` can be compared.

    INT <-> FLOAT numeric promotion is allowed; everything else requires
    the same tag.
    """
    if a is Tag.NULL or b is Tag.NULL:
        return True  # comparisons propagate UNKNOWN; we don't reject
    if a is b:
        return True
    if {a, b} <= {Tag.INT, Tag.FLOAT}:
        return True
    return False