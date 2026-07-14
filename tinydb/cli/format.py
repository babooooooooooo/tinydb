"""Tabular output for SELECT results.

Produces column-aligned, header + rule + rows style. Each cell is a
string; column widths are computed from the longest of the header or
any cell value. Empty result sets produce a single "(empty result set)"
line so the caller always sees something useful.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def format_rows(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a result set as a string.

    Returns ``""`` if both columns and rows are empty; otherwise a
    header + rule + rows table. Single-column results are printed with
    the same border style for consistency.
    """
    if not columns:
        return "(empty result set)\n"
    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _display_width(cell))
    # Build the table.
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines: list[str] = [sep, _row_line(columns, widths), sep]
    for row in rows:
        lines.append(_row_line(row, widths))
    lines.append(sep)
    if rows:
        lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines) + "\n"


def format_rows_affected(n: int) -> str:
    """One-line message for non-SELECT statements (INSERT / UPDATE / DELETE)."""
    return f"{n} row{'s' if n != 1 else ''} affected\n"


def _row_line(cells: Sequence[str], widths: Sequence[int]) -> str:
    parts = []
    for cell, w in zip(cells, widths):
        parts.append(f" {cell.ljust(w)} ")
    return "|" + "|".join(parts) + "|"


def _display_width(s: str) -> int:
    """Visible width of ``s``.

    For ASCII text the length is correct. We don't try to do anything
    fancy with East Asian wide characters — this is good enough for the
    MVP CLI.
    """
    return len(s)


def format_columns_only(columns: Sequence[str]) -> str:
    """Header-only display (used by COUNT(*) and other zero-row queries)."""
    return format_rows(columns, [])