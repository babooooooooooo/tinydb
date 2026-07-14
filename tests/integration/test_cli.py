"""End-to-end CLI tests using subprocess to drive `python -m tinydb`.

These exercise the full REPL — statement buffering, formatting, and
meta-commands — through the actual `python -m tinydb <file>` entry
point. We use `subprocess.run` and feed stdin via a temp file because
the interactive REPL reads from a real terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run_cli(db_path: Path, stdin_text: str) -> tuple[int, str, str]:
    """Run ``python -m tinydb <db_path>`` with the given stdin and return (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "tinydb", str(db_path)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_creates_table_and_selects(tmp_path: Path):
    db = tmp_path / "cli.db"
    rc, out, err = _run_cli(
        db,
        "CREATE TABLE t (id INT PRIMARY KEY, name TEXT);\n"
        "INSERT INTO t VALUES (1, 'alice');\n"
        "INSERT INTO t VALUES (2, 'bob');\n"
        "SELECT * FROM t ORDER BY id;\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "alice" in out
    assert "bob" in out
    assert "2 rows" in out


def test_cli_multiline_statement(tmp_path: Path):
    db = tmp_path / "ml.db"
    rc, out, err = _run_cli(
        db,
        "CREATE TABLE t (id INT PRIMARY KEY, n INT);\n"
        "INSERT INTO t VALUES (1, 10);\n"
        "SELECT id,\n"
        "       n\n"
        "FROM t\n"
        "WHERE n > 5;\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "10" in out


def test_cli_meta_tables(tmp_path: Path):
    db = tmp_path / "mt.db"
    rc, out, err = _run_cli(
        db,
        "CREATE TABLE users (id INT PRIMARY KEY);\n"
        "CREATE TABLE orders (id INT PRIMARY KEY);\n"
        ".tables\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "orders" in out
    assert "users" in out


def test_cli_meta_schema(tmp_path: Path):
    db = tmp_path / "sc.db"
    rc, out, err = _run_cli(
        db,
        "CREATE TABLE t (id INT PRIMARY KEY, val INT NOT NULL);\n"
        ".schema t\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "CREATE TABLE t" in out
    assert "PRIMARY KEY" in out
    assert "NOT NULL" in out


def test_cli_error_reported(tmp_path: Path):
    db = tmp_path / "err.db"
    rc, out, err = _run_cli(
        db,
        "SELECT * FROM missing;\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "error" in out.lower()


def test_cli_unknown_meta_command(tmp_path: Path):
    db = tmp_path / "unk.db"
    rc, out, err = _run_cli(
        db,
        ".frobnicate\n"
        ".exit\n",
    )
    assert rc == 0, f"stderr={err}"
    assert "unknown" in out.lower() or "error" in out.lower()


def test_cli_no_args_prints_usage(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "tinydb"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode != 0
    assert "usage" in (proc.stdout + proc.stderr).lower()


def test_cli_help(tmp_path: Path):
    db = tmp_path / "h.db"
    rc, out, err = _run_cli(db, ".help\n.exit\n")
    assert rc == 0, f"stderr={err}"
    assert ".tables" in out
    assert ".exit" in out