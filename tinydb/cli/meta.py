"""REPL meta-commands: .tables, .schema, .exit, .quit.

Meta-commands start with a leading dot and are NOT SQL — they are
interpreted by the REPL driver itself. The grammar here is intentionally
simple: ``<dot> <name> [arg]``.
"""

from __future__ import annotations

from typing import Callable

from tinydb import Database


# Registry of meta-command names -> handler(db, args) -> (should_continue, output)
Handlers = dict[str, Callable[[Database, str], tuple[bool, str]]]


def _list_tables(db: Database, _args: str) -> tuple[bool, str]:
    names = db.catalog.list_tables()
    if not names:
        return True, "(no tables)\n"
    return True, "\n".join(names) + "\n"


def _show_schema(db: Database, args: str) -> tuple[bool, str]:
    name = args.strip()
    if not name:
        return True, "usage: .schema <table_name>\n"
    try:
        meta = db.catalog.get_table(name)
    except Exception as e:
        return True, f"error: {e}\n"
    lines = [f"CREATE TABLE {meta.name} ("]
    cols: list[str] = []
    for c in meta.columns:
        parts = [c.name, c.type.name]
        if c.is_primary_key:
            parts.append("PRIMARY KEY")
        if c.is_not_null:
            parts.append("NOT NULL")
        if c.is_unique:
            parts.append("UNIQUE")
        cols.append("    " + " ".join(parts))
    lines.append(",\n".join(cols))
    lines.append(");")
    if meta.indexes:
        lines.append("")
        lines.append("-- indexes:")
        for idx in meta.indexes:
            lines.append(f"--   {idx.name} ON {idx.column}{' UNIQUE' if idx.is_unique else ''}")
    return True, "\n".join(lines) + "\n"


def _exit(_db: Database, _args: str) -> tuple[bool, str]:
    return False, ""


def _quit(_db: Database, _args: str) -> tuple[bool, str]:
    return False, ""


def _help(_db: Database, _args: str) -> tuple[bool, str]:
    return True, (
        "Meta-commands:\n"
        "  .tables              list tables\n"
        "  .schema <name>       show CREATE TABLE for a table\n"
        "  .exit                exit the REPL\n"
        "  .quit                alias for .exit\n"
        "  .help                this message\n"
    )


def get_handlers() -> Handlers:
    return {
        ".tables": _list_tables,
        ".schema": _show_schema,
        ".exit": _exit,
        ".quit": _quit,
        ".help": _help,
    }