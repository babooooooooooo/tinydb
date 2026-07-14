"""CLI entry point: `python -m tinydb <dbfile>`."""

import sys

from tinydb.cli.repl import run_repl


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m tinydb <dbfile>", file=sys.stderr)
        return 2
    run_repl(argv[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())