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

from tinydb.errors import TypeMismatchError
from tinydb.types.value import Tag, Value


def coerce(literal: Value, declared: Tag) -> Value:
    """Coerce ``literal`` to ``declared`` type or raise ``TypeMismatchError``.

    Rules:
    - Same tag → return as-is.
    - literal is NULL → return NULL (NOT NULL is checked elsewhere).
    - literal is INT, declared is FLOAT → promote to float.
    - FLOAT declared rejects non-finite literals (inf, -inf, NaN).
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
    raise TypeMismatchError(
        f"value {literal!r} of type {literal.tag.name} "
        f"cannot be stored in column of type {declared.name}"
    )


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