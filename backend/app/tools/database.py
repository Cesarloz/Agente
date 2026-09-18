"""Read-only SQL execution behind the SQLGuard boundary."""

import asyncio
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
import sqlglot
from sqlglot import exp

from .sql_guard import SQLGuard, SQLGuardError


class DatabaseToolError(RuntimeError):
    """Sanitized errors safe for the API and runtime."""


class DatabaseConnector:
    """Sync SQLAlchemy drivers run on a worker thread; connections never escape it.

    Production credentials MUST belong to dedicated SELECT-only users. SQL Server
    has no general SET TRANSACTION READ ONLY; that backend requires an explicitly
    provisioned least-privilege login in addition to AST validation and timeouts.
    """

    def __init__(self, timeout_seconds: int = 10, guard: SQLGuard | None = None):
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("El timeout debe estar entre 1 y 60 segundos.")
        self.timeout_seconds = timeout_seconds
        self.guard = guard or SQLGuard()

    @staticmethod
    def _dialect(dsn: str) -> str:
        try:
            dialect = make_url(dsn).get_backend_name()
            if dialect not in {"postgresql", "mysql", "mssql", "sqlite"}:
                raise ValueError
            return dialect
        except Exception:
            raise DatabaseToolError("La conexión utiliza un motor o formato no soportado.") from None

    def _engine(self, dsn: str) -> Engine:
        try:
            url = make_url(dsn)
            dialect = self._dialect(dsn)
            timeout = self.timeout_seconds
            if dialect == "sqlite":
                # Only existing local files; force URI mode=ro and discard unsafe DSN options.
                if url.host or not url.database or url.database == ":memory:":
                    raise ValueError
                raw_path = url.database
                if raw_path.startswith("file:"):
                    raw_path = unquote(raw_path[5:].split("?", 1)[0])
                path = Path(raw_path).resolve(strict=True)
                if not path.is_file():
                    raise ValueError
                uri = path.as_uri() + "?mode=ro"
                def connect_sqlite():
                    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
                    connection.execute("PRAGMA query_only = ON")
                    deadline = time.monotonic() + timeout
                    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                    return connection
                return create_engine("sqlite://", creator=connect_sqlite, poolclass=NullPool, hide_parameters=True)
            if dialect == "postgresql":
                url = url.set(drivername="postgresql+psycopg")
                args = {"connect_timeout": timeout}
            elif dialect == "mysql":
                url = url.set(drivername="mysql+pymysql")
                args = {"connect_timeout": timeout, "read_timeout": timeout, "write_timeout": timeout, "local_infile": False}
            else:
                url = url.set(drivername="mssql+pymssql")
                args = {"timeout": timeout, "login_timeout": timeout}
            engine = create_engine(url, connect_args=args, poolclass=NullPool, hide_parameters=True)
            return engine
        except DatabaseToolError:
            raise
        except Exception:
            raise DatabaseToolError("No se pudo preparar la conexión; revisa su configuración.") from None

    def _run(self, dsn: str, sql: str, max_rows: int) -> list[dict[str, Any]]:
        engine = self._engine(dsn)
        dialect = self._dialect(dsn)
        try:
            with engine.connect() as connection:
                with connection.begin():
                    if dialect == "postgresql":
                        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
                        connection.exec_driver_sql("SET LOCAL lock_timeout = 2000")
                    elif dialect == "mysql":
                        # Applies to the next transaction. SQLAlchemy autobegin does not
                        # issue BEGIN with PyMySQL until the first transactional statement.
                        connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {self.timeout_seconds * 1000}")
                        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    elif dialect == "mssql":
                        connection.exec_driver_sql("SET LOCK_TIMEOUT 2000")
                    result = connection.exec_driver_sql(sql)
                    if not result.returns_rows:
                        raise DatabaseToolError("La consulta no devolvió un conjunto de resultados.")
                    rows = [dict(row) for row in result.mappings().fetchmany(max_rows)]
                    result.close()
                    # Explicit rollback even for SELECT; never persist a transaction.
                    connection.rollback()
                    return rows
        except DatabaseToolError:
            raise
        except Exception:
            raise DatabaseToolError("No se pudo ejecutar la consulta de lectura; revisa permisos, tablas y conexión.") from None
        finally:
            engine.dispose()

    async def query(self, dsn: str, sql: str, allowed_tables: Iterable[str], max_rows: int = 100) -> list[dict[str, Any]]:
        bounded_sql = self.guard.validate(sql, allowed_tables, max_rows, self._dialect(dsn))
        return await asyncio.to_thread(self._run, dsn, bounded_sql, max_rows)

    def _schema(self, dsn: str, allowed_tables: list[str]) -> dict[str, list[dict[str, Any]]]:
        dialect = self._dialect(dsn)
        if not allowed_tables or len(allowed_tables) > 100:
            raise DatabaseToolError("Configura entre 1 y 100 tablas para consultar su esquema.")
        targets = []
        for name in allowed_tables:
            # The same policy validates configured identifiers before any metadata call.
            self.guard.validate(f"SELECT * FROM {name}", allowed_tables, 1, dialect)
            table = sqlglot.parse_one(name, into=exp.Table, read=self.guard.DIALECTS[dialect])
            if len(table.parts) > 2:
                raise DatabaseToolError("La inspección admite tablas y esquema.tabla dentro de la base configurada.")
            targets.append((name, table.name, table.db or None))
        engine = self._engine(dsn)
        try:
            with engine.connect() as connection:
                if dialect == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    connection.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
                elif dialect == "mysql":
                    connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {self.timeout_seconds * 1000}")
                elif dialect == "mssql":
                    connection.exec_driver_sql("SET LOCK_TIMEOUT 2000")
                inspector = inspect(connection)
                result = {}
                for label, table_name, schema_name in targets:
                    columns = inspector.get_columns(table_name, schema=schema_name)
                    result[label] = [{"name": str(column["name"]), "type": str(column["type"]), "nullable": bool(column.get("nullable", True))} for column in columns[:200]]
                connection.rollback()
                return result
        except Exception:
            raise DatabaseToolError("No se pudo inspeccionar el esquema de las tablas autorizadas.") from None
        finally:
            engine.dispose()

    async def schema(self, dsn: str, allowed_tables: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Metadata only for explicitly approved tables; never enumerates the database."""
        if isinstance(allowed_tables, (str, bytes)):
            raise DatabaseToolError("Configura una lista explícita de tablas permitidas.")
        return await asyncio.to_thread(self._schema, dsn, list(allowed_tables))

    async def test(self, dsn: str) -> dict[str, Any]:
        try:
            dialect = self._dialect(dsn)
            await asyncio.to_thread(self._run, dsn, "SELECT 1 AS connected", 1)
            return {"ok": True, "dialect": dialect, "read_only_enforced": dialect != "mssql",
                    "permission_requirement": "Usuario con permisos SELECT únicamente; obligatorio en SQL Server."}
        except DatabaseToolError:
            return {"ok": False, "error": "No fue posible conectar. Revisa credenciales, red, driver y permisos de lectura."}


class DatabaseQueryTool:
    """Only entry point offered to the AgentRuntime; no unrestricted engine exposed."""

    def __init__(self, connector: DatabaseConnector | None = None, guard: SQLGuard | None = None):
        self.connector = connector or DatabaseConnector(guard=guard)

    async def execute(self, dsn: str, sql: str, allowed_tables: Iterable[str], max_rows: int = 100) -> dict[str, Any]:
        try:
            rows = await self.connector.query(dsn, sql, allowed_tables, max_rows)
            return {"ok": True, "rows": rows, "row_count": len(rows), "max_rows": max_rows}
        except (SQLGuardError, DatabaseToolError) as error:
            return {"ok": False, "rows": [], "row_count": 0, "error": str(error)}
