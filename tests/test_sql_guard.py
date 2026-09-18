import sqlite3

import pytest

from app.tools import DatabaseConnector, DatabaseQueryTool, SQLGuard, SQLGuardError


@pytest.fixture
def guard():
    return SQLGuard()


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "SELECT * FROM orders; DROP TABLE orders", "SELECT * INTO stolen FROM orders",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "SELECT * FROM orders FOR UPDATE",
    "SELECT pg_read_file('/etc/passwd')", "SELECT pg_sleep(100)", "SELECT load_extension('bad')",
    "SELECT * FROM information_schema.tables", "SELECT * FROM pg_catalog.pg_user", "SELECT * FROM private.orders",
    "SELECT * FROM orders UNION SELECT * FROM credentials", "SELECT * FROM orders WHERE id IN (SELECT id FROM secrets)",
    "SELECT malicious_function(id) FROM orders", "SELECT public.lower(id) FROM orders",
    "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
    "SELECT * FROM orders LIMIT $1", "SELECT * FROM orders LIMIT 100; SELECT 1",
    "SELECT * FROM orders WHERE EXISTS (WITH secrets AS (SELECT * FROM orders) SELECT * FROM secrets) UNION SELECT * FROM secrets",
])
def test_rejects_writes_exfiltration_and_cte_shadow(guard, sql):
    with pytest.raises(SQLGuardError):
        guard.validate(sql, ["orders"])


@pytest.mark.parametrize("dialect,sql", [
    ("mysql", "SELECT load_file('/etc/passwd') FROM orders"),
    ("mysql", "SELECT * FROM orders INTO OUTFILE '/tmp/stolen'"),
    ("mysql", "SELECT @x := id FROM orders"),
    ("mssql", "SELECT * FROM OPENROWSET('provider', 'connection', 'query')"),
    ("mssql", "SELECT TOP 10 PERCENT * FROM orders"),
    ("sqlite", "SELECT * FROM sqlite_master"),
    ("sqlite", "PRAGMA writable_schema=ON"),
])
def test_dialect_specific_escape_rejected(guard, dialect, sql):
    with pytest.raises(SQLGuardError):
        guard.validate(sql, ["orders"], dialect=dialect)


def test_readonly_cte_and_aggregation_are_bounded(guard):
    actual = guard.validate("WITH recent AS (SELECT id FROM orders) SELECT COUNT(*) AS total FROM recent", ["orders"], max_rows=25)
    assert "LIMIT 25" in actual
    assert "COUNT(*)" in actual


@pytest.mark.parametrize("sql", [
    "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.id = o.id)",
    "SELECT COUNT(CASE WHEN total > 5 THEN 1 ELSE NULL END) FROM orders",
    "SELECT LOWER(CAST(id AS TEXT)) FROM orders",
    "SELECT id FROM orders UNION SELECT id FROM orders",
])
def test_standard_readonly_queries_remain_supported(guard, sql):
    assert "LIMIT 10" in guard.validate(sql, ["orders"], 10)


def test_cte_alias_cannot_authorize_a_physical_table(guard):
    with pytest.raises(SQLGuardError):
        guard.validate("WITH trusted AS (SELECT id FROM orders) SELECT * FROM secrets AS trusted", ["orders"])


def test_sql_server_generates_top_cap(guard):
    assert "TOP 12" in guard.validate("SELECT * FROM orders", ["orders"], 12, "mssql")


def test_qualified_tables_are_exact(guard):
    assert "public.orders" in guard.validate("SELECT * FROM public.orders", ["public.orders"])
    with pytest.raises(SQLGuardError):
        guard.validate("SELECT * FROM private.orders", ["public.orders"])


def test_quoted_table_case_not_granted_by_casefold(guard):
    with pytest.raises(SQLGuardError):
        guard.validate('SELECT * FROM "Orders"', ["orders"])


def test_cap_does_not_expand_explicit_smaller_limit(guard):
    assert "LIMIT 3" in guard.validate("SELECT * FROM orders LIMIT 3", ["orders"], max_rows=20)
    assert "LIMIT 20" in guard.validate("SELECT * FROM orders LIMIT 9999", ["orders"], max_rows=20)


def test_allowlist_required_even_for_constant_select(guard):
    with pytest.raises(SQLGuardError):
        guard.validate("SELECT 1", [])


@pytest.mark.parametrize("cap", [0, -1, 1001, True, "10"])
def test_invalid_cap(guard, cap):
    with pytest.raises(SQLGuardError):
        guard.validate("SELECT * FROM orders", ["orders"], max_rows=cap)


@pytest.fixture
def sqlite_dsn(tmp_path):
    path = tmp_path / "business.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER, total REAL)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", [(n, n * 1.5) for n in range(20)])
        connection.execute("CREATE TABLE secrets (secret TEXT)")
    return f"sqlite:///{path.as_posix()}"


async def test_sqlite_executes_guarded_bounded_query(sqlite_dsn):
    connector = DatabaseConnector()
    rows = await connector.query(sqlite_dsn, "SELECT * FROM orders ORDER BY id", ["orders"], max_rows=4)
    assert rows == [{"id": n, "total": n * 1.5} for n in range(4)]
    assert (await connector.test(sqlite_dsn))["ok"]


async def test_schema_only_lists_approved_tables(sqlite_dsn):
    schema = await DatabaseConnector().schema(sqlite_dsn, ["orders"])
    assert list(schema) == ["orders"]
    assert [column["name"] for column in schema["orders"]] == ["id", "total"]
    assert "secrets" not in schema


async def test_schema_rejects_system_and_sql_injection(sqlite_dsn):
    with pytest.raises(SQLGuardError):
        await DatabaseConnector().schema(sqlite_dsn, ["sqlite_master"])
    with pytest.raises(SQLGuardError):
        await DatabaseConnector().schema(sqlite_dsn, ["orders; DROP TABLE orders"])


async def test_tool_rejects_write_without_touching_database(sqlite_dsn):
    tool = DatabaseQueryTool()
    result = await tool.execute(sqlite_dsn, "DELETE FROM orders", ["orders"], 10)
    assert not result["ok"]
    after = await tool.execute(sqlite_dsn, "SELECT COUNT(*) AS total FROM orders", ["orders"], 10)
    assert after["rows"] == [{"total": 20}]


def test_sqlite_driver_itself_is_readonly(sqlite_dsn):
    connector = DatabaseConnector()
    engine = connector._engine(sqlite_dsn)
    try:
        with engine.connect() as connection:
            with pytest.raises(Exception):
                connection.exec_driver_sql("DELETE FROM orders")
    finally:
        engine.dispose()


async def test_missing_sqlite_is_not_created(tmp_path):
    path = tmp_path / "must-not-exist.sqlite3"
    result = await DatabaseConnector().test(f"sqlite:///{path.as_posix()}")
    assert not result["ok"]
    assert not path.exists()


async def test_database_errors_are_sanitized(sqlite_dsn):
    result = await DatabaseQueryTool().execute(sqlite_dsn, "SELECT highly_secret_column FROM orders", ["orders"], 10)
    assert not result["ok"]
    assert "highly_secret_column" not in result["error"]
    assert sqlite_dsn not in result["error"]
