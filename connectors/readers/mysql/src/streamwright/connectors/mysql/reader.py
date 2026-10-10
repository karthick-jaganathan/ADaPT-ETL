# /*************************************************************************
# * Copyright 2026 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

"""
Reading a MySQL database as records on a DuckDB connection of the `mysql` connector's own.
"""

import decimal
import json
import re
import shutil
import tempfile
import weakref
from urllib.parse import unquote, urlsplit

from streamwright.core.net.downloads import PAGE_SIZE
from streamwright.core.outputs.staging import sql_string

__all__ = ["QueryError", "ConnectError", "Database", "PAGE_SIZE", "LIMITS", "ALIAS", "ATTACH_TYPES",
           "WRITE_KEYWORDS", "TABLE_FUNCTIONS", "OPERATORS", "SYSTEM_SCHEMAS", "SYSTEM_PREFIXES", "SECRET",
           "query_problems", "check_query", "query_parameters",
           "table_query", "where_problems", "identifier_problem", "quote_identifier", "type_problem", "record_query",
           "dsn_secrets", "timeout_ms", "decode_record", "message",
           "DEFAULT_PORT", "SSLMODES", "CONNECTION_OPTIONS", "conninfo_value", "conninfo", "structured_conninfo"]

LIMITS = {"memory_limit": "1GB", "threads": 1}
ALIAS = "src"
ATTACH_TYPES = {"mysql": "mysql", "duckdb": None, "sqlite": "sqlite"}
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "COPY", "CREATE", "DROP", "ALTER", "GRANT",
                  "REVOKE", "TRUNCATE", "CALL", "EXECUTE", "PREPARE", "DEALLOCATE", "ATTACH", "DETACH", "INSTALL",
                  "VACUUM", "CHECKPOINT", "PRAGMA", "REINDEX", "REFRESH", "LOCK")
STARTS = ("SELECT", "WITH", "FROM", "VALUES")
TABLE_FUNCTIONS = ("unnest", "range", "generate_series")
_DENIED_FUNCTIONS = ("query", "query_table", "getenv", "glob", "current_setting")
_DENIED_PREFIXES = ("postgres_", "sqlite_", "mysql_", "read_", "parquet_", "iceberg_", "delta_",
                    "duckdb_", "ducklake_", "pragma_")
SYSTEM_SCHEMAS = ("system", "temp", "memory", "information_schema", "mysql", "performance_schema", "sys")
SYSTEM_PREFIXES = ("duckdb_", "pragma_", "sqlite_")
SECRET = "streamwright_src"
OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "IS NULL", "IS NOT NULL", "LIKE")
_NO_VALUE = ("IS NULL", "IS NOT NULL")
_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9 ,]+\))?$")
_WRITE = re.compile(r"\b(%s)\b" % "|".join(WRITE_KEYWORDS), re.IGNORECASE)
_TIMEOUT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|min|h)?\s*$")
_TIMEOUT_UNITS = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000, None: 1000}
_URL_DSN = re.compile(r"^mysql://", re.IGNORECASE)
_KEYWORD_PASSWORD = re.compile(r"(?:^|\s)password\s*=\s*('(?:[^'\\]|\\.)*'|[^\s']+)", re.IGNORECASE)
_CONNINFO_KEYWORD = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_PORT = 3306
SSLMODES = ("disabled", "preferred", "required", "verify-ca", "verify-identity")
CONNECTION_OPTIONS = ("connect_timeout", "compress", "local_infile", "charset")


class QueryError(Exception):
    pass


class ConnectError(Exception):
    pass


def _statement(text):
    clean = re.sub(r"--[^\n]*", "", text)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL).strip()
    match = _WRITE.search(clean)
    if match:
        return text, None, [f"the query holds write keyword {match.group(1)!r}"]
    return text.strip().rstrip(";"), None, []


def _parse(statement):
    import duckdb
    try:
        con = duckdb.connect(":memory:", config={"enable_external_access": False})
        try:
            raw = con.execute("SELECT json_serialize_sql(?)", [statement]).fetchone()[0]
            return json.loads(raw)
        finally:
            con.close()
    except Exception as exc:
        return {"error": True, "error_message": str(exc)}


def check_query(text, params=None):
    if not isinstance(text, str) or not text.strip():
        return text, ["`query` must be a SELECT statement (a non-empty text)"]
    if "{{" in text:
        return text, ["the query cannot hold references ({{ ... }}): pass values in `params` and write $name in the query"]
    if "\x00" in text:
        return text, ["the query has a NUL character"]
    statement, _, problems = _statement(text)
    if problems:
        return statement, problems
    tree = _parse(statement)
    if tree.get("error"):
        detail = tree.get("error_message") or "it cannot be parsed"
        return statement, [f"the query must be a single SELECT: {detail}"]
    return statement, []


def query_parameters(text):
    return re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", text)


def record_query(query):
    return f"SELECT to_json(t) AS record FROM (\n{query}\n) t"


def identifier_problem(name):
    if not isinstance(name, str) or not name.strip():
        return "is not a name (a non-empty text)"
    if "\x00" in name:
        return "has a NUL character"
    if len(name.encode("utf-8")) > 64:
        return "is longer than 64 bytes"
    return None


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def type_problem(kind):
    if isinstance(kind, str) and _TYPE.match(kind.strip()):
        return None
    return f"{kind!r} is not a column type (e.g. BIGINT, DATE, TIMESTAMP, DECIMAL(12,2), VARCHAR)"


def where_problems(where):
    if where is None:
        return []
    if not isinstance(where, list):
        return ["`where` must be a list of conditions, e.g. [{column: id, op: '=', value: 3}]"]
    problems = []
    for index, item in enumerate(where):
        at = f"`where`[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{at} must be a mapping {{column, op, value, type}}")
            continue
        col = item.get("column")
        if not col or identifier_problem(col):
            problems.append(f"{at}.column is not a valid column name")
        op = item.get("op", "=").upper()
        if op not in OPERATORS:
            problems.append(f"{at}.op {op!r} is not supported")
    return problems


def table_query(schema, table, columns=None, where=None):
    tbl = f"{quote_identifier(schema)}.{quote_identifier(table)}" if schema else quote_identifier(table)
    cols = ", ".join(quote_identifier(c) for c in columns) if columns else "*"
    clauses = []
    params = {}
    for i, cond in enumerate(where or []):
        col_name = quote_identifier(cond["column"])
        op = cond.get("op", "=").upper()
        if op in _NO_VALUE:
            clauses.append(f"{col_name} {op}")
        else:
            param_key = f"p_{i}"
            params[param_key] = cond.get("value")
            clauses.append(f"{col_name} {op} ${param_key}")
    where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return f"SELECT {cols} FROM {tbl}{where_clause}", params


def dsn_secrets(dsn):
    found = []
    if _URL_DSN.match(dsn):
        try:
            parts = urlsplit(dsn)
            password = parts.password
            if password:
                found += [password, unquote(password)]
        except Exception:
            pass
    for match in _KEYWORD_PASSWORD.finditer(dsn):
        val = match.group(1).strip("'\"")
        found.append(val)
    return [v for v in dict.fromkeys(found) if v]


def timeout_ms(value):
    if isinstance(value, bool):
        raise ValueError("expected a number of seconds or a text such as 30s, 500ms, 5min")
    if isinstance(value, (int, float, decimal.Decimal)):
        ms = float(value) * 1000
    else:
        match = _TIMEOUT.match(str(value))
        if not match:
            raise ValueError(f"{value!r} is not a valid timeout")
        ms = float(match.group(1)) * _TIMEOUT_UNITS[match.group(2)]
    return int(round(ms))


def conninfo_value(value, key=None):
    if key == "port" or isinstance(value, int):
        return str(int(value))
    s = str(value)
    if any(c in s for c in (" ", "\t", "'", '"', "\\")):
        return f"'{s.replace(chr(92), chr(92)+chr(92)).replace(chr(39), chr(92)+chr(39))}'"
    return s


def conninfo(parameters):
    pairs = []
    for k, v in parameters:
        pairs.append(f"{k}={conninfo_value(v, key=k)}")
    return " ".join(pairs)


def structured_conninfo(host, database, user, port=DEFAULT_PORT, password=None, sslmode=None, options=None):
    pairs = [("host", host), ("port", DEFAULT_PORT if port in (None, "") else port),
             ("database", database), ("user", user)]
    if password is not None:
        pairs.append(("password", password))
    if sslmode:
        pairs.append(("sslmode", sslmode))
    for k, v in (options or {}).items():
        pairs.append((k, v))
    return conninfo(pairs)


def decode_record(text):
    return None if text is None else json.loads(text, parse_float=decimal.Decimal)


def message(exc):
    text = str(exc).strip().split("\n\n")[0]
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("LINE ")]
    return " ".join(lines) or type(exc).__name__


def _cleanup(connection, folder):
    try:
        connection.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Database(object):
    """
    DuckDB connection managing a MySQL database attachment.
    """

    def __init__(self, dsn, attach_type="mysql", limits=None, page_size=PAGE_SIZE):
        import duckdb
        if attach_type not in ATTACH_TYPES:
            raise ConnectError(f"unknown attach type {attach_type!r} (types: {', '.join(ATTACH_TYPES)})")
        self.duckdb = duckdb
        self.attach_type = attach_type
        self.page_size = page_size
        self.folder = tempfile.mkdtemp(prefix="streamwright-mysql-")
        try:
            config = dict(limits or LIMITS, autoinstall_known_extensions=False, autoload_known_extensions=False,
                          python_enable_replacements=False, temp_directory=self.folder)
            self.connection = duckdb.connect(":memory:", config=config)
        except Exception:
            shutil.rmtree(self.folder, ignore_errors=True)
            raise

        self._finalizer = weakref.finalize(self, _cleanup, self.connection, self.folder)

        try:
            self.connection.execute("SET TimeZone = 'UTC'")
            extension = ATTACH_TYPES[attach_type]
            if extension:
                try:
                    installed = self.connection.execute(
                        f"SELECT installed FROM duckdb_extensions() WHERE extension_name = '{extension}'"
                    ).fetchone()
                    if not (installed and installed[0]):
                        self.connection.execute(f"INSTALL {extension}")
                    self.connection.execute(f"LOAD {extension}")
                except duckdb.Error as exc:
                    raise ConnectError(f"cannot load DuckDB's {extension} extension: {message(exc)}")

            try:
                if attach_type == "mysql":
                    self.connection.execute(f"ATTACH {sql_string(dsn)} AS {ALIAS} (TYPE mysql, READ_ONLY)")
                else:
                    self.connection.execute(f"ATTACH {sql_string(dsn)} AS {ALIAS} (TYPE {attach_type}, READ_ONLY)")
            except duckdb.Error as exc:
                raise ConnectError(f"cannot attach the database: {message(exc)}")

            self.connection.execute(f"USE {ALIAS}")
            self.connection.execute("SET enable_external_access = false")
            self.connection.execute("SET lock_configuration = true")
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._finalizer()

    @property
    def closed(self):
        return not self._finalizer.alive

    def pages(self, query, parameters=None, page_size=None):
        if self.closed:
            raise QueryError("the database connection is closed")
        statement, problems = check_query(query, parameters or {})
        if problems:
            raise QueryError("; ".join(problems))
        size = page_size or self.page_size
        result = self.connection.execute(record_query(statement), dict(parameters or {}))
        while True:
            rows = result.fetchmany(size)
            if not rows:
                return
            yield [decode_record(row[0]) for row in rows]
