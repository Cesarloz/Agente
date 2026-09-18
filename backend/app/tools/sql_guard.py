"""Parse SQL before execution; never trust a model's SQL as authorization."""

from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


class SQLGuardError(ValueError):
    """A safe public error containing no SQL, DSN, or database details."""


class SQLGuard:
    MAX_ROWS = 1000
    MAX_SQL_LENGTH = 20_000
    DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite", "mssql": "tsql", "tsql": "tsql"}
    FORBIDDEN_NODES = {
        "insert", "update", "delete", "merge", "create", "drop", "alter", "truncate", "truncatetable",
        "command", "transaction", "commit", "rollback", "grant", "revoke", "copy", "load", "loaddata",
        "into", "lock", "execute", "exec", "call", "set", "setitem", "pragma", "attach", "detach",
        "use", "analyze", "vacuum", "cache", "uncache", "export", "import", "kill", "refresh",
        "parameter", "placeholder", "sessionparameter", "property", "userdefinedfunction",
        "nextvaluefor", "hint", "queryoption", "withtablehint", "tablesample", "lateral", "pivot", "unpivot",
    }
    SAFE_FUNCTIONS = frozenset({
        "ABS", "AVG", "CEIL", "CEILING", "FLOOR", "ROUND", "POWER", "SQRT", "MOD", "SIGN",
        "COUNT", "SUM", "MIN", "MAX", "COALESCE", "NULLIF", "IF", "IIF", "IFNULL", "ISNULL", "CASE", "EXISTS",
        "LOWER", "UPPER", "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "TRIM", "LTRIM", "RTRIM",
        "CONCAT", "CONCAT_WS", "SUBSTRING", "SUBSTR", "REPLACE", "LEFT", "RIGHT", "STR_POSITION",
        "CAST", "TRY_CAST", "EXTRACT", "DATE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "YEAR", "MONTH", "DAY",
        "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "NOW", "GETDATE", "DATEDIFF", "DATE_DIFF",
        "DATE_ADD", "DATE_SUB", "DATEADD", "STRFTIME", "DATETIME", "TIME", "JULIANDAY", "UNIXEPOCH",
        "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
        "BOOL_AND", "BOOL_OR", "EVERY", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP",
        "VAR_SAMP", "GROUP_CONCAT", "STRING_AGG", "ARRAY_AGG", "JSON_EXTRACT", "JSON_EXTRACT_SCALAR",
    })
    SYSTEM_NAMES = frozenset({"pg_catalog", "information_schema", "mysql", "performance_schema", "sys", "msdb", "master", "tempdb"})

    @staticmethod
    def _parts(table: exp.Table) -> tuple[str, ...]:
        if not isinstance(table.this, exp.Identifier):
            raise SQLGuardError("Las funciones o expresiones como origen de tablas no están permitidas.")
        # Preserve quoted case; unquoted SQL identifiers are compared case-insensitively.
        return tuple(part.name if part.args.get("quoted") else part.name.lower() for part in table.parts)

    @classmethod
    def _system_table(cls, parts: tuple[str, ...]) -> bool:
        return any(p.lower() in cls.SYSTEM_NAMES or p.lower().startswith(("sqlite_", "pg_")) for p in parts)

    def validate(self, sql: str, allowed_tables: Iterable[str], max_rows: int = 100, dialect: str = "postgresql") -> str:
        """Return normalized SQL with a literal result cap, or reject it entirely.

        Table permissions are exact: ``orders`` does not authorize ``private.orders``.
        CTE references are resolved per lexical scope, preventing alias shadow bypasses.
        """
        dialect = self.DIALECTS.get(dialect, "")
        if not dialect:
            raise SQLGuardError("Motor SQL no soportado.")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= self.MAX_ROWS:
            raise SQLGuardError("El límite debe estar entre 1 y 1000 filas.")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > self.MAX_SQL_LENGTH:
            raise SQLGuardError("La consulta SQL está vacía o es demasiado extensa.")
        if isinstance(allowed_tables, (str, bytes)):
            raise SQLGuardError("Configura una lista explícita de tablas permitidas.")
        try:
            permitted = set()
            for name in allowed_tables:
                table = sqlglot.parse_one(name, read=dialect, into=exp.Table, error_level=sqlglot.ErrorLevel.RAISE)
                if table.alias or table.args.get("joins"):
                    raise SQLGuardError("La lista de tablas contiene un identificador no válido.")
                parts = self._parts(table)
                if self._system_table(parts):
                    raise SQLGuardError("Los catálogos del sistema no se pueden autorizar.")
                permitted.add(parts)
            if not permitted:
                raise SQLGuardError("Configura al menos una tabla permitida.")
            statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
            if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                raise SQLGuardError("Solo se permite una consulta SELECT de lectura.")
            tree = statements[0]
            nodes = list(tree.walk())
            if len(nodes) > 2000:
                raise SQLGuardError("La consulta SQL es demasiado compleja.")
            for node in nodes:
                if node.key.lower() in self.FORBIDDEN_NODES:
                    raise SQLGuardError("La consulta contiene operaciones no permitidas.")
                if isinstance(node, exp.With) and node.args.get("recursive"):
                    raise SQLGuardError("Las consultas recursivas no están habilitadas.")
                if isinstance(node, exp.Table) and self._system_table(self._parts(node)):
                    raise SQLGuardError("El acceso a catálogos del sistema está deshabilitado.")
                if isinstance(node, exp.Func):
                    name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
                    if name not in self.SAFE_FUNCTIONS or isinstance(node.parent, exp.Dot):
                        raise SQLGuardError("La consulta utiliza una función no autorizada.")
                if isinstance(node, exp.Limit) and node.args.get("limit_options"):
                    raise SQLGuardError("Los límites porcentuales o WITH TIES no están permitidos.")
            scopes = traverse_scope(tree)
            if not scopes:
                raise SQLGuardError("No se pudo validar el alcance de la consulta.")
            for scope in scopes:
                for table in scope.tables:
                    source = scope.sources.get(table.alias_or_name)
                    if isinstance(source, Scope):
                        continue  # This reference resolves to a validated CTE in this scope.
                    if self._parts(table) not in permitted:
                        raise SQLGuardError("La consulta accede a una tabla no autorizada.")
            limit = tree.args.get("limit")
            if limit:
                value = limit.expression
                if not isinstance(value, exp.Literal) or not value.is_int:
                    raise SQLGuardError("El límite de filas debe ser un entero literal.")
                max_rows = min(max_rows, max(0, int(value.this)))
            bounded = tree.limit(max_rows, copy=True)
            return bounded.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
        except SQLGuardError:
            raise
        except Exception:
            raise SQLGuardError("No se pudo validar la consulta SQL.") from None
