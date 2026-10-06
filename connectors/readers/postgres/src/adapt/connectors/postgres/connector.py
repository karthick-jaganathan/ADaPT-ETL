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
The `postgres` connector: read-only SELECT queries on a PostgreSQL database as records.

- `auth: {provider: postgres, dsn: "{{ secrets.pg_dsn }}", statement_timeout: 5min}`: the DSN (a libpq URL or
  keyword DSN) comes from secrets only, and is redacted - with any password inside it - in every log line and error.
  connect() opens a Database (adapt.connectors.postgres.reader): a DuckDB connection of its own that attaches the
  database READ_ONLY through DuckDB's postgres extension (installed and loaded on demand); `statement_timeout`
  (seconds, or 500ms, 30s, 5min, 1h) becomes Postgres' statement_timeout for the session.
- Or, instead of `dsn`, the structured form: `auth: {provider: postgres, host, port: 5432, database (or dbname),
  user, password: "{{ secrets.pg_password }}", sslmode, options: {connect_timeout: "10", ...}, statement_timeout}`:
  host, port, database, user, sslmode and options (extra libpq parameters) are config, literal or references; the
  password is the only credential, a secret only (redacted like a DSN). connect() writes them as a libpq connection
  string, every value quoted and escaped (no value can add a keyword), and attaches it the way it attaches a DSN.
- A `requests` item `{name, sdk: postgres, service: database, method: query, arguments: {query, params}}` runs one
  SELECT (DuckDB SQL over the attached database: `schema.table`, or `table` in the default schema) whose values are
  named parameters - `$since` in the query, `params: {since: "{{ window.start }}"}` - BOUND, never pasted into the
  query: a list binds as a list (`id = ANY($ids::BIGINT[])`, e.g. a `batch_size` partition of ids).
- `{method: table, arguments: {schema, table, columns, where}}` reads a table: the columns (default: all) of the rows
  where every condition `{column, op, value, type}` holds, its names quoted and its values bound.
- Each row is one record, a JSON object, in pages of at most 1,000 records. Each query is a call (rate limit, retries
  and the run's request counts) and logs a line on `adapt.network`: its rows and time.

Only these read-only methods can be called. The query is checked to be one SELECT without a write keyword before it
runs, reading the database's own tables only (no DuckDB system view such as duckdb_databases, which would show the
attached database's path); the database is attached READ_ONLY (Postgres reads in READ ONLY transactions) through a
temporary DuckDB secret holding the DSN (or the structured form's connection string), and the connection can reach
nothing else: writes fail even when the database role could write.
"""

import re
import time

from adapt.core.runtime import logs
from adapt.core.runtime.components import Connector, ConnectorError
from adapt.connectors.postgres.reader import (ATTACH_TYPES, CONNECTION_OPTIONS, DEFAULT_PORT, PAGE_SIZE, SSLMODES,
                                              ConnectError, Database, QueryError, check_query, conninfo_value,
                                              dsn_secrets, identifier_problem, message, structured_conninfo,
                                              table_query, timeout_ms, where_problems, with_statement_timeout)


__all__ = ["PostgresConnector", "SERVICES", "DEFAULT_SERVICE", "CONNECTION_KEYS"]

DEFAULT_SERVICE = "database"
SERVICES = {"database": {"query": ("query", "params"), "table": ("schema", "table", "columns", "where")}}
REQUIRED = {("database", "query"): ("query",), ("database", "table"): ("table",)}
# the structured form of `auth`, the alternative to `dsn` (`dbname`: an alias of `database`)
CONNECTION_KEYS = ("host", "port", "database", "dbname", "user", "password", "sslmode", "options")
# `options` keys that are auth keys of their own, or credentials (only `password`, a secret, is one)
_OWN_KEYS = {"host": "host", "port": "port", "dbname": "database", "database": "database", "user": "user",
             "sslmode": "sslmode"}
_CREDENTIALS = ("password", "passfile", "sslpassword", "oauth_client_secret", "scram_client_key", "scram_server_key")
_SECRETS = re.compile(r"\{\{\s*secrets\b")
_SECRET_REFERENCE = re.compile(r"^\s*\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}\s*$")
_WHOLE_REFERENCE = re.compile(r"^\s*\{\{[^{}]*\}\}\s*$")
_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPTION_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_TRANSIENT = ("could not connect", "connection refused", "server closed the connection", "connection timed out",
              "timeout expired", "connection reset", "ssl syscall", "terminating connection", "no connection to the "
              "server", "could not receive data", "could not send data")
_DSN_EXAMPLE = "dsn: \"{{ secrets.pg_dsn }}\""
_PASSWORD_EXAMPLE = "password: \"{{ secrets.pg_password }}\""


def _has_secret(value):
    if isinstance(value, str):
        return bool(_SECRETS.search(value))
    if isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    if isinstance(value, dict):
        return any(_has_secret(item) for item in value.values())
    return False


def _reference(value):
    return isinstance(value, str) and "{{" in value


def _port_problem(port):
    if _reference(port):
        return None  # e.g. "{{ config.pg_port }}": checked once rendered
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port)
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return None
    return "`port` must be a port number (1-65535), e.g. %d; not %r" % (DEFAULT_PORT, port)


def _options_problems(options):
    if not isinstance(options, dict):
        return ["`options` must be a mapping of libpq connection parameters to their values, e.g. {connect_timeout: "
                "\"10\", application_name: adapt}"]
    problems = []
    for key, value in options.items():
        if not isinstance(key, str) or not _OPTION_KEY.match(key):
            problems.append("`options` key %r is not a libpq parameter name (lowercase letters, digits and _)" % (
                key,))
        elif key in _CREDENTIALS:
            problems.append("`options` cannot set `%s`: the only credential is `password`, a secret reference" % key)
        elif key == "dsn":
            problems.append("`options` cannot set `dsn`: a DSN is the auth key `dsn` (a secret), instead of the "
                            "connection keys")
        elif key in _OWN_KEYS:
            problems.append("`options` cannot set `%s`: use the auth key `%s`" % (key, _OWN_KEYS[key]))
        elif key not in CONNECTION_OPTIONS:
            problems.append("`options` key `%s` is not a libpq connection parameter it may set (e.g. connect_timeout, "
                            "application_name, target_session_attrs, sslrootcert)" % key)
        elif _has_secret(value):
            problems.append("`options.%s` cannot use secrets (`password` is the only secret)" % key)
        elif isinstance(value, bool) or not isinstance(value, (str, int)):
            problems.append("`options.%s` must be text (or a whole number), e.g. \"10\"" % key)
        elif "\x00" in str(value):
            problems.append("`options.%s` cannot hold a NUL character" % key)
    return problems


def _connection_problems(auth):
    """The problems of the structured form of `auth` (its keys may be references, unless rendered)."""
    problems = ["needs `%s`" % key for key in ("host", "user") if key not in auth]
    if "database" in auth and "dbname" in auth:
        problems.append("use `database` or its alias `dbname`, not both")
    elif "database" not in auth and "dbname" not in auth:
        problems.append("needs `database` (or its alias `dbname`)")
    for key in ("host", "database", "dbname", "user"):
        value = auth.get(key)
        if key not in auth:
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append("`%s` must be text or a reference, e.g. \"{{ config.pg_%s }}\"" % (key, key))
        elif "\x00" in value:
            problems.append("`%s` cannot hold a NUL character" % key)
    if "port" in auth:
        problem = _port_problem(auth["port"])
        if problem:
            problems.append(problem)
    if "password" in auth:
        password = auth["password"]
        if not isinstance(password, str) or not password:
            problems.append("`password` must be a secret reference, e.g. %s" % _PASSWORD_EXAMPLE)
        elif _reference(password) and not _SECRET_REFERENCE.match(password):
            problems.append("`password` must be one secret reference, e.g. %s (a password is never written in the "
                            "source or its config)" % _PASSWORD_EXAMPLE)
    if "sslmode" in auth:
        sslmode = auth["sslmode"]
        if not isinstance(sslmode, str) or not (sslmode in SSLMODES or _reference(sslmode)):
            problems.append("`sslmode` must be one of: %s; not %r" % (", ".join(SSLMODES), sslmode))
    if "options" in auth:
        problems += _options_problems(auth["options"])
    return problems


def connector_error(exc, redact=None):
    """A ConnectorError for the reader's and DuckDB's errors (redacted with `redact`), or None for other exceptions."""
    redact = redact or str
    if isinstance(exc, QueryError):
        return ConnectorError("postgres: %s" % redact(exc), code="QUERY_REFUSED")
    if isinstance(exc, ConnectError):
        text = str(exc)
        return ConnectorError("postgres: %s" % redact(text), code="CONNECT_ERROR",
                              retryable=any(phrase in text.lower() for phrase in _TRANSIENT))
    try:
        import duckdb
    except ImportError:  # pragma: no cover - duckdb is a dependency
        return None
    if not isinstance(exc, duckdb.Error):
        return None
    text = message(exc)
    lower = text.lower()
    if "read-only" in lower or "read only transaction" in lower:
        return ConnectorError("postgres: the database is read-only: %s" % redact(text), code="READ_ONLY")
    if "statement timeout" in lower:
        return ConnectorError("postgres: %s" % redact(text), code="STATEMENT_TIMEOUT")
    if any(phrase in lower for phrase in _TRANSIENT):
        return ConnectorError("postgres: %s" % redact(text), code="CONNECTION_ERROR", retryable=True)
    return ConnectorError("postgres: %s" % redact(text), code="QUERY_ERROR")


class PostgresConnector(Connector):

    name = "postgres"
    auth_required = ()  # `dsn`, or `host`, `database` and `user` (check_auth)
    auth_optional = ("dsn",) + CONNECTION_KEYS + ("statement_timeout",)
    network_loggers = ()  # DuckDB and its postgres extension write no logs
    category = "databases"
    summary = "PostgreSQL tables, read-only"

    def __init__(self, page_size=PAGE_SIZE, clock=time.monotonic, attach_type="postgres"):
        """
        `attach_type`: postgres; tests and embedding may attach a DuckDB or SQLite file instead (its path as `dsn`, or
        as `database` in the structured form).
        """
        if attach_type not in ATTACH_TYPES:
            raise ValueError("unknown attach type %r (types: %s)" % (attach_type, ", ".join(ATTACH_TYPES)))
        self.page_size = page_size
        self.clock = clock
        self.attach_type = attach_type

    # checks -----------------------------------------------------------------------

    def check_auth(self, auth):
        """
        Either `dsn` or the structured form (host, port, database or dbname, user, password, sslmode, options), not
        both. `dsn`: one `{{ secrets.NAME }}` reference (any other reference is refused: a DSN holds credentials). adapt
        run checks the rendered block too - the DSN itself - and connect() then refuses a DSN that is not a secret
        of the run, so a DSN written in the source never connects. The structured form needs `host`, `database` and
        `user` (text or references: they are config); `password`, the only credential, is the same as `dsn`: one
        secret reference, and connect() refuses a password that is not a secret of the run. `statement_timeout`:
        optional.
        """
        problems = super(PostgresConnector, self).check_auth(auth)
        what = "auth: provider 'postgres':"
        structured = [key for key in CONNECTION_KEYS if key in auth]
        if "dsn" in auth:
            dsn = auth["dsn"]
            if not isinstance(dsn, str) or not dsn.strip():
                problems.append("%s `dsn` must be a secret reference, e.g. %s" % (what, _DSN_EXAMPLE))
            elif _reference(dsn) and not _SECRET_REFERENCE.match(dsn):
                problems.append("%s `dsn` must be one secret reference, e.g. %s (a DSN holds credentials: it is "
                                "never written in the source or its config)" % (what, _DSN_EXAMPLE))
            if structured:
                problems.append("%s use either `dsn` or the connection keys (host, port, database, user, password, "
                                "sslmode, options), not both: %s" % (what, ", ".join("`%s`" % key
                                                                                     for key in structured)))
        elif structured:
            problems += ["%s %s" % (what, problem) for problem in _connection_problems(auth)]
        else:
            problems.append("%s needs `dsn` (a secret reference, e.g. %s) or the connection keys `host`, `database` "
                            "and `user` (and `password`, a secret reference)" % (what, _DSN_EXAMPLE))
        if "statement_timeout" in auth:
            timeout = auth["statement_timeout"]
            if _has_secret(timeout):
                problems.append("%s `statement_timeout` cannot use secrets" % what)
            elif timeout is not None and not _reference(timeout):
                try:
                    timeout_ms(timeout)
                except ValueError as exc:
                    problems.append("%s `statement_timeout`: %s" % (what, exc))
        return problems

    def check_request(self, request):
        return self._check_call(request, rendered=False)

    @staticmethod
    def _check_call(request, rendered):
        """The service, method and arguments of a call; `rendered`: its references were rendered already."""
        service = request.get("service") or DEFAULT_SERVICE
        methods = SERVICES.get(service)
        if methods is None:
            return ["postgres: service %r is not supported (supported: %s)" % (service, ", ".join(SERVICES))]
        method = request.get("method")
        if method not in methods:
            return ["postgres: %s.%s is not supported (supported: %s)" % (service, method, ", ".join(methods))]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["postgres: `arguments` must be a mapping"]
        expected = methods[method]
        problems = ["postgres: %s.%s needs `%s`" % (service, method, key) for key in REQUIRED[(service, method)]
                    if key not in arguments]
        problems += ["postgres: %s.%s does not take `%s` (arguments: %s)" % (service, method, key, ", ".join(expected))
                     for key in arguments if key not in expected]
        if _has_secret(arguments):
            return problems + ["postgres: arguments cannot use secrets (the DSN is the only credential; queries and "
                               "their values are logged and kept in the source)"]
        if method == "query":
            problems += PostgresConnector._query_problems(arguments, rendered)
        else:
            problems += PostgresConnector._table_problems(arguments, rendered)
        return problems

    @staticmethod
    def _query_problems(arguments, rendered):
        params = arguments.get("params")
        problems = []
        if params is not None and not isinstance(params, dict):
            problems.append("postgres: `params` must be a mapping of names to values, e.g. {since: "
                            "\"{{ window.start }}\"}")
            params = None
        elif params:
            wrong = [str(name) for name in params if not isinstance(name, str) or not _PARAMETER.match(name)]
            if wrong:
                problems.append("postgres: `params` names must be letters, digits and _ (not starting with a digit): "
                                "%s" % ", ".join(wrong))
        if "query" not in arguments:
            return problems
        query = arguments["query"]
        if not isinstance(query, str):
            return problems + ["postgres: `query` must be SQL text: one SELECT, with $name for the values in "
                               "`params`"]
        if problems:
            return problems
        problems += ["postgres: %s" % problem for problem in check_query(query, params or {})[1]]
        return problems

    @staticmethod
    def _table_problems(arguments, rendered):
        problems = []
        for key in ("schema", "table"):
            if key not in arguments or (arguments[key] is None and key == "schema"):
                continue
            value = arguments[key]
            if not rendered and _reference(value) and _WHOLE_REFERENCE.match(value):
                continue  # e.g. "{{ partition.schema }}": quoted at run time
            problem = identifier_problem(value)
            if problem:
                problems.append("postgres: `%s` %r %s" % (key, value, problem))
        columns = arguments.get("columns")
        if columns is not None:
            if not isinstance(columns, list) or not columns:
                problems.append("postgres: `columns` must be a non-empty list of column names")
            else:
                for column in columns:
                    problem = identifier_problem(column) or ("is a reference: columns are literal names"
                                                             if _reference(column) else None)
                    if problem:
                        problems.append("postgres: column %r %s" % (column, problem))
        problems += ["postgres: %s" % problem for problem in where_problems(arguments.get("where"), rendered)]
        return problems

    # running ----------------------------------------------------------------------

    def connect(self, auth, context):
        if "dsn" not in auth and any(key in auth for key in CONNECTION_KEYS):
            target = self._structured_target(auth, context)
        else:
            target = self._dsn_target(auth, context)
        try:
            return Database(target, self.attach_type, page_size=self.page_size)
        except (ConnectError, QueryError) as exc:
            raise connector_error(exc, context.redact)
        except Exception as exc:
            error = connector_error(exc, context.redact)
            if error is None:
                raise
            raise error

    def _dsn_target(self, auth, context):
        """What the `dsn` form attaches: the DSN (with the statement timeout), redacted from now on."""
        dsn = auth.get("dsn")
        given = isinstance(dsn, str) and bool(dsn.strip())
        # a secret of the run is masked whole already; a DSN written in the source is not
        secret = given and context.redact(dsn).strip() == "***"
        if given:  # redacted from now on, whatever happens next
            context.secret(dsn)
            for value in dsn_secrets(dsn):
                context.secret(value)
        problems = self.check_auth(auth)
        if not problems and not given:
            problems = ["auth: provider 'postgres': `dsn` is empty"]
        if problems:
            raise ConnectorError(context.redact("; ".join(problems)))
        if not secret:
            raise ConnectorError("postgres: `dsn` must come from secrets: write %s in the source's auth and pass the "
                                 "DSN as a secret (e.g. ADAPT_SECRET_PG_DSN, or adapt run --secrets FILE)"
                                 % _DSN_EXAMPLE)
        target = dsn.strip()
        timeout = auth.get("statement_timeout")
        if timeout not in (None, "") and self.attach_type == "postgres":  # (stand-ins have no server)
            try:
                target = with_statement_timeout(target, timeout_ms(timeout))
            except ValueError as exc:
                raise ConnectorError("postgres: `statement_timeout`: %s" % exc)
            context.secret(target)
        return target

    def _structured_target(self, auth, context):
        """
        What the structured form attaches: its libpq connection string (structured_conninfo: every value quoted and
        escaped), attached like a DSN, through a temporary DuckDB secret. The password - and the connection string,
        which holds it - is redacted from now on. A stand-in attaches the file `database`.
        """
        password = auth.get("password")
        given = isinstance(password, str) and password != ""
        secret = given and context.redact(password).strip() == "***"
        if given:
            context.secret(password)
            escaped = conninfo_value(password)
            context.secret(escaped)
            context.secret(escaped[1:-1])
        problems = self.check_auth(auth)
        if problems:
            raise ConnectorError(context.redact("; ".join(problems)))
        if given and not secret:
            raise ConnectorError("postgres: `password` must come from secrets: write %s in the source's auth and pass "
                                 "the password as a secret (e.g. ADAPT_SECRET_PG_PASSWORD, or adapt run --secrets FILE)"
                                 % _PASSWORD_EXAMPLE)
        database = auth["database"] if "database" in auth else auth["dbname"]
        if self.attach_type != "postgres":  # (a stand-in: a file, no server)
            return database
        timeout = auth.get("statement_timeout")
        try:
            milliseconds = None if timeout in (None, "") else timeout_ms(timeout)
        except ValueError as exc:
            raise ConnectorError("postgres: `statement_timeout`: %s" % exc)
        try:
            target = structured_conninfo(auth["host"], database, auth["user"], port=auth.get("port"),
                                         password=password if given else None, sslmode=auth.get("sslmode"),
                                         options=auth.get("options"), statement_timeout=milliseconds)
        except ValueError as exc:
            raise ConnectorError(context.redact("postgres: %s" % exc))
        context.secret(target)
        return target

    def request(self, client, request, context):
        problems = self._check_call(request, rendered=True)
        if problems:
            raise ConnectorError("; ".join(problems), code="QUERY_REFUSED")
        method = request["method"]
        arguments = request["arguments"]
        if method == "query":
            query, parameters = arguments["query"], dict(arguments.get("params") or {})
            label = "query"
        else:
            schema = arguments.get("schema") or None
            query, parameters = table_query(arguments["table"], schema, arguments.get("columns"),
                                            arguments.get("where"))
            label = "table %s" % (arguments["table"] if not schema else "%s.%s" % (schema, arguments["table"]))
        for page in self._read(client, method, label, query, parameters, context):
            yield page

    def _read(self, client, method, label, query, parameters, context):
        """The pages of one query; the first is read through context.call(), then one adapt.network line."""
        where = context.where

        def start():
            try:
                pages = client.pages(query, parameters)
                return next(pages, None), pages
            except ConnectorError:
                raise
            except Exception as exc:
                error = connector_error(exc, context.redact)
                if error is None:
                    raise
                raise error
        rows, seconds = 0, 0.0
        started = self.clock()
        page, pages = context.call(start)
        seconds += self.clock() - started
        try:
            while page is not None:
                rows += len(page)
                yield page
                started = self.clock()
                page = next(pages, None)
                seconds += self.clock() - started
        except Exception as exc:  # a failure after the first page is not retried: its rows were already read
            error = connector_error(exc, context.redact)
            if error is None:
                raise
            raise error
        logs.NETWORK.info("%spostgres %s: %s row(s), %.2f s", _prefix(where), label, "{:,}".format(rows), seconds,
                          extra=logs.fields(event="db_query", connector=self.name, call="database.%s" % method,
                                            records=rows, duration_ms=int(round(seconds * 1000)), **(where or {})))

    def error(self, exc):
        return connector_error(exc)


def _prefix(where):
    text = logs.describe(where) if where else ""
    return text + ": " if text else ""
