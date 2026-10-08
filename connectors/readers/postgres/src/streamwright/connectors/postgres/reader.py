#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
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
Reading a PostgreSQL database as records on a DuckDB connection of the `postgres` connector's own (never the run's
transform sandbox).

- query_problems() / check_query(): a source's query must be ONE read-only SELECT. Checked twice before it runs: its
  text (no `;` between statements, no write keyword such as INSERT, DELETE, COPY, CREATE, DROP, ..., references and
  positional parameters refused) and DuckDB's parse of it (a single SELECT statement; no table functions but unnest,
  range and generate_series; no function that runs text of its own, e.g. postgres_execute or postgres_query; no
  file names as tables; only the database's own tables: no DuckDB system view or schema - duckdb_databases,
  pragma_database_list, system.*, pg_catalog.*, information_schema.*, ... - and no SHOW, DESCRIBE or SUMMARIZE).
- table_query(): the SELECT of a `table` request, its identifiers quoted and its `where` values bound.
- Database: the DuckDB connection. It attaches the database READ_ONLY as `src` (DuckDB then refuses every write, and
  the postgres extension reads in READ ONLY transactions), makes `src` the default catalog, and then turns external
  access off and locks its configuration. A postgres DSN goes into a temporary DuckDB secret, never the attach path
  (which DuckDB's system views show), and an unconstrained NUMERIC reads as exact text (pg_numeric_as_varchar), not
  a DOUBLE. Database.pages() runs a checked query with BOUND parameters (DuckDB prepared parameters `$name`: the
  values never become query text), each row one JSON object (`to_json(t)`), and yields lists of records (fetchmany:
  memory stays bounded however many rows the query returns).
- dsn_secrets() / with_statement_timeout(): the passwords inside a DSN (to redact), and a DSN that sets Postgres'
  statement_timeout.
- conninfo() / structured_conninfo(): the libpq keyword/value connection string of a structured auth (host, port,
  dbname, user, password, sslmode and extra libpq parameters), every value quoted and escaped by libpq's rules, so
  no value can add keywords; it is attached like a DSN (a temporary secret).

Records are JSON: decimals stay exact (decimal.Decimal), a NUMERIC without precision is text, dates and timestamps
are text (timestamps with time zone in UTC); steps cast, e.g. `(record->>'amount')::DECIMAL(12,2)`.
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
           "dsn_secrets", "with_statement_timeout", "timeout_ms", "decode_record", "message",
           "DEFAULT_PORT", "SSLMODES", "CONNECTION_OPTIONS", "conninfo_value", "conninfo", "structured_conninfo"]

LIMITS = {"memory_limit": "1GB", "threads": 1}
ALIAS = "src"  # the attached database's name in DuckDB
# attach TYPE -> the DuckDB extension it needs (None: built in). Only the connector's constructor picks one: sources
# always attach postgres; the others are stand-ins for tests and embedding.
ATTACH_TYPES = {"postgres": "postgres", "duckdb": None, "sqlite": "sqlite"}
# statements a read-only query never holds (DuckDB's parse refuses every other non-SELECT statement too); a column
# named like one must be double-quoted, e.g. "update"
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "COPY", "CREATE", "DROP", "ALTER", "GRANT",
                  "REVOKE", "TRUNCATE", "CALL", "EXECUTE", "PREPARE", "DEALLOCATE", "ATTACH", "DETACH", "INSTALL",
                  "VACUUM", "CHECKPOINT", "PRAGMA", "REINDEX", "REFRESH", "LISTEN", "NOTIFY")
STARTS = ("SELECT", "WITH", "FROM", "VALUES")
TABLE_FUNCTIONS = ("unnest", "range", "generate_series")
# functions that run text of their own, read files or settings; refused anywhere in a query
_DENIED_FUNCTIONS = ("query", "query_table", "getenv", "glob", "current_setting")
_DENIED_PREFIXES = ("postgres_", "sqlite_", "mysql_", "read_", "parquet_", "iceberg_", "delta_",
                    "duckdb_", "ducklake_", "pragma_")
# DuckDB's own catalogs and schemas, and the names of its system views (duckdb_databases, pragma_database_list,
# sqlite_master, ...): they are no tables of the database, and some show its path - for Postgres, the DSN
SYSTEM_SCHEMAS = ("system", "temp", "memory", "pg_catalog", "information_schema")
SYSTEM_PREFIXES = ("duckdb_", "pragma_", "sqlite_")
SECRET = "streamwright_src"  # the DuckDB secret holding a postgres DSN (never the attach path, which queries could read)
OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "IS NULL", "IS NOT NULL")
_NO_VALUE = ("IS NULL", "IS NOT NULL")
_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9 ,]+\))?$")  # a column type, e.g. DECIMAL(12,2)
_WRITE = re.compile(r"\b(%s)\b" % "|".join(WRITE_KEYWORDS), re.IGNORECASE)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_TIMEOUT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s|min|h)?\s*$")
_TIMEOUT_UNITS = {"ms": 1, "s": 1000, "min": 60000, "h": 3600000, None: 1000}
_URL_DSN = re.compile(r"^postgres(ql)?://", re.IGNORECASE)
_KEYWORD_PASSWORD = re.compile(r"(?:^|\s)password\s*=\s*('(?:[^'\\]|\\.)*'|[^\s']+)", re.IGNORECASE)
_KEYWORD_OPTIONS = re.compile(r"(?:^|\s)options\s*=", re.IGNORECASE)
_CONNINFO_KEYWORD = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_PORT = 5432
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# the libpq connection parameters a structured auth's `options` may set (DuckDB's postgres secret takes them too):
# not those of its own keys (host, port, dbname, user, password, sslmode), no credential (passfile, sslpassword,
# oauth_client_secret, scram_*_key), no replication connection and no TLS key log file
CONNECTION_OPTIONS = ("hostaddr", "require_auth", "channel_binding", "connect_timeout", "client_encoding", "options",
                      "application_name", "fallback_application_name", "keepalives", "keepalives_idle",
                      "keepalives_interval", "keepalives_count", "tcp_user_timeout", "gssencmode", "sslnegotiation",
                      "sslcompression", "sslcert", "sslkey", "sslcertmode", "sslrootcert", "sslcrl", "sslcrldir",
                      "sslsni", "requirepeer", "ssl_min_protocol_version", "ssl_max_protocol_version",
                      "min_protocol_version", "max_protocol_version", "krbsrvname", "gsslib", "gssdelegation",
                      "service", "target_session_attrs", "load_balance_hosts", "oauth_issuer", "oauth_client_id",
                      "oauth_scope")


class QueryError(Exception):
    """A query the connector does not run: not one read-only SELECT, or parameters that do not match."""


class ConnectError(Exception):
    """The database cannot be attached (the DuckDB extension, the DSN or the server)."""


# * --------------------------
# * checking a source's query
# * --------------------------

def _scan(text):
    """
    (code, semicolons): the text with string literals blanked to '', quoted identifiers to "", dollar-quoted strings
    to '' and comments to a space - what is left is SQL code - and the positions (in `text`) of the semicolons in it.
    """
    code, semicolons = [], []
    index, size = 0, len(text)
    while index < size:
        character = text[index]
        if character == "-" and text.startswith("--", index):
            end = text.find("\n", index)
            index = size if end < 0 else end
            code.append(" ")
        elif character == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            code.append(" ")
        elif character in "'\"":
            escapes = character == "'" and index > 0 and text[index - 1] in "eE" and (
                index < 2 or not (text[index - 2].isalnum() or text[index - 2] == "_"))
            index += 1
            while index < size:
                if escapes and text[index] == "\\":
                    index += 2
                    continue
                if text[index] == character:
                    if text.startswith(character * 2, index):
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            code.append(character * 2)
        elif character == "$" and _DOLLAR_TAG.match(text, index) and (
                index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_")):
            tag = _DOLLAR_TAG.match(text, index).group(0)
            end = text.find(tag, index + len(tag))
            index = size if end < 0 else end + len(tag)
            code.append("''")
        else:
            if character == ";":
                semicolons.append(index)
            code.append(character)
            index += 1
    return "".join(code), semicolons


def _statement(text):
    """(the query without a trailing `;`, its code, problems) by the text alone."""
    code, semicolons = _scan(text)
    problems = []
    if semicolons:
        last = semicolons[-1]
        rest, _ = _scan(text[last + 1:])
        if len(semicolons) > 1 or rest.strip():
            problems.append("the query must be a single statement (a `;` separates statements)")
        else:
            text = text[:last]
            code, _ = _scan(text)
    words = sorted(set(match.upper() for match in _WRITE.findall(code)))
    if words:
        problems.append("the query is read-only: it cannot use %s (a column named like one must be double-quoted, "
                        "e.g. \"update\")" % ", ".join(words))
    first = code.strip().lstrip("(").strip().split(None, 1)
    if not first or first[0].upper() not in STARTS:
        problems.append("the query must be a SELECT (it starts with %s)" % (
            ", ".join(STARTS) if not first else repr(first[0][:20])))
    if "?" in code:
        problems.append("the query must use named parameters ($name, declared in `params`), not ?")
    return text.strip(), code, problems


def _parse(text):
    """DuckDB's parse of a query (json_serialize_sql: SELECT statements only; nothing runs), as a dict."""
    import duckdb
    connection = duckdb.connect(":memory:", config={"enable_external_access": False,
                                                     "autoinstall_known_extensions": False,
                                                     "autoload_known_extensions": False})
    try:
        return json.loads(connection.execute("SELECT json_serialize_sql(?)", [text]).fetchone()[0])
    finally:
        connection.close()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            for item in _walk(value):
                yield item
    elif isinstance(node, list):
        for value in node:
            for item in _walk(value):
                yield item


def _denied(name):
    name = name.lower()
    return name in _DENIED_FUNCTIONS or name.startswith(_DENIED_PREFIXES)


_SYSTEM_VIEWS = []


def _system_views():
    """The names of DuckDB's system views a bare `name` (or `main.name`) reaches: system.main's and pg_catalog's."""
    if not _SYSTEM_VIEWS:
        import duckdb
        connection = duckdb.connect(":memory:", config={"enable_external_access": False,
                                                         "autoinstall_known_extensions": False,
                                                         "autoload_known_extensions": False})
        try:
            _SYSTEM_VIEWS.extend(sorted(row[0] for row in connection.execute(
                "SELECT DISTINCT lower(view_name) FROM duckdb_views() WHERE internal AND database_name = 'system' "
                "AND schema_name IN ('main', 'pg_catalog')").fetchall()))
        finally:
            connection.close()
    return _SYSTEM_VIEWS


def _table_problem(catalog, schema, table):
    """Why a table reference (its catalog and schema empty when not written) is not a table of the database, or None."""
    catalog, schema, name = catalog.lower(), schema.lower(), table.lower()
    if catalog and catalog != ALIAS:
        return "the query reads the database only: %r is not it (write schema.table or table)" % (catalog,)
    if re.search(r"[/\\:.]", table):
        return "the query cannot read files: %r is not a table name" % table
    if schema in SYSTEM_SCHEMAS:  # (`system.x`: a catalog when the parser saw a schema)
        return "the query reads the database's tables only: %r is a system catalog or schema" % (schema,)
    if name.startswith(SYSTEM_PREFIXES) or (not catalog and schema in ("", "main") and name in _system_views()):
        return "the query reads the database's tables only: %r is a DuckDB system view" % (table,)
    return None


def _tree_problems(tree):
    """(problems, parameter names) from DuckDB's parse of a query."""
    problems, names, refused, table_calls = [], [], set(), set()
    for node in _walk(tree):
        kind = node.get("type")
        if kind == "TABLE_FUNCTION":
            call = node.get("function") or {}
            table_calls.add(id(call))
            name = str(call.get("function_name") or "")
            if _denied(name):
                refused.add("the query cannot call %s" % name)
            elif name.lower() not in TABLE_FUNCTIONS:
                refused.add("the query cannot call the table function %s (only %s)" % (name, ", ".join(
                    TABLE_FUNCTIONS)))
        elif kind == "BASE_TABLE":
            problem = _table_problem(str(node.get("catalog_name") or ""), str(node.get("schema_name") or ""),
                                     str(node.get("table_name") or ""))
            if problem:
                refused.add(problem)
        elif kind == "SHOW_REF":  # (SHOW, DESCRIBE, SUMMARIZE: of any table or view, system views too)
            refused.add("the query cannot use SHOW, DESCRIBE or SUMMARIZE")
        elif node.get("class") == "PARAMETER":
            names.append(str(node.get("identifier")))
        name = node.get("function_name")
        if isinstance(name, str) and id(node) not in table_calls and _denied(name):
            refused.add("the query cannot call %s" % name)
    problems += sorted(refused)
    if any(name.isdigit() for name in names):
        problems.append("the query must use named parameters ($name, declared in `params`), not $1 or ?")
    return problems, sorted(set(names))


def query_problems(text, params=None):
    """
    Why `text` is not a query the connector runs - a single read-only SELECT whose named parameters ($name) are the
    names in `params` (a mapping, or None to not compare them) - as messages; [] when it is.
    """
    return check_query(text, params)[1]


def check_query(text, params=None):
    """(the query to run: without a trailing `;`, problems) - see query_problems()."""
    if not isinstance(text, str) or not text.strip():
        return text, ["`query` must be a SELECT statement (a non-empty text)"]
    if "{{" in text:
        return text, ["the query cannot hold references ({{ ... }}): pass values in `params` and write $name in "
                      "the query, so they are bound, never pasted into it"]
    if "\x00" in text:
        return text, ["the query has a NUL character"]
    statement, _, problems = _statement(text)
    if problems:
        return statement, problems
    tree = _parse(statement)
    if tree.get("error"):
        detail = tree.get("error_message") or "it cannot be parsed"
        if "Only SELECT" in detail:
            detail = "it is not a SELECT statement"
        return statement, ["the query must be a single SELECT: %s" % detail]
    if len(tree.get("statements") or []) != 1:
        return statement, ["the query must be a single statement"]
    problems, names = _tree_problems(tree["statements"])
    if params is not None and not problems:
        declared = sorted(params) if isinstance(params, dict) else []
        missing = [name for name in names if name not in declared]
        unused = [name for name in declared if name not in names]
        if missing:
            problems.append("the query uses $%s: declare %s in `params`" % (
                ", $".join(missing), "it" if len(missing) == 1 else "them"))
        if unused:
            problems.append("`params` %s not used in the query (write $%s in it)" % (
                ", ".join(unused) + (" is" if len(unused) == 1 else " are"), ", $".join(unused)))
    return statement, problems


def query_parameters(text):
    """The named parameters ($name) of a query that passes check_query()."""
    tree = _parse(text)
    return _tree_problems(tree.get("statements") or [])[1]


def record_query(query):
    """A checked query whose rows become JSON objects, one `record` each (a comment ending it stays inside)."""
    return "SELECT to_json(t) AS record FROM (\n%s\n) t" % query


# * ----------------------
# * a `table` request
# * ----------------------

def identifier_problem(name):
    """Why `name` cannot be a schema, table or column name, or None (it is quoted, so any text but NUL works)."""
    if not isinstance(name, str) or not name.strip():
        return "is not a name (a non-empty text)"
    if "\x00" in name:
        return "has a NUL character"
    if len(name.encode("utf-8")) > 63:
        return "is longer than 63 bytes"
    return None


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def type_problem(kind):
    if isinstance(kind, str) and _TYPE.match(kind.strip()):
        return None
    return "%r is not a column type (e.g. BIGINT, DATE, TIMESTAMP, DECIMAL(12,2), VARCHAR)" % (kind,)


def where_problems(where, rendered=False):
    """Problems with a `table` request's `where`: a list of {column, op, value, type} conditions (joined with AND)."""
    if where is None:
        return []
    if not isinstance(where, list):
        return ["`where` must be a list of conditions, e.g. [{column: id, op: '=', value: 3}]"]
    problems = []
    for index, item in enumerate(where):
        at = "`where`[%d]" % index
        if not isinstance(item, dict):
            problems.append("%s must be a mapping {column, op, value, type}" % at)
            continue
        unknown = [str(key) for key in item if key not in ("column", "op", "value", "type")]
        if unknown:
            problems.append("%s does not take %s (keys: column, op, value, type)" % (at, ", ".join(unknown)))
        problem = identifier_problem(item.get("column"))
        if problem:
            problems.append("%s: `column` %s" % (at, problem))
        op = str(item.get("op", "=")).upper()
        if op not in OPERATORS:
            problems.append("%s: unknown `op` %r (operators: %s)" % (at, item.get("op"), ", ".join(OPERATORS)))
            continue
        if op in _NO_VALUE:
            if "value" in item:
                problems.append("%s: %s takes no `value`" % (at, op))
        elif "value" not in item:
            problems.append("%s: %s needs a `value`" % (at, op))
        elif op in ("IN", "NOT IN"):
            value = item["value"]
            reference = isinstance(value, str) and "{{" in value and not rendered
            if not reference and not isinstance(value, (list, tuple)):
                problems.append("%s: %s needs a list `value`" % (at, op))
        if "type" in item:
            problem = type_problem(item["type"])
            if problem:
                problems.append("%s: `type` %s" % (at, problem))
    return problems


def table_query(table, schema=None, columns=None, where=None):
    """
    (query, parameters) of a `table` request: SELECT the columns (default: all) FROM the table WHERE every condition
    holds, each value a parameter ($w0, $w1, ...), cast to the condition's `type` when it has one.
    """
    names = [quote_identifier(column) for column in columns] if columns else ["*"]
    source = quote_identifier(table) if not schema else "%s.%s" % (quote_identifier(schema), quote_identifier(table))
    conditions, parameters = [], {}
    for index, item in enumerate(where or ()):
        column, op = quote_identifier(item["column"]), str(item.get("op", "=")).upper()
        if op in _NO_VALUE:
            conditions.append("%s %s" % (column, op))
            continue
        name = "w%d" % index
        parameters[name] = list(item["value"]) if isinstance(item["value"], tuple) else item["value"]
        kind = item.get("type")
        if op in ("IN", "NOT IN"):
            value = "$%s" % name if not kind else "CAST($%s AS %s[])" % (name, kind.strip())
            condition = "%s = ANY(%s)" % (column, value)
            conditions.append(condition if op == "IN" else "NOT (%s)" % condition)
        else:
            value = "$%s" % name if not kind else "CAST($%s AS %s)" % (name, kind.strip())
            conditions.append("%s %s %s" % (column, op, value))
    query = "SELECT %s FROM %s" % (", ".join(names), source)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    return query, parameters


# * ------
# * DSNs
# * ------

def dsn_secrets(dsn):
    """The DSN's passwords (a URL's password, a `password=` keyword), to redact along with the DSN."""
    found = []
    if not isinstance(dsn, str):
        return found
    if _URL_DSN.match(dsn.strip()):
        try:
            parts = urlsplit(dsn.strip())
            password, query = parts.password, parts.query
        except ValueError:  # e.g. a port that is no number: the DSN itself is redacted all the same
            password, query = None, ""
        if password:
            found += [password, unquote(password)]
        for pair in query.split("&") if query else ():
            key, _, value = pair.partition("=")
            if key.lower() == "password" and value:
                found += [value, unquote(value)]
    for match in _KEYWORD_PASSWORD.finditer(dsn):
        value = match.group(1)
        found.append(value)
        if value.startswith("'") and value.endswith("'") and len(value) > 1:
            found.append(re.sub(r"\\(.)", r"\1", value[1:-1]))
    return [value for value in dict.fromkeys(found) if value]


def timeout_ms(value):
    """A statement timeout (seconds as a number, or text such as 500ms, 30s, 5min, 1h) in milliseconds; ValueError."""
    if isinstance(value, bool):
        raise ValueError("expected a number of seconds or a text such as 30s, 500ms, 5min")
    if isinstance(value, (int, float, decimal.Decimal)):
        milliseconds = float(value) * 1000
    else:
        match = _TIMEOUT.match(str(value))
        if not match:
            raise ValueError("%r is not a timeout (a number of seconds, or e.g. 30s, 500ms, 5min, 1h)" % (value,))
        milliseconds = float(match.group(1)) * _TIMEOUT_UNITS[match.group(2)]
    if milliseconds < 1:
        raise ValueError("a statement timeout must be at least 1ms")
    return int(round(milliseconds))


def with_statement_timeout(dsn, milliseconds):
    """
    The DSN that also sets Postgres' statement_timeout for every statement of the session (libpq `options`): a URL
    gets an `options` query parameter, a keyword DSN an `options` keyword. ValueError when it sets `options` already.
    """
    setting = "-c statement_timeout=%d" % milliseconds
    text = dsn.strip()
    if _URL_DSN.match(text):
        query = urlsplit(text).query
        if any(pair.partition("=")[0].lower() == "options" for pair in query.split("&") if pair):
            raise ValueError("the dsn sets `options` already: add -c statement_timeout=... to them instead")
        return text + ("&" if query else ("" if text.endswith("?") else "?")) + "options=" + setting.replace(
            " ", "%20").replace("=", "%3D")
    if _KEYWORD_OPTIONS.search(text):
        raise ValueError("the dsn sets `options` already: add -c statement_timeout=... to them instead")
    return "%s options='%s'" % (text, setting) if text else "options='%s'" % setting


def conninfo_value(value):
    """A value of a libpq keyword/value connection string: single-quoted, its backslashes and quotes escaped."""
    return "'%s'" % str(value).replace("\\", "\\\\").replace("'", "\\'")


def conninfo(parameters):
    """
    The libpq keyword/value connection string of (keyword, value) pairs, every value quoted by conninfo_value(): a
    value holding spaces, quotes or `=` stays that one value, never more keywords. ValueError for a keyword that is
    not one (letters, digits and _), or a value holding a NUL character.
    """
    pairs = []
    for keyword, value in parameters:
        if not isinstance(keyword, str) or not _CONNINFO_KEYWORD.match(keyword):
            raise ValueError("%r is not a libpq connection parameter name" % (keyword,))
        if "\x00" in str(value):
            raise ValueError("the value of `%s` holds a NUL character" % keyword)
        pairs.append("%s=%s" % (keyword, conninfo_value(value)))
    return " ".join(pairs)


def structured_conninfo(host, dbname, user, port=DEFAULT_PORT, password=None, sslmode=None, options=None,
                        statement_timeout=None):
    """
    The libpq connection string of a structured auth (conninfo()): host, port, dbname, user, then password and
    sslmode when given, then the extra libpq parameters of `options` (keyword -> value). `statement_timeout`
    (milliseconds) is added to the libpq `options` parameter (-c statement_timeout=...), after any it sets.
    """
    parameters = [("host", host), ("port", DEFAULT_PORT if port in (None, "") else port), ("dbname", dbname),
                  ("user", user)]
    if password is not None:
        parameters.append(("password", password))
    if sslmode not in (None, ""):
        parameters.append(("sslmode", sslmode))
    extra = dict(options or {})
    if statement_timeout is not None:
        setting = "-c statement_timeout=%d" % statement_timeout
        server = str(extra.get("options") or "").strip()
        extra["options"] = "%s %s" % (server, setting) if server else setting
    parameters += list(extra.items())
    return conninfo(parameters)


# * ------------------
# * reading the rows
# * ------------------

def decode_record(text):
    """A JSON text as a record, its numbers with fractions exact (decimal.Decimal)."""
    return None if text is None else json.loads(text, parse_float=decimal.Decimal)


def message(exc):
    """DuckDB's message on one line, without its query excerpt (LINE 1: ...)."""
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
    A DuckDB connection of the connector's own, in memory, spilling to a private temporary folder (removed by close(),
    or when the Database is collected), with the database `dsn` attached READ_ONLY as `src`, its default catalog.
    `attach_type`: postgres (installing and loading DuckDB's postgres extension when it is not yet), or a stand-in
    (duckdb, sqlite: `dsn` is then a file). A postgres `dsn` (a DSN, or a structured auth's structured_conninfo())
    is kept in a temporary DuckDB secret (the attach path,
    which system views show, is empty) and unconstrained NUMERICs read as text. Once attached, external access is
    off and the configuration locked: a query reads `src` only. ConnectError when the database cannot be attached.
    """

    def __init__(self, dsn, attach_type="postgres", limits=None, page_size=PAGE_SIZE):
        import duckdb
        if attach_type not in ATTACH_TYPES:
            raise ConnectError("unknown attach type %r (types: %s)" % (attach_type, ", ".join(ATTACH_TYPES)))
        self.duckdb = duckdb
        self.attach_type = attach_type
        self.page_size = page_size
        self.folder = tempfile.mkdtemp(prefix="streamwright-postgres-")
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
                    self.connection.execute("INSTALL %s" % extension)  # (nothing is downloaded once installed)
                    self.connection.execute("LOAD %s" % extension)
                except duckdb.Error as exc:
                    raise ConnectError("cannot load DuckDB's %s extension (install it with: INSTALL %s): %s" % (
                        extension, extension, message(exc)))
            try:
                if attach_type == "postgres":
                    # a NUMERIC without (or above 38 digits of) precision is text, exact - not a DOUBLE
                    self.connection.execute("SET pg_numeric_as_varchar = true")
                    # the DSN goes into a temporary secret, never the attach path: duckdb_databases shows that one
                    self.connection.execute("CREATE TEMPORARY SECRET %s (TYPE postgres, URI %s)" % (
                        SECRET, sql_string(dsn)))
                    self.connection.execute("ATTACH '' AS %s (TYPE postgres, SECRET %s, READ_ONLY)" % (ALIAS,
                                                                                                         SECRET))
                else:
                    self.connection.execute("ATTACH %s AS %s (TYPE %s, READ_ONLY)" % (sql_string(dsn), ALIAS,
                                                                                      attach_type))
            except duckdb.Error as exc:
                raise ConnectError("cannot attach the database: %s" % message(exc))
            self.connection.execute("USE %s" % ALIAS)
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
        """
        The rows of a query checked by check_query() (or built by table_query()) as lists of at most page_size
        records, its `parameters` (name -> value) bound. Raises QueryError for a query that is not one read-only
        SELECT, and DuckDB's errors as they are.
        """
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
