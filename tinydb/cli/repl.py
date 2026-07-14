"""REPL driver: read SQL, accumulate until ``;``, hand to Database.execute.

The buffer logic is the core piece. A SQL statement ends at the first
unescaped ``;``. Multi-line input is supported (e.g. INSERT into a
SELECT). Empty lines and lines that are only ``;`` are ignored. Lines
starting with ``.`` are meta-commands handled by ``cli.meta``.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, TextIO

from tinydb import Database
from tinydb.cli.format import format_columns_only, format_rows, format_rows_affected
from tinydb.cli.meta import get_handlers
from tinydb.executor.result import ResultSet


def run_repl(path: str, *, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    """Run the REPL against ``path``; return process exit code."""
    in_ = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    out.write(f"tinydb v0.1.0 — file: {path}\n")
    out.write("Type .help for meta-commands or .exit to quit.\n")
    db = Database(path)
    try:
        _drive_loop(db, in_, out)
    finally:
        db.close()
    return 0


def run_script(path: str, script: str, *, stdout: Optional[TextIO] = None) -> int:
    """Run a single SQL script (split on ``;``) without an interactive REPL.

    Used by tests and by future ``--script`` modes. Each statement's
    output is written to ``stdout``.
    """
    out = stdout if stdout is not None else sys.stdout
    db = Database(path)
    try:
        for stmt in _split_statements(script):
            if not stmt.strip():
                continue
            if stmt.lstrip().startswith("."):
                handled, text = _handle_meta(db, stmt)
                if text:
                    out.write(text)
                if not handled:
                    return 0
                continue
            try:
                rs = db.execute(stmt)
            except Exception as e:
                out.write(f"error: {e}\n")
                continue
            out.write(_format_result(rs))
    finally:
        db.close()
    return 0


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _drive_loop(db: Database, in_: TextIO, out: TextIO) -> None:
    handlers = get_handlers()
    buf: list[str] = []
    while True:
        try:
            prompt = "tinydb> " if not buf else "     -> "
            line = in_.readline()
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return
        if not line:
            # EOF — flush any pending buffer (best effort).
            if buf:
                _run_statement(db, "\n".join(buf), out)
            return
        line = line.rstrip("\n")
        stripped = line.strip()
        if not buf and (not stripped or stripped == ";"):
            continue  # skip blank lines at the top level
        # Meta-commands are dispatched immediately at top-level; they
        # are not buffered because they have no terminator.
        if not buf and stripped.startswith("."):
            handled, text = _handle_meta(db, stripped)
            if text:
                out.write(text)
            if not handled:
                return
            continue
        buf.append(line)
        joined = "\n".join(buf)
        # If the buffer ends in ``;``, the statement is complete.
        if _is_complete(joined):
            statement = _strip_trailing_semicolon(joined)
            buf = []
            if not statement.strip():
                continue
            _run_statement(db, statement, out)


def _run_statement(db: Database, sql: str, out: TextIO) -> None:
    try:
        rs = db.execute(sql)
    except Exception as e:
        out.write(f"error: {e}\n")
        return
    out.write(_format_result(rs))


def _format_result(rs: ResultSet) -> str:
    if rs.rows_affected:
        return format_rows_affected(rs.rows_affected)
    if rs.columns:
        return format_rows(rs.columns, rs.rows)
    return ""


def _handle_meta(db: Database, raw: str) -> tuple[bool, str]:
    handlers = get_handlers()
    parts = raw.split(None, 1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    fn = handlers.get(name, _unknown_meta)
    return fn(db, args)


def _unknown_meta(_db: Database, raw: str) -> tuple[bool, str]:
    name = raw.split()[0] if raw.split() else ""
    return True, f"unknown meta-command: {name!r} (try .help)\n"


def _is_complete(text: str) -> bool:
    """True if ``text`` ends in ``;`` outside of any string literal."""
    i = 0
    in_str = False
    quote = ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == quote:
                in_str = False
        else:
            if ch in ("'", '"'):
                in_str = True
                quote = ch
            elif ch == ";":
                # Make sure the ; is the trailing non-whitespace char.
                rest = text[i + 1:].strip()
                if not rest:
                    return True
                # Otherwise the statement continues after a sub-expression ;.
        i += 1
    return False


def _strip_trailing_semicolon(text: str) -> str:
    idx = text.rfind(";")
    if idx == -1:
        return text
    return text[:idx].rstrip()


def _split_statements(text: str) -> list[str]:
    """Split ``text`` on top-level ``;`` outside string literals."""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    in_str = False
    quote = ""
    while i < len(text):
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
        else:
            if ch in ("'", '"'):
                in_str = True
                quote = ch
                buf.append(ch)
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    out.append(stmt)
                buf = []
            else:
                buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out