"""Row representation: a mapping of column-name → Value."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tinydb.types import Value


@dataclass(frozen=True)
class Row:
    """A row is an immutable mapping column-name → Value.

    Rows are produced by scans and consumed by operators. Equality is by
    full content so the same logical row can be deduped.
    """

    values: Mapping[str, Value]

    def get(self, column: str) -> Value:
        return self.values[column]

    def __getitem__(self, column: str) -> Value:
        return self.values[column]

    def __contains__(self, column: str) -> bool:
        return column in self.values