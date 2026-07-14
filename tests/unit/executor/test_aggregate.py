"""Tests for aggregate operators (COUNT, SUM, AVG, GROUP BY)."""

from __future__ import annotations

import pytest

from tinydb import Database


@pytest.fixture
def shop(tmp_db):
    tmp_db.execute("CREATE TABLE orders (id INT PRIMARY KEY, region TEXT NOT NULL, amount INT)")
    rows = [
        (1, "north", 100),
        (2, "south", 200),
        (3, "north", 150),
        (4, "east", 50),
        (5, "south", 75),
        (6, "north", 25),
    ]
    for r in rows:
        tmp_db.execute(f"INSERT INTO orders VALUES ({r[0]}, '{r[1]}', {r[2]})")
    return tmp_db


def test_count_star(shop):
    rs = shop.execute("SELECT COUNT(*) FROM orders")
    assert rs.rows == [["6"]]


def test_count_column_ignores_null(shop):
    shop.execute("CREATE TABLE t (val INT)")
    shop.execute("INSERT INTO t VALUES (1)")
    shop.execute("INSERT INTO t VALUES (NULL)")
    shop.execute("INSERT INTO t VALUES (3)")
    shop.execute("INSERT INTO t VALUES (NULL)")
    rs = shop.execute("SELECT COUNT(val) FROM t")
    assert rs.rows == [["2"]]


def test_sum(shop):
    rs = shop.execute("SELECT SUM(amount) FROM orders")
    assert rs.rows == [[str(100 + 200 + 150 + 50 + 75 + 25)]]


def test_avg(shop):
    total = 100 + 200 + 150 + 50 + 75 + 25
    rs = shop.execute("SELECT AVG(amount) FROM orders")
    expected = total / 6
    # Float is rendered as '100.0' (or similar) by the formatter.
    assert float(rs.rows[0][0]) == pytest.approx(expected)


def test_unknown_aggregate_rejected(shop):
    # MIN/MAX are not implemented; the parser rejects unknown function names.
    import pytest as _pytest
    from tinydb.errors import ParseError
    with _pytest.raises(ParseError):
        shop.execute("SELECT MIN(amount) FROM orders")


def test_group_by(shop):
    rs = shop.execute("SELECT region, COUNT(*) FROM orders GROUP BY region ORDER BY region")
    assert rs.rows == [
        ["east", "1"],
        ["north", "3"],
        ["south", "2"],
    ]


def test_group_by_sum(shop):
    rs = shop.execute("SELECT region, SUM(amount) FROM orders GROUP BY region ORDER BY region")
    assert rs.rows == [
        ["east", "50"],
        ["north", str(100 + 150 + 25)],
        ["south", str(200 + 75)],
    ]


def test_group_by_with_filter(shop):
    rs = shop.execute(
        "SELECT region, COUNT(*) FROM orders WHERE amount > 60 GROUP BY region ORDER BY region"
    )
    # north: 100, 150 (>60) → 2; south: 200, 75 → 2; east: 50 (excluded) → 0 (no row).
    assert rs.rows == [
        ["north", "2"],
        ["south", "2"],
    ]


def test_having_not_supported(shop):
    # HAVING is not in scope; query should succeed and the HAVING predicate
    # would simply be ignored. Verify the basic SELECT still works.
    rs = shop.execute("SELECT region, COUNT(*) FROM orders GROUP BY region ORDER BY region")
    assert len(rs.rows) == 3


def test_sum_empty_input_returns_null(shop):
    """SUM over no rows (or all-NULL) returns NULL, per SQL standard.

    Currently SUM returns 0 for empty input, which mixes up a missing
    measurement with a real zero.
    """
    shop.execute("CREATE TABLE empty (val INT)")
    rs = shop.execute("SELECT SUM(val) FROM empty")
    assert rs.rows == [[""]]  # NULL renders as empty string in CLI output


def test_sum_all_null_returns_null(shop):
    """SUM with no non-NULL inputs returns NULL (zero contributions)."""
    shop.execute("CREATE TABLE nullable (val INT)")
    shop.execute("INSERT INTO nullable VALUES (NULL)")
    shop.execute("INSERT INTO nullable VALUES (NULL)")
    rs = shop.execute("SELECT SUM(val) FROM nullable")
    assert rs.rows == [[""]]


def test_sum_mixed_null_ignores_null_only(shop):
    """SUM ignores NULL operands; 1 + NULL + 3 == 4, not NULL."""
    shop.execute("CREATE TABLE mixed (val INT)")
    shop.execute("INSERT INTO mixed VALUES (1)")
    shop.execute("INSERT INTO mixed VALUES (NULL)")
    shop.execute("INSERT INTO mixed VALUES (3)")
    rs = shop.execute("SELECT SUM(val) FROM mixed")
    assert rs.rows == [["4"]]

def test_sum_empty_with_filter_returns_null(shop):
    """A WHERE filter that selects no rows still yields one output row
    for SUM — but with NULL, not 0. Distinguishes a missing measurement
    from a real zero.
    """
    shop.execute("CREATE TABLE t (val INT)")
    shop.execute("INSERT INTO t VALUES (1)")
    shop.execute("INSERT INTO t VALUES (2)")
    rs = shop.execute("SELECT SUM(val) FROM t WHERE val > 100")
    assert rs.rows == [[""]]

def test_sum_float_inputs(shop):
    """SUM must accept FLOAT values; result type and value are correct."""
    shop.execute("CREATE TABLE f (val FLOAT)")
    shop.execute("INSERT INTO f VALUES (1.5)")
    shop.execute("INSERT INTO f VALUES (2.25)")
    rs = shop.execute("SELECT SUM(val) FROM f")
    assert float(rs.rows[0][0]) == pytest.approx(3.75)

def test_sum_of_zero_returns_zero_not_null(shop):
    """With a non-NULL operand that contributes zero, SUM must be 0, not NULL.
    Distinguishes the empty-input NULL case from a real zero sum.
    """
    shop.execute("CREATE TABLE z (val INT)")
    shop.execute("INSERT INTO z VALUES (0)")
    rs = shop.execute("SELECT SUM(val) FROM z")
    assert rs.rows == [["0"]]

def test_sum_grouped_all_null_returns_null(shop):
    """For a GROUP BY group whose SUM inputs are all NULL, the per-group
    SUM value is NULL — distinct from COUNT, which is still 0.
    """
    shop.execute("CREATE TABLE g (g TEXT, val INT)")
    shop.execute("INSERT INTO g VALUES ('a', NULL)")
    shop.execute("INSERT INTO g VALUES ('a', NULL)")
    shop.execute("INSERT INTO g VALUES ('b', 5)")
    rs = shop.execute(
        "SELECT g, SUM(val), COUNT(val) FROM g GROUP BY g ORDER BY g"
    )
    assert rs.rows == [
        ["a", "", "0"],
        ["b", "5", "1"],
    ]

def test_sum_large_int_preserves_precision(shop):
    """SUM of INTs must stay in the integer domain. Accumulating in float
    loses precision above 2^53, so 9007199254740993 would round to
    9007199254740992.
    """
    shop.execute("CREATE TABLE big (val INT)")
    shop.execute("INSERT INTO big VALUES (9007199254740993)")
    rs = shop.execute("SELECT SUM(val) FROM big")
    assert rs.rows == [["9007199254740993"]]


def test_sum_many_large_ints_stay_exact(shop):
    """Summing several values past 2^53 must be exact, not float-rounded."""
    shop.execute("CREATE TABLE big (val INT)")
    vals = [9007199254740993, 9007199254740995, 1, 1]
    for v in vals:
        shop.execute(f"INSERT INTO big VALUES ({v})")
    rs = shop.execute("SELECT SUM(val) FROM big")
    assert rs.rows == [[str(sum(vals))]]


def test_sum_grouped_large_ints_preserve_precision(shop):
    """Per-group SUM of large INTs must stay exact within each group."""
    shop.execute("CREATE TABLE g (grp TEXT, val INT)")
    shop.execute("INSERT INTO g VALUES ('a', 9007199254740993)")
    shop.execute("INSERT INTO g VALUES ('a', 1)")
    shop.execute("INSERT INTO g VALUES ('b', 9007199254740995)")
    rs = shop.execute("SELECT grp, SUM(val) FROM g GROUP BY grp ORDER BY grp")
    assert rs.rows == [
        ["a", str(9007199254740993 + 1)],
        ["b", "9007199254740995"],
    ]


def test_avg_empty_returns_null_regression(shop):
    """Regression: AVG of empty input must be NULL (already worked, but
    covered here alongside SUM for parity).
    """
    shop.execute("CREATE TABLE t (val INT)")
    rs = shop.execute("SELECT AVG(val) FROM t")
    assert rs.rows == [[""]]


def test_group_by_empty_input_zero_rows(shop):
    """GROUP BY over no input rows must return zero rows, not one NULL row.

    SQL standard: ``SELECT a, COUNT(*) FROM empty GROUP BY a`` returns 0
    rows (the empty input has no groups). Previously the executor
    synthesized an empty ``()`` group when no rows arrived, so a query
    against an empty table returned one row of (NULL, 0).
    """
    shop.execute("CREATE TABLE empty_t (a INT)")
    rs = shop.execute("SELECT a, COUNT(*) FROM empty_t GROUP BY a")
    assert rs.rows == [], (
        f"GROUP BY empty input should yield 0 rows, got {rs.rows}"
    )


def test_group_by_no_matching_groups_zero_rows(shop):
    """GROUP BY where the WHERE filter excludes everything returns 0 rows.

    Same SQL semantic: zero surviving rows means zero groups.
    """
    shop.execute("CREATE TABLE t (g TEXT, v INT)")
    shop.execute("INSERT INTO t VALUES ('a', 1)")
    shop.execute("INSERT INTO t VALUES ('b', 2)")
    rs = shop.execute("SELECT g, COUNT(*) FROM t WHERE v > 100 GROUP BY g")
    assert rs.rows == [], (
        f"filtered GROUP BY with no matches should yield 0 rows, got {rs.rows}"
    )


def test_aggregate_no_group_by_empty_returns_one_row(shop):
    """A scalar aggregate (no GROUP BY) over empty input still returns
    ONE row — distinguishing GROUP BY (0 rows for empty) from a bare
    aggregate (1 row, NULL for SUM/AVG).
    """
    shop.execute("CREATE TABLE empty_t (val INT)")
    rs = shop.execute("SELECT COUNT(*) FROM empty_t")
    assert rs.rows == [["0"]]
    rs = shop.execute("SELECT SUM(val) FROM empty_t")
    assert rs.rows == [[""]]  # NULL for empty SUM
