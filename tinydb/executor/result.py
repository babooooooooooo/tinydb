"""ResultSet: the return value of ``Database.execute``."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ResultSet:
    """The result of executing a single SQL statement.

    ``columns``  : ordered list of projected column names. Empty for writes.
    ``rows``     : ordered list of result rows; each row is a list of
                   stringified values (one per column).
    ``rows_affected`` : meaningful only for write statements.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    rows_affected: int = 0