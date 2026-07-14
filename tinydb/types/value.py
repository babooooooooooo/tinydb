"""Value representation: tagged scalar values + 3-valued comparison logic.

Supported tags: ``INT``, ``FLOAT``, ``TEXT``, ``BOOL``, ``NULL``.
Comparisons return Python ``None`` to represent SQL UNKNOWN (NULL propagates).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class Tag(IntEnum):
    """Scalar value type tag. The numeric value is the on-disk byte encoding."""

    INT = 0
    FLOAT = 1
    TEXT = 2
    BOOL = 3
    NULL = 4


# Sentinel returned from comparison methods to indicate SQL UNKNOWN.
# Tests/executor code must check for this explicitly.
UNKNOWN: Any = None


@dataclass(frozen=True, eq=False, slots=True)
class Value:
    """A tagged scalar value.

    Use the factory classmethods (``Value.int_(...)`` etc.) instead of
    constructing directly to keep the ``tag`` / ``payload`` invariants.
    """

    tag: Tag
    payload: Any  # int | float | str | bool | None

    # ---- factories --------------------------------------------------------

    @classmethod
    def null(cls) -> "Value":
        return cls(Tag.NULL, None)

    @classmethod
    def int_(cls, v: int) -> "Value":
        return cls(Tag.INT, int(v))

    @classmethod
    def float_(cls, v: float) -> "Value":
        return cls(Tag.FLOAT, float(v))

    @classmethod
    def text(cls, v: str) -> "Value":
        return cls(Tag.TEXT, str(v))

    @classmethod
    def bool_(cls, v: bool) -> "Value":
        return cls(Tag.BOOL, bool(v))

    # ---- predicates -------------------------------------------------------

    @property
    def is_null(self) -> bool:
        return self.tag is Tag.NULL

    # ---- hashing ----------------------------------------------------------
    # Hash on (tag, payload); NULL uses a stable constant. We do not use
    # Value in dicts/sets where NULL equality would matter.

    def __hash__(self) -> int:
        return hash((self.tag, self.payload))

    # ---- representation ---------------------------------------------------

    def __repr__(self) -> str:
        if self.tag is Tag.NULL:
            return "NULL"
        return f"{self.tag.name}({self.payload!r})"

    # ---- 3-valued comparisons --------------------------------------------
    # Returns True / False / UNKNOWN (None). NULL propagates: any comparison
    # involving a NULL operand is UNKNOWN. INT/FLOAT compare via float
    # promotion. Other cross-type comparisons are UNKNOWN.

    def __eq__(self, other: object) -> Any:
        if not isinstance(other, Value):
            return NotImplemented
        if self.tag is Tag.NULL or other.tag is Tag.NULL:
            return UNKNOWN
        if Value._both_numeric(self, other):
            return float(self.payload) == float(other.payload)
        if self.tag is not other.tag:
            return False
        return self.payload == other.payload

    def __ne__(self, other: object) -> Any:
        eq = self.__eq__(other)
        if eq is NotImplemented:
            return NotImplemented
        if eq is UNKNOWN:
            return UNKNOWN
        return not eq

    def __lt__(self, other: "Value") -> Any:
        return self._ordered(other, lambda a, b: a < b)

    def __le__(self, other: "Value") -> Any:
        return self._ordered(other, lambda a, b: a <= b)

    def __gt__(self, other: "Value") -> Any:
        return self._ordered(other, lambda a, b: a > b)

    def __ge__(self, other: "Value") -> Any:
        return self._ordered(other, lambda a, b: a >= b)

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _both_numeric(a: "Value", b: "Value") -> bool:
        # True only when BOTH sides are numeric, so cross-type comparisons
        # (e.g. TEXT vs INT, BOOL vs INT) fall into the cross-type branch.
        return a.tag in (Tag.INT, Tag.FLOAT) and b.tag in (Tag.INT, Tag.FLOAT)

    def _ordered(self, other: "Value", op) -> Any:
        if not isinstance(other, Value):
            return NotImplemented
        if self.tag is Tag.NULL or other.tag is Tag.NULL:
            return UNKNOWN
        if Value._both_numeric(self, other):
            return op(float(self.payload), float(other.payload))
        if self.tag is not other.tag:
            return UNKNOWN
        return op(self.payload, other.payload)