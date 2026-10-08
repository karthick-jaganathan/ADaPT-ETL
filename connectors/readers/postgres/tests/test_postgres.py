"""
The postgres connector: auth and request checks, the single-SELECT and read-only guards, bound parameters, pages and
JSON records, redaction, and runs through SourceRunner and the CLI. Reads use a stand-in: a local DuckDB file attached
READ_ONLY as the connector's `src`, through the same connect/pages/to_json path (no Postgres, no network). Set
STREAMWRIGHT_TEST_PG_DSN to a throwaway Postgres to also run test_a_real_postgres, and STREAMWRIGHT_TEST_PG_HOST (with
STREAMWRIGHT_TEST_PG_USER, STREAMWRIGHT_TEST_PG_DATABASE and optionally STREAMWRIGHT_TEST_PG_PORT, STREAMWRIGHT_TEST_PG_PASSWORD,
STREAMWRIGHT_TEST_PG_SSLMODE) to run test_a_real_postgres_with_the_structured_form.
"""

import datetime
import decimal
import json
import logging
import os
import uuid

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("streamwright.connectors.postgres.connector")

from streamwright.connectors.postgres import connector as connector_module  # noqa: E402
from streamwright.connectors.postgres import reader  # noqa: E402
from streamwright.connectors.postgres.connector import PostgresConnector, connector_error  # noqa: E402
from streamwright.connectors.postgres.reader import (ConnectError, Database, QueryError, check_query, conninfo,  # noqa: E402
                                              conninfo_value, dsn_secrets, structured_conninfo, table_query, timeout_ms,
                                              with_statement_timeout)
from streamwright.core import cli
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.logs import RunMetrics  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "readers", "postgres_demo")
TODAY = datetime.date(2026, 10, 5)
BIG = "1234567890123456.78"
INJECTION = "'; DROP TABLE customers; --"
DAY = datetime.timedelta(days=1)


# * -------
# * helpers
# * -------

def standin(path, today=TODAY):
    """A DuckDB file shaped like the demo's Postgres database (schema public), orders on the two days before today."""
    import duckdb
    first, second = today - 2 * DAY, today - DAY
    connection = duckdb.connect(str(path))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("CREATE SCHEMA public")
        connection.execute("CREATE TABLE public.customers (id BIGINT, name VARCHAR, email VARCHAR, status VARCHAR, "
                           "created_at TIMESTAMPTZ)")
        connection.executemany("INSERT INTO public.customers VALUES (?, ?, ?, ?, ?::TIMESTAMPTZ)", [
            (1, "Ann", "ANN@example.com", "active", "2026-01-01 00:00:00+00"),
            (2, "Bob", "bob@example.com", "closed", "2026-02-01 12:30:00+00"),
            (3, INJECTION, None, "active", "2026-03-01 00:00:00+00")])
        connection.execute("CREATE TABLE public.orders (id BIGINT, customer_id BIGINT, status VARCHAR, "
                           "total DECIMAL(18,2), currency VARCHAR, updated_at TIMESTAMPTZ)")
        connection.executemany("INSERT INTO public.orders VALUES (?, ?, ?, ?::DECIMAL(18,2), ?, ?::TIMESTAMPTZ)", [
            (10, 1, "paid", "12.50", "EUR", "%s 08:00:00+00" % first),
            (11, 2, "paid", BIG, "USD", "%s 23:30:00+00" % second),
            (12, 1, "new", "1.00", "EUR", "%s 00:00:00+00" % (today - 30 * DAY))])
        connection.execute("CREATE TABLE public.order_lines (order_id BIGINT, line_number INTEGER, product_id BIGINT, "
                           "quantity INTEGER, unit_price DECIMAL(12,2))")
        connection.executemany("INSERT INTO public.order_lines VALUES (?, ?, ?, ?, ?::DECIMAL(12,2))", [
            (10, 1, 100, 2, "6.25"), (10, 2, 101, 1, "0.00"), (11, 1, 102, 1, "99.99"), (12, 1, 103, 1, "1.00")])
    finally:
        connection.close()
    return str(path)


@pytest.fixture
def db(tmp_path):
    return standin(tmp_path / "shop.duckdb")


@pytest.fixture
def connector():
    """The connector attaching the DuckDB stand-in instead of Postgres (same code path), registered for the test."""
    component = PostgresConnector(attach_type="duckdb")
    components.register(component)
    yield component
    components.unregister("postgres")


@pytest.fixture
def small_pages():
    component = PostgresConnector(attach_type="duckdb", page_size=2)
    components.register(component)
    yield component
    components.unregister("postgres")


def context(connector, secrets=(), where=None, metrics=None):
    result = ConnectorContext(connector, Redactor(secrets), metrics=metrics)
    result.where = where
    return result


def connect(connector, dsn, **auth):
    return connector.connect(dict({"dsn": dsn}, **auth), context(connector, [dsn]))


def pages(connector, dsn, method, arguments, ctx=None):
    client = connect(connector, dsn)
    try:
        return list(connector.request(client, {"service": "database", "method": method, "arguments": arguments},
                                      ctx or context(connector, [dsn])))
    finally:
        client.close()


def query(connector, dsn, text, params=None):
    return [record for page in pages(connector, dsn, "query", {"query": text, "params": params or {}})
            for record in page]


def table(connector, dsn, **arguments):
    return [record for page in pages(connector, dsn, "table", arguments) for record in page]


def sdk(name, method="query", **arguments):
    return {"name": name, "sdk": "postgres", "service": "database", "method": method, "arguments": arguments}


def pg_source(*streams):
    return {"kind": "source", "name": "pg", "auth": {"provider": "postgres", "dsn": "{{ secrets.pg_dsn }}"},
            "streams": list(streams)}


def run(source, dsn, output=None, config=None, state=None, metrics=None, redact=None):
    output = output if output is not None else MemoryOutput()
    SourceRunner(source, config or {}, {"pg_dsn": dsn}, state=state, output=output, today=TODAY,
                 sleep=lambda seconds: None, allowed_connectors=["postgres"], metrics=metrics, redact=redact).run()
    return output


def records(output, name):
    return [record for stream, record in output.records if stream == name]


class Spy(object):
    """A DuckDB connection that records what is executed: (SQL text, parameters)."""

    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def execute(self, text, parameters=None):
        self.calls.append((text, parameters))
        return self.connection.execute(text, parameters) if parameters is not None else self.connection.execute(text)


def spied(connector, dsn):
    client = connect(connector, dsn)
    spy = Spy(client.connection)
    client.connection = spy
    return client, spy


# * ------------------------------
# * reads: records, pages, values
# * ------------------------------

def test_a_query_reads_rows_as_exact_json_records(connector, db):
    rows = query(connector, db, "SELECT * FROM public.orders WHERE id <= $last ORDER BY id", {"last": 11})
    assert rows == [
        {"id": 10, "customer_id": 1, "status": "paid", "total": decimal.Decimal("12.50"), "currency": "EUR",
         "updated_at": "%s 08:00:00+00" % (TODAY - 2 * DAY)},
        {"id": 11, "customer_id": 2, "status": "paid", "total": decimal.Decimal(BIG), "currency": "USD",
         "updated_at": "%s 23:30:00+00" % (TODAY - DAY)}]
    # unqualified names are in the database's default schema (public on Postgres; main in the stand-in)
    assert query(connector, db, "SELECT count(*) AS n FROM public.customers WHERE email IS NULL") == [{"n": 1}]


def test_rows_arrive_in_pages_of_at_most_page_size(small_pages, db):
    found = pages(small_pages, db, "query", {"query": "SELECT * FROM public.order_lines ORDER BY order_id, "
                                                      "line_number"})
    assert [len(page) for page in found] == [2, 2]
    assert pages(small_pages, db, "query", {"query": "SELECT * FROM public.orders WHERE false"}) == []


def test_large_results_land_in_pages_of_1000_records(connector, db):
    found = pages(connector, db, "query", {"query": "SELECT range AS n FROM range($count)", "params": {"count": 2500}})
    assert [len(page) for page in found] == [1000, 1000, 500] and found[2][-1] == {"n": 2499}


def test_a_trailing_semicolon_and_comments_are_fine(connector, db):
    assert query(connector, db, "SELECT id FROM public.customers WHERE id = $id; -- the first one", {"id": 1}) == [
        {"id": 1}]
    # a comment ending the query does not swallow the connector's wrapper
    assert query(connector, db, "SELECT id FROM public.customers WHERE id = 2 -- note") == [{"id": 2}]


def test_the_table_method_quotes_names_and_binds_where_values(connector, db):
    assert table(connector, db, schema="public", table="customers", columns=["id", "name"],
                 where=[{"column": "status", "op": "=", "value": "active"},
                        {"column": "id", "op": "IN", "value": ["1", "2", "3"], "type": "BIGINT"}]) == [
        {"id": 1, "name": "Ann"}, {"id": 3, "name": INJECTION}]
    assert table(connector, db, schema="public", table="customers", columns=["id"],
                 where=[{"column": "email", "op": "IS NULL"}]) == [{"id": 3}]
    assert table(connector, db, schema="public", table="orders", columns=["id"],
                 where=[{"column": "id", "op": "NOT IN", "value": [10, 11]}]) == [{"id": 12}]
    query_text, parameters = table_query("my table", "public", ["id", 'odd"name'],
                                         [{"column": "updated_at", "op": ">=", "value": "2026-10-01",
                                           "type": "DATE"}, {"column": "x", "op": "IS NOT NULL"}])
    assert query_text == ('SELECT "id", "odd""name" FROM "public"."my table" WHERE "updated_at" >= '
                          'CAST($w0 AS DATE) AND "x" IS NOT NULL')
    assert parameters == {"w0": "2026-10-01"}
    with pytest.raises(ConnectorError, match="does not exist"):
        table(connector, db, schema="public", table='customers"; DROP TABLE customers; --')


# * ---------------------------------------
# * bound parameters: values never pasted
# * ---------------------------------------

def test_parameter_values_are_bound_never_pasted_into_the_query(connector, db):
    client, spy = spied(connector, db)
    try:
        found = [record for page in connector.request(client, {"method": "query", "arguments": {
            "query": "SELECT id, name FROM public.customers WHERE name = $name", "params": {"name": INJECTION}}},
            context(connector, [db])) for record in page]
        # the injection attempt is a value: it matches the customer named so, and runs nothing
        assert found == [{"id": 3, "name": INJECTION}]
        text, parameters = spy.calls[-1]
        assert INJECTION not in text and "DROP" not in text and "$name" in text
        assert parameters == {"name": INJECTION}
        assert [record for page in connector.request(client, {"method": "table", "arguments": {
            "schema": "public", "table": "customers", "columns": ["id"],
            "where": [{"column": "status", "value": INJECTION}]}}, context(connector, [db])) for record in page] == []
        text, parameters = spy.calls[-1]
        assert INJECTION not in text and parameters == {"w0": INJECTION}
    finally:
        client.close()
    assert len(query(connector, db, "SELECT * FROM public.customers")) == 3  # nothing was dropped


def test_typed_values_lists_and_dates_bind_as_they_are(connector, db):
    since = TODAY - 2 * DAY
    rows = query(connector, db, "SELECT id FROM public.orders WHERE updated_at >= $since AND updated_at < $until + "
                                "INTERVAL 1 DAY AND id = ANY($ids::BIGINT[]) ORDER BY id",
                 {"since": since, "until": TODAY - DAY, "ids": ["10", "11", "12"]})
    assert rows == [{"id": 10}, {"id": 11}]
    assert query(connector, db, "SELECT $amount AS amount, $flag AS flag, $none AS none",
                 {"amount": decimal.Decimal(BIG), "flag": True, "none": None}) == [
        {"amount": decimal.Decimal(BIG), "flag": True, "none": None}]


# * ----------------------------------
# * read-only: guards and the attach
# * ----------------------------------

@pytest.mark.parametrize("statement", [
    "INSERT INTO public.customers VALUES (9, 'x', 'x', 'x', now())",
    "UPDATE public.customers SET name = 'x'",
    "DELETE FROM public.customers",
    "COPY public.customers TO 'out.csv'",
    "CREATE TABLE x AS SELECT 1",
    "DROP TABLE public.customers",
    "ALTER TABLE public.customers ADD COLUMN y INTEGER",
    "GRANT SELECT ON public.customers TO someone",
    "TRUNCATE public.customers",
    "CALL postgres_execute('src', 'DELETE FROM customers')",
    "SELECT 1; DELETE FROM public.customers",
    "WITH gone AS (DELETE FROM public.customers RETURNING *) SELECT * FROM gone",
    "SELECT * FROM public.customers FOR UPDATE",
    "SELECT * FROM postgres_query('src', 'DELETE FROM customers RETURNING *')",
    "SELECT postgres_execute('src', 'DROP TABLE customers')",
    "ATTACH 'other.duckdb' AS other",
    "SET enable_external_access = true",
])
def test_writes_are_refused_before_anything_runs(connector, db, statement):
    client, spy = spied(connector, db)
    try:
        with pytest.raises(ConnectorError) as caught:
            list(connector.request(client, {"method": "query", "arguments": {"query": statement}},
                                   context(connector, [db])))
        assert caught.value.code == "QUERY_REFUSED"
        assert spy.calls == []  # refused by the checks: nothing reached DuckDB
        with pytest.raises(QueryError):
            list(client.pages(statement))  # the reader checks too
    finally:
        client.close()
    assert len(query(connector, db, "SELECT * FROM public.customers")) == 3


def test_the_database_is_attached_read_only_even_when_writable(connector, db):
    assert os.access(db, os.W_OK)  # the file (the role) could be written
    client = connect(connector, db)
    try:
        for statement in ("INSERT INTO src.public.customers VALUES (9, 'x', 'x', 'x', now())",
                          "DELETE FROM public.customers", "DROP TABLE public.customers",
                          "CREATE TABLE src.public.x (a INTEGER)"):
            with pytest.raises(Exception) as caught:  # past the checks, DuckDB refuses it
                client.connection.execute(statement)
            error = connector.error(caught.value)
            assert error.code == "READ_ONLY" and "read-only" in str(error)
        # and the connection reaches nothing else: no other files, no other databases, no settings changes
        other = os.path.join(os.path.dirname(db), "other.csv")
        with open(other, "w") as stream:
            stream.write("a\n1\n")
        for statement in ("SELECT * FROM read_csv('%s')" % other, "ATTACH '%s' AS other" % (db + ".other"),
                          "SET enable_external_access = true", "INSTALL httpfs", "LOAD httpfs"):
            with pytest.raises(Exception):
                client.connection.execute(statement)
    finally:
        client.close()
    assert len(query(connector, db, "SELECT * FROM public.customers")) == 3
    assert client.closed


# * ---------------------------------------------------------------------
# * the DSN: DuckDB's system views show the attached database's path
# * ---------------------------------------------------------------------

PASSWORD = "Sup3r-S3cret-pw"
LEAKS = [
    "SELECT file FROM pragma_database_list WHERE file IS NOT NULL",
    "SELECT path FROM system.duckdb_databases",
    "SELECT * FROM duckdb_databases",
    "SELECT * FROM main.duckdb_databases",
    "SELECT * FROM system.main.duckdb_databases",
    "SELECT * FROM \"SYSTEM\".\"DuckDB_Databases\"",
    "SELECT (SELECT max(path) FROM duckdb_databases) AS p",
    "WITH d AS (SELECT * FROM system.duckdb_databases) SELECT * FROM d",
    "SELECT * FROM pragma_database_list()",
    "SELECT * FROM (SUMMARIZE duckdb_databases)",
    "SELECT * FROM (SHOW DATABASES)",
    "SELECT * FROM (DESCRIBE public.orders)",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM pg_tables",
    "SELECT * FROM pg_catalog.pg_settings",
    "SELECT * FROM src.pg_catalog.pg_class",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM temp.main.x",
    "SELECT * FROM memory.x",
]


@pytest.fixture
def secret_db(tmp_path):
    """The stand-in at a path holding a password: attached by path, as a DSN is (the path is the 'DSN')."""
    folder = tmp_path / PASSWORD
    folder.mkdir()
    return standin(folder / "shop.duckdb")


@pytest.mark.parametrize("statement", LEAKS)
def test_system_views_and_schemas_are_refused(connector, secret_db, statement):
    found = check(connector, query=statement)
    assert any("reads the database" in problem or "cannot call" in problem or "SHOW, DESCRIBE" in problem
               for problem in found), found
    client, spy = spied(connector, secret_db)
    try:
        with pytest.raises(ConnectorError) as caught:
            list(connector.request(client, {"method": "query", "arguments": {"query": statement}},
                                   context(connector, [secret_db])))
        assert caught.value.code == "QUERY_REFUSED" and PASSWORD not in str(caught.value)
        with pytest.raises(QueryError):
            list(client.pages(statement))  # the reader checks too
        assert spy.calls == []
    finally:
        client.close()


def test_the_database_tables_stay_readable(connector, secret_db):
    for statement in ("SELECT id FROM src.public.orders", "SELECT id FROM public.orders",
                      "SELECT o.id FROM public.orders o JOIN public.customers c ON c.id = o.customer_id",
                      "SELECT 1 AS id FROM range(1)"):
        assert check(connector, query=statement) == [], statement
    assert [row["id"] for row in query(connector, secret_db, "SELECT id FROM src.public.orders ORDER BY id")] == [
        10, 11, 12]
    assert reader.query_problems("SELECT * FROM orders") == []
    assert reader.query_problems("SELECT * FROM public.pg_tables") == []  # a table of the database's own schema


def test_a_run_cannot_read_the_dsn(connector, secret_db, tmp_path, monkeypatch, caplog, capsys):
    import yaml
    stream = page_stream("leak", sdk("raw_leak", query="SELECT file FROM pragma_database_list WHERE file IS NOT NULL"),
                         "SELECT record->>'file' AS file FROM raw_leak")
    with pytest.raises(SourceError) as caught:
        run(pg_source(stream), secret_db)
    assert "pragma_database_list" in str(caught.value) and PASSWORD not in str(caught.value)
    with open(os.path.join(EXAMPLE, "source.yaml")) as handle:
        document = yaml.safe_load(handle)
    document["streams"] = [stream]
    document["auth"] = {"provider": "postgres", "dsn": "{{ secrets.pg_dsn }}"}
    document["spec"]["secrets"] = {"pg_dsn": {"type": "string"}}
    source = tmp_path / "leak.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False))
    monkeypatch.setenv("STREAMWRIGHT_SECRET_PG_DSN", secret_db)
    out, summary = tmp_path / "out", tmp_path / "summary.json"
    assert cli.main(["run", str(source), "--allow-connector", "postgres", "--output", "jsonl:%s" % out,
                     "--summary", str(summary)]) != 0
    written = ""
    for folder, _, names in os.walk(str(tmp_path)):
        for name in names:
            if folder.startswith(str(out)) or name == "summary.json":
                with open(os.path.join(folder, name)) as handle:
                    written += handle.read()
    printed = capsys.readouterr()
    for text in (written, printed.out, printed.err, caplog.text):
        assert PASSWORD not in text and secret_db not in text


def served_by_standin(monkeypatch, db):
    """
    DuckDB connections that record their statements and serve the postgres ATTACH with the stand-in `db` (no server):
    the list of those made. Skips when DuckDB's postgres extension is not installed (offline).
    """
    import duckdb
    real = duckdb.connect
    probe = real(":memory:")
    try:
        installed = probe.execute("SELECT installed FROM duckdb_extensions() WHERE extension_name = "
                                  "'postgres_scanner'").fetchone()
    finally:
        probe.close()
    if not installed or not installed[0]:
        pytest.skip("DuckDB's postgres extension is not installed (offline)")

    class Connection(object):
        def __init__(self, connection):
            self.connection, self.calls = connection, []

        def execute(self, text, parameters=None):
            self.calls.append(text)
            if text.startswith("ATTACH '' AS src (TYPE postgres"):
                assert "SECRET %s" % reader.SECRET in text and "READ_ONLY" in text
                text = "ATTACH %s AS src (READ_ONLY)" % reader.sql_string(db)
            if parameters is None:
                return self.connection.execute(text)
            return self.connection.execute(text, parameters)

        def close(self):
            self.connection.close()

    made = []

    def connect(database=":memory:", config=None):
        made.append(Connection(real(database, config=config or {})))
        return made[-1]
    monkeypatch.setattr(duckdb, "connect", connect)
    return made


def test_a_postgres_dsn_is_attached_through_a_secret_and_numerics_as_text(monkeypatch, db):
    """The postgres attach path, its ATTACH served by the stand-in (no server): statements, secret and settings."""
    made = served_by_standin(monkeypatch, db)
    dsn = "postgresql://reader:%s@db.example.com:5432/shop?options=-c%%20statement_timeout%%3D30000" % PASSWORD
    client = Database(dsn, "postgres")
    try:
        calls = [connection for connection in made if any(call.startswith("ATTACH") for call in connection.calls)]
        assert len(calls) == 1
        calls = calls[0].calls
        order = [calls.index(statement) for statement in ("LOAD postgres", "SET pg_numeric_as_varchar = true")]
        order += [next(index for index, call in enumerate(calls) if call.startswith(prefix))
                  for prefix in ("CREATE TEMPORARY SECRET", "ATTACH", "SET lock_configuration")]
        assert order == sorted(order)
        assert [call for call in calls if PASSWORD in call] == [
            "CREATE TEMPORARY SECRET %s (TYPE postgres, URI %s)" % (reader.SECRET, reader.sql_string(dsn))]
        connection = client.connection.connection
        assert connection.execute("SELECT current_setting('pg_numeric_as_varchar')").fetchone()[0] is True
        secrets = connection.execute("SELECT secret_string FROM duckdb_secrets()").fetchall()
        assert len(secrets) == 1 and "uri=redacted" in secrets[0][0] and PASSWORD not in secrets[0][0]
        assert [row["id"] for page in client.pages("SELECT id FROM public.orders ORDER BY id") for row in page] == [
            10, 11, 12]
        with pytest.raises(QueryError):
            list(client.pages("SELECT * FROM duckdb_secrets()"))
    finally:
        client.close()


# * ------
# * checks
# * ------

def check(connector, method="query", **arguments):
    return connector.check_request({"service": "database", "method": method, "arguments": arguments})


def test_valid_requests(connector):
    assert check(connector, query="SELECT * FROM public.orders WHERE updated_at >= $since AND id = ANY($ids)",
                 params={"since": "{{ window.start }}", "ids": "{{ partition.ids }}"}) == []
    assert check(connector, query="WITH a AS (SELECT 1 AS x) SELECT * FROM a") == []
    assert check(connector, query="SELECT \"update\", 'DELETE; DROP' AS s, $$a;b$$ AS d FROM t /* ; */") == []
    assert check(connector, query="SELECT * FROM unnest($xs) u, range(3) r", params={"xs": [1, 2]}) == []
    assert check(connector, "table", table="orders") == []
    assert check(connector, "table", schema="{{ partition.schema }}", table="orders", columns=["id", "updated_at"],
                 where=[{"column": "updated_at", "op": ">=", "value": "{{ window.start }}", "type": "DATE"},
                        {"column": "id", "op": "IN", "value": "{{ partition.ids }}"},
                        {"column": "deleted_at", "op": "IS NULL"}]) == []
    assert connector.check_request({"method": "query", "arguments": {"query": "SELECT 1"}}) == []  # service: database


@pytest.mark.parametrize("method,arguments,expected", [
    ("query", {}, "database.query needs `query`"),
    ("query", {"query": "SELECT 1", "limit": 3}, "database.query does not take `limit` (arguments: query, params)"),
    ("table", {}, "database.table needs `table`"),
    ("table", {"table": "t", "order_by": "id"}, "does not take `order_by` (arguments: schema, table, columns, where)"),
    ("query", {"query": "SELECT 1; SELECT 2"}, "must be a single statement"),
    ("query", {"query": "DELETE FROM t"}, "it cannot use DELETE"),
    ("query", {"query": "SELECT * FROM t; DROP TABLE t"}, "it cannot use DROP"),
    ("query", {"query": "INSERT INTO t SELECT 1"}, "it cannot use INSERT"),
    ("query", {"query": "SHOW TABLES"}, "must be a SELECT (it starts with 'SHOW')"),
    ("query", {"query": "SUMMARIZE t"}, "must be a SELECT"),
    ("query", {"query": "SELECT * FROM t WHERE day >= '{{ window.start }}'"}, "cannot hold references"),
    ("query", {"query": "SELECT '{{ secrets.pw }}'"}, "arguments cannot use secrets"),
    ("query", {"query": "SELECT $pw", "params": {"pw": "{{ secrets.pw }}"}}, "arguments cannot use secrets"),
    ("query", {"query": "SELECT * FROM t WHERE a = $a"}, "the query uses $a: declare it in `params`"),
    ("query", {"query": "SELECT 1", "params": {"b": 1}}, "`params` b is not used in the query (write $b in it)"),
    ("query", {"query": "SELECT * FROM t WHERE a = ?"}, "named parameters ($name"),
    ("query", {"query": "SELECT * FROM t WHERE a = $1", "params": {}}, "named parameters"),
    ("query", {"query": "SELECT 1", "params": ["a"]}, "`params` must be a mapping"),
    ("query", {"query": "SELECT $1x", "params": {"1x": 1}}, "`params` names must be letters, digits and _"),
    ("query", {"query": {"sql": "SELECT 1"}}, "`query` must be SQL text"),
    ("query", {"query": "  "}, "`query` must be a SELECT statement"),
    ("query", {"query": "SELECT * FROM read_csv('/etc/passwd')"}, "the query cannot call read_csv"),
    ("query", {"query": "SELECT * FROM '/etc/passwd.csv'"}, "the query cannot read files"),
    ("query", {"query": "SELECT * FROM duckdb_settings()"}, "the query cannot call duckdb_settings"),
    ("query", {"query": "SELECT * FROM my_function(1)"}, "the table function my_function (only unnest"),
    ("query", {"query": "SELECT * FROM postgres_query('src', 'SELECT 1')"}, "cannot call postgres_query"),
    ("query", {"query": "SELECT * FROM query('SELECT 1')"}, "cannot call query"),
    ("query", {"query": "SELECT getenv('HOME')"}, "cannot call getenv"),
    ("query", {"query": "SELECT * FROM memory.main.t"}, "'memory' is not it"),
    ("query", {"query": "SELECT * FROM t WHERE"}, "must be a single SELECT: syntax error"),
    ("table", {"table": ""}, "`table` '' is not a name"),
    ("table", {"table": "t", "schema": "x" * 64}, "is longer than 63 bytes"),
    ("table", {"table": "t", "columns": []}, "`columns` must be a non-empty list"),
    ("table", {"table": "t", "columns": ["{{ config.c }}"]}, "is a reference: columns are literal names"),
    ("table", {"table": "t", "where": {"id": 1}}, "`where` must be a list of conditions"),
    ("table", {"table": "t", "where": [{"column": "a", "op": "LIKE", "value": "x"}]}, "unknown `op` 'LIKE'"),
    ("table", {"table": "t", "where": [{"column": "a", "op": "IN", "value": 3}]}, "IN needs a list `value`"),
    ("table", {"table": "t", "where": [{"column": "a", "op": "="}]}, "= needs a `value`"),
    ("table", {"table": "t", "where": [{"column": "a", "op": "IS NULL", "value": 1}]}, "IS NULL takes no `value`"),
    ("table", {"table": "t", "where": [{"column": "a", "value": 1, "type": "INT; DROP"}]}, "is not a column type"),
    ("table", {"table": "t", "where": [{"column": "a", "value": 1, "cast": "x"}]}, "does not take cast"),
])
def test_requests_are_checked(connector, method, arguments, expected):
    found = check(connector, method, **arguments)
    assert any(expected in problem for problem in found), found


def test_unknown_services_and_methods_are_refused(connector):
    assert connector.check_request({"service": "admin", "method": "query", "arguments": {}}) == [
        "postgres: service 'admin' is not supported (supported: database)"]
    assert connector.check_request({"service": "database", "method": "execute", "arguments": {}}) == [
        "postgres: database.execute is not supported (supported: query, table)"]
    assert connector.check_request({"method": "query", "arguments": ["SELECT 1"]}) == [
        "postgres: `arguments` must be a mapping"]


@pytest.mark.parametrize("auth,expected", [
    ({"dsn": "{{ secrets.pg_dsn }}"}, []),
    ({"dsn": "{{secrets.pg_dsn}}", "statement_timeout": "30s"}, []),
    ({"dsn": "{{ secrets.pg_dsn }}", "statement_timeout": 90}, []),
    ({"dsn": "{{ secrets.pg_dsn }}", "statement_timeout": "{{ config.timeout }}"}, []),
    ({}, ["auth: provider 'postgres': needs `dsn` (a secret reference, e.g. dsn: \"{{ secrets.pg_dsn }}\") or the "
          "connection keys `host`, `database` and `user` (and `password`, a secret reference)"]),
    ({"dsn": "{{ secrets.pg_dsn }}", "user": "x"}, [
        "auth: provider 'postgres': use either `dsn` or the connection keys (host, port, database, user, password, "
        "sslmode, options), not both: `user`"]),
    ({"dsn": "{{ secrets.pg_dsn }}", "schema": "x"}, [
        "auth: provider 'postgres' does not support 'schema' (supported: dsn, host, port, database, dbname, user, "
        "password, sslmode, options, statement_timeout)"]),
    ({"dsn": "{{ config.dsn }}"}, ["auth: provider 'postgres': `dsn` must be one secret reference, e.g. "
                                   "dsn: \"{{ secrets.pg_dsn }}\" (a DSN holds credentials: it is never written in "
                                   "the source or its config)"]),
    ({"dsn": "postgresql://reader:{{ secrets.pw }}@db/shop"}, ["auth: provider 'postgres': `dsn` must be one secret "
                                                               "reference, e.g. dsn: \"{{ secrets.pg_dsn }}\" (a DSN "
                                                               "holds credentials: it is never written in the source "
                                                               "or its config)"]),
    ({"dsn": ""}, ["auth: provider 'postgres': `dsn` must be a secret reference, e.g. dsn: \"{{ secrets.pg_dsn }}\""]),
    ({"dsn": 3}, ["auth: provider 'postgres': `dsn` must be a secret reference, e.g. dsn: \"{{ secrets.pg_dsn }}\""]),
    ({"dsn": "{{ secrets.pg_dsn }}", "statement_timeout": "soon"}, [
        "auth: provider 'postgres': `statement_timeout`: 'soon' is not a timeout (a number of seconds, or e.g. 30s, "
        "500ms, 5min, 1h)"]),
    ({"dsn": "{{ secrets.pg_dsn }}", "statement_timeout": "{{ secrets.t }}"}, [
        "auth: provider 'postgres': `statement_timeout` cannot use secrets"]),
])
def test_auth_is_checked(connector, auth, expected):
    assert connector.check_auth(auth) == expected


def test_a_dsn_written_in_the_source_never_connects(connector, db, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing is attached")
    monkeypatch.setattr(connector_module, "Database", refuse)
    with pytest.raises(ConnectorError, match="`dsn` must come from secrets"):
        connector.connect({"dsn": "postgresql://reader:Sup3r-pw@db.example.com/shop"}, context(connector))
    with pytest.raises(ConnectorError, match="`dsn` must come from secrets"):  # (also part of a secret is not enough)
        connector.connect({"dsn": db}, context(connector, [os.path.basename(db)]))


def test_check_source_reports_problems_without_connecting(connector):
    source = pg_source(page_stream("orders", sdk("raw_orders", query="SELECT * FROM orders WHERE day >= $since",
                                                 params={"since": "{{ window.start }}"}),
                                   "SELECT record->>'id' AS id FROM raw_orders"))
    assert components.check_source(source, ["postgres"]) == []
    source["streams"][0]["requests"][0]["arguments"]["query"] = "DELETE FROM orders WHERE day >= $since"
    source["auth"]["dsn"] = "{{ config.dsn }}"
    assert components.check_source(source, ["postgres"]) == [
        "auth: provider 'postgres': `dsn` must be one secret reference, e.g. dsn: \"{{ secrets.pg_dsn }}\" (a DSN "
        "holds credentials: it is never written in the source or its config)",
        "stream 'orders': requests[0]: postgres: the query is read-only: it cannot use DELETE (a column named like one "
        "must be double-quoted, e.g. \"update\")",
        "stream 'orders': requests[0]: postgres: the query must be a SELECT (it starts with 'DELETE')"]


def test_the_lexical_scan_skips_strings_comments_and_quoted_names():
    statement, problems = check_query("SELECT 'it''s; DROP' AS a, E'x\\'; DELETE' AS b, \"drop\" -- DELETE\n"
                                      "FROM t /* ; UPDATE */;  ")
    assert problems == [] and statement.endswith("/* ; UPDATE */")
    assert check_query("SELECT is_deleted, updated_at, created_by FROM t")[1] == []
    assert check_query("SELECT 1 -- a\n; SELECT 2")[1] == [
        "the query must be a single statement (a `;` separates statements)"]


# * --------------
# * DSNs, timeouts
# * --------------

def test_dsn_passwords_and_statement_timeouts():
    assert dsn_secrets("postgresql://reader:p%40ss-word@db:5432/shop?sslmode=require") == ["p%40ss-word", "p@ss-word"]
    assert dsn_secrets("postgres://db/shop?user=reader&password=Sup3r") == ["Sup3r"]
    assert dsn_secrets("host=db user=reader password='a b\\'c' dbname=shop") == ["'a b\\'c'", "a b'c"]
    assert dsn_secrets("host=db password=Sup3r dbname=shop") == ["Sup3r"]
    assert dsn_secrets("host=db dbname=shop") == []
    assert with_statement_timeout("postgresql://db/shop", 1500) == (
        "postgresql://db/shop?options=-c%20statement_timeout%3D1500")
    assert with_statement_timeout("postgresql://db/shop?sslmode=require", 30000) == (
        "postgresql://db/shop?sslmode=require&options=-c%20statement_timeout%3D30000")
    assert with_statement_timeout("host=db dbname=shop", 300000) == (
        "host=db dbname=shop options='-c statement_timeout=300000'")
    for dsn in ("postgresql://db/shop?options=-c%20x%3D1", "host=db options='-c x=1'"):
        with pytest.raises(ValueError, match="sets `options` already"):
            with_statement_timeout(dsn, 1000)
    assert [timeout_ms(value) for value in (30, 1.5, "500ms", "30s", "5min", "1h", "2")] == [
        30000, 1500, 500, 30000, 300000, 3600000, 2000]
    for value in ("soon", True, "0ms", -1):
        with pytest.raises(ValueError):
            timeout_ms(value)


class FakeDatabase(object):
    attached = []

    def __init__(self, dsn, attach_type, page_size=None):
        FakeDatabase.attached.append((dsn, attach_type))
        raise ConnectError('cannot attach the database: IO Error: Unable to connect to Postgres at "%s": password '
                           'authentication failed for user "reader"; password=Sup3r-pw' % dsn)


def test_connect_sets_the_timeout_and_redacts_the_dsn_and_its_password(monkeypatch):
    monkeypatch.setattr(connector_module, "Database", FakeDatabase)
    FakeDatabase.attached = []
    component = PostgresConnector()
    dsn = "postgresql://reader:Sup3r-pw@db.example.com:5432/shop"
    ctx = context(component, [dsn])
    with pytest.raises(ConnectorError) as caught:
        component.connect({"dsn": dsn, "statement_timeout": "5min"}, ctx)
    target = "%s?options=-c%%20statement_timeout%%3D300000" % dsn
    assert FakeDatabase.attached == [(target, "postgres")]
    text = str(caught.value)
    assert "Sup3r" not in text and "db.example.com" not in text and "***" in text
    assert caught.value.code == "CONNECT_ERROR" and not caught.value.retryable
    for value in (dsn, target, "Sup3r-pw", "the password is Sup3r-pw"):
        assert "Sup3r" not in ctx.redact(value)
    keyword = "host=db.example.com user=reader password=Sup3r-pw dbname=shop"
    with pytest.raises(ConnectorError) as caught:
        component.connect({"dsn": keyword}, context(component, [keyword]))
    assert FakeDatabase.attached[-1] == (keyword, "postgres") and "Sup3r" not in str(caught.value)


def test_a_failing_attach_is_redacted_through_a_run(connector, tmp_path, caplog):
    dsn = str(tmp_path / "Sup3r-S3cret-pw" / "missing.duckdb")
    stream = page_stream("orders", sdk("raw_orders", query="SELECT 1 AS id"), "SELECT record->>'id' AS id "
                                                                             "FROM raw_orders")
    with pytest.raises(SourceError) as caught:
        run(pg_source(stream), dsn)
    assert "postgres: cannot connect: postgres: cannot attach the database" in str(caught.value)
    assert "Sup3r" not in str(caught.value) and "***" in str(caught.value)
    assert "Sup3r" not in caplog.text


def test_errors_map_to_connector_errors(connector):
    import duckdb
    refused = connector.error(duckdb.IOException('IO Error: Unable to connect to Postgres: Connection refused'))
    assert (refused.code, refused.retryable) == ("CONNECTION_ERROR", True)
    timeout = connector.error(duckdb.IOException("IO Error: ERROR:  canceling statement due to statement timeout"))
    assert (timeout.code, timeout.retryable) == ("STATEMENT_TIMEOUT", False)
    assert connector.error(duckdb.CatalogException("Catalog Error: Table x does not exist")).code == "QUERY_ERROR"
    assert connector.error(duckdb.Error("cannot execute DELETE in a read-only transaction")).code == "READ_ONLY"
    assert connector.error(QueryError("no")).code == "QUERY_REFUSED"
    assert connector_error(ConnectError("could not connect to server")).retryable
    assert connector.error(ValueError("x")) is None
    with pytest.raises(ValueError):
        PostgresConnector(attach_type="mysql")
    with pytest.raises(ConnectError, match="unknown attach type"):
        Database("x", attach_type="mysql")


# * -----------------------------------------------------
# * the structured form: host, port, database, user, ...
# * -----------------------------------------------------

STRUCTURED = {"host": "{{ config.pg_host }}", "port": 5432, "database": "shop", "user": "reader",
              "password": "{{ secrets.pg_password }}", "sslmode": "require",
              "options": {"connect_timeout": "10", "application_name": "streamwright"}, "statement_timeout": "30s"}
WHAT = "auth: provider 'postgres': "


def structured(**changes):
    auth = dict(STRUCTURED, **changes)
    return dict((key, value) for key, value in auth.items() if value is not None)


@pytest.mark.parametrize("auth,expected", [
    (structured(), []),
    (structured(port=None, sslmode=None, options=None, statement_timeout=None, password=None), []),
    (structured(database=None, dbname="{{ config.db }}", port="{{ config.pg_port }}", sslmode="{{ config.ssl }}",
                host="db-{{ config.env }}.example.com"), []),
    (structured(port="6543", sslmode="verify-full", options={"target_session_attrs": "read-only", "keepalives": 1,
                                                             "options": "-c search_path=shop"}), []),
    (structured(dsn="{{ secrets.pg_dsn }}"), [
        WHAT + "use either `dsn` or the connection keys (host, port, database, user, password, sslmode, options), not "
        "both: `host`, `port`, `database`, `user`, `password`, `sslmode`, `options`"]),
    ({"dsn": "{{ secrets.pg_dsn }}", "password": "{{ secrets.pg_password }}"}, [
        WHAT + "use either `dsn` or the connection keys (host, port, database, user, password, sslmode, options), not "
        "both: `password`"]),
    (structured(host=None, user=None), [WHAT + "needs `host`", WHAT + "needs `user`"]),
    (structured(database=None), [WHAT + "needs `database` (or its alias `dbname`)"]),
    (structured(dbname="shop"), [WHAT + "use `database` or its alias `dbname`, not both"]),
    ({"password": "{{ secrets.pg_password }}"}, [WHAT + "needs `host`", WHAT + "needs `user`",
                                                 WHAT + "needs `database` (or its alias `dbname`)"]),
    (structured(host="", user=5), [WHAT + "`host` must be text or a reference, e.g. \"{{ config.pg_host }}\"",
                                   WHAT + "`user` must be text or a reference, e.g. \"{{ config.pg_user }}\""]),
    (structured(password="{{ config.pg_password }}"), [
        WHAT + "`password` must be one secret reference, e.g. password: \"{{ secrets.pg_password }}\" (a password "
        "is never written in the source or its config)"]),
    (structured(password="pw-{{ secrets.pg_password }}"), [
        WHAT + "`password` must be one secret reference, e.g. password: \"{{ secrets.pg_password }}\" (a password "
        "is never written in the source or its config)"]),
    (structured(password=""), [WHAT + "`password` must be a secret reference, e.g. password: "
                                      "\"{{ secrets.pg_password }}\""]),
    (structured(sslmode="required"), [
        WHAT + "`sslmode` must be one of: disable, allow, prefer, require, verify-ca, verify-full; not 'required'"]),
    (structured(port="54x"), [WHAT + "`port` must be a port number (1-65535), e.g. 5432; not '54x'"]),
    (structured(port=70000), [WHAT + "`port` must be a port number (1-65535), e.g. 5432; not 70000"]),
    (structured(port=True), [WHAT + "`port` must be a port number (1-65535), e.g. 5432; not True"]),
    (structured(options="connect_timeout=10"), [
        WHAT + "`options` must be a mapping of libpq connection parameters to their values, e.g. {connect_timeout: "
        "\"10\", application_name: streamwright}"]),
    (structured(options={"password": "x", "passfile": "/p", "sslpassword": "x"}), [
        WHAT + "`options` cannot set `password`: the only credential is `password`, a secret reference",
        WHAT + "`options` cannot set `passfile`: the only credential is `password`, a secret reference",
        WHAT + "`options` cannot set `sslpassword`: the only credential is `password`, a secret reference"]),
    (structured(options={"dbname": "evil", "host": "evil", "dsn": "x"}), [
        WHAT + "`options` cannot set `dbname`: use the auth key `database`",
        WHAT + "`options` cannot set `host`: use the auth key `host`",
        WHAT + "`options` cannot set `dsn`: a DSN is the auth key `dsn` (a secret), instead of the connection keys"]),
    (structured(options={"connect_timeout=1 dbname": "x", "a b": "x", "it's": "x", "Port": "1"}), [
        WHAT + "`options` key 'connect_timeout=1 dbname' is not a libpq parameter name (lowercase letters, digits "
        "and _)",
        WHAT + "`options` key 'a b' is not a libpq parameter name (lowercase letters, digits and _)",
        WHAT + "`options` key \"it's\" is not a libpq parameter name (lowercase letters, digits and _)",
        WHAT + "`options` key 'Port' is not a libpq parameter name (lowercase letters, digits and _)"]),
    (structured(options={"replication": "database", "sslkeylogfile": "/tmp/k"}), [
        WHAT + "`options` key `replication` is not a libpq connection parameter it may set (e.g. connect_timeout, "
        "application_name, target_session_attrs, sslrootcert)",
        WHAT + "`options` key `sslkeylogfile` is not a libpq connection parameter it may set (e.g. connect_timeout, "
        "application_name, target_session_attrs, sslrootcert)"]),
    (structured(options={"application_name": "{{ secrets.x }}", "connect_timeout": ["10"], "keepalives": True}), [
        WHAT + "`options.application_name` cannot use secrets (`password` is the only secret)",
        WHAT + "`options.connect_timeout` must be text (or a whole number), e.g. \"10\"",
        WHAT + "`options.keepalives` must be text (or a whole number), e.g. \"10\""]),
    (structured(statement_timeout="soon"), [
        WHAT + "`statement_timeout`: 'soon' is not a timeout (a number of seconds, or e.g. 30s, 500ms, 5min, 1h)"]),
])
def test_the_structured_auth_is_checked(connector, auth, expected):
    assert connector.check_auth(auth) == expected


def parse_conninfo(text):
    """
    A libpq keyword/value connection string as {keyword: value}, by libpq's own rules (conninfo_parse): `keyword =
    value` pairs apart by whitespace, a value single-quoted (a backslash escapes the next character) or up to the
    next whitespace. AssertionError for text libpq refuses, and for a keyword given twice.
    """
    found, index, size = {}, 0, len(text)
    while True:
        while index < size and text[index].isspace():
            index += 1
        if index >= size:
            return found
        start = index
        while index < size and text[index] != "=" and not text[index].isspace():
            index += 1
        keyword = text[start:index]
        while index < size and text[index].isspace():
            index += 1
        assert index < size and text[index] == "=", "missing \"=\" after %r" % keyword
        index += 1
        while index < size and text[index].isspace():
            index += 1
        value = []
        if index < size and text[index] == "'":
            index += 1
            while True:
                assert index < size, "unterminated quoted string"
                if text[index] == "\\" and index + 1 < size:
                    value.append(text[index + 1])
                    index += 2
                elif text[index] == "'":
                    index += 1
                    break
                else:
                    value.append(text[index])
                    index += 1
        else:
            while index < size and not text[index].isspace():
                if text[index] == "\\" and index + 1 < size:
                    index += 1
                value.append(text[index])
                index += 1
        assert keyword not in found, "%r given twice" % keyword
        found[keyword] = "".join(value)


EVIL = ["x' dbname=evil", "x\\' user=postgres password=pw", "a b=c", "'", "\\", "it's \\'quoted\\'", "", " = ",
        "x\\", "host=evil port=1"]


def test_the_structured_connection_string_quotes_and_escapes_every_value():
    assert structured_conninfo("db.example.com", "shop", "reader") == (
        "host='db.example.com' port='5432' dbname='shop' user='reader'")
    assert structured_conninfo("db", "shop", "reader", port=6543, password="pw", sslmode="require",
                               options={"connect_timeout": 10, "application_name": "streamwright"},
                               statement_timeout=30000) == (
        "host='db' port='6543' dbname='shop' user='reader' password='pw' sslmode='require' connect_timeout='10' "
        "application_name='streamwright' options='-c statement_timeout=30000'")
    # the timeout joins the server options the source sets
    assert parse_conninfo(structured_conninfo("db", "shop", "reader", options={"options": "-c search_path=shop"},
                                              statement_timeout=1500))["options"] == (
        "-c search_path=shop -c statement_timeout=1500")
    assert conninfo_value("it's a \\ test") == "'it\\'s a \\\\ test'"
    # no value - host, dbname, user, password or an option - can add or replace a keyword
    for value in EVIL:
        text = structured_conninfo(value, value, value, password=value, sslmode="require",
                                   options={"application_name": value}, statement_timeout=1000)
        assert parse_conninfo(text) == {"host": value, "port": "5432", "dbname": value, "user": value,
                                        "password": value, "sslmode": "require", "application_name": value,
                                        "options": "-c statement_timeout=1000"}, (value, text)
    # (what quoting prevents: the same values pasted unquoted become keywords of their own)
    assert parse_conninfo("host=x dbname=evil user=reader") == {"host": "x", "dbname": "evil", "user": "reader"}
    for keyword in ("a b", "x=1", "it's", ""):
        with pytest.raises(ValueError, match="not a libpq connection parameter name"):
            conninfo([(keyword, "v")])
    with pytest.raises(ValueError, match="NUL"):
        conninfo([("host", "x\x00y")])


class StructuredDatabase(object):
    attached = []

    def __init__(self, target, attach_type, page_size=None):
        StructuredDatabase.attached.append((target, attach_type))
        raise ConnectError('cannot attach the database: IO Error: Unable to connect to Postgres at "%s": FATAL: '
                           'password authentication failed (%s)' % (target, parse_conninfo(target).get("password")))


def test_connect_builds_the_structured_connection_string_and_redacts_it(monkeypatch):
    monkeypatch.setattr(connector_module, "Database", StructuredDatabase)
    StructuredDatabase.attached = []
    component = PostgresConnector()
    password = "Sup3r 'pw' \\ host=evil"
    auth = structured(host="x' dbname=evil", password=password, statement_timeout="5min")
    ctx = context(component, [password])
    with pytest.raises(ConnectorError) as caught:
        component.connect(auth, ctx)
    [(target, attach_type)] = StructuredDatabase.attached
    assert attach_type == "postgres"
    assert parse_conninfo(target) == {"host": "x' dbname=evil", "port": "5432", "dbname": "shop", "user": "reader",
                                      "password": password, "sslmode": "require", "connect_timeout": "10",
                                      "application_name": "streamwright", "options": "-c statement_timeout=300000"}
    text = str(caught.value)
    assert caught.value.code == "CONNECT_ERROR" and "***" in text
    assert "Sup3r" not in text and "evil" not in text  # the connection string is redacted whole, with its password
    for value in (target, password, conninfo_value(password), "password=%s" % conninfo_value(password)):
        assert "Sup3r" not in ctx.redact(value)
    # without a password, and the `dbname` alias
    auth = structured(database=None, dbname="shop", password=None, options=None, statement_timeout=None)
    with pytest.raises(ConnectorError):
        component.connect(auth, context(component))
    assert parse_conninfo(StructuredDatabase.attached[-1][0]) == {
        "host": "{{ config.pg_host }}", "port": "5432", "dbname": "shop", "user": "reader", "sslmode": "require"}


def test_a_structured_password_written_in_the_source_never_connects(connector, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing is attached")
    monkeypatch.setattr(connector_module, "Database", refuse)
    for component in (connector, PostgresConnector()):
        with pytest.raises(ConnectorError, match="`password` must come from secrets") as caught:
            component.connect(structured(password="Sup3r-pw"), context(component))
        assert "Sup3r" not in str(caught.value)
        with pytest.raises(ConnectorError) as caught:  # problems are redacted too
            component.connect(structured(password="Sup3r-pw", sslmode="bad"), context(component, ["Sup3r-pw"]))
        assert "`sslmode` must be one of" in str(caught.value) and "Sup3r" not in str(caught.value)


def test_the_structured_form_is_attached_through_a_secret_with_an_empty_path(monkeypatch, db):
    """The postgres attach of the structured form, served by the stand-in: one temporary secret, its URI the string."""
    made = served_by_standin(monkeypatch, db)
    component = PostgresConnector()
    password = "Sup3r-S3cret-pw"
    ctx = context(component, [password])
    client = component.connect(structured(host="db.example.com", user="release", password=password), ctx)
    try:
        [calls] = [connection.calls for connection in made if any(call.startswith("ATTACH")
                                                                   for call in connection.calls)]
        [create] = [call for call in calls if call.startswith("CREATE TEMPORARY SECRET")]
        target = structured_conninfo("db.example.com", "shop", "release", password=password, sslmode="require",
                                     options={"connect_timeout": "10", "application_name": "streamwright"},
                                     statement_timeout=30000)
        assert create == "CREATE TEMPORARY SECRET %s (TYPE postgres, URI %s)" % (reader.SECRET,
                                                                                reader.sql_string(target))
        assert [call for call in calls if "db.example.com" in call or "release" in call or password in call] == [
            create]
        assert "ATTACH '' AS src (TYPE postgres, SECRET %s, READ_ONLY)" % reader.SECRET in calls
        secrets = client.connection.connection.execute("SELECT secret_string FROM duckdb_secrets()").fetchall()
        assert len(secrets) == 1 and "uri=redacted" in secrets[0][0]
        assert password not in secrets[0][0] and "db.example.com" not in secrets[0][0]
        assert ctx.redact(target) == "***" and password not in ctx.redact(create)
        assert [row["id"] for page in component.request(client, {"method": "query", "arguments": {
            "query": "SELECT id FROM public.orders ORDER BY id"}}, ctx) for row in page] == [10, 11, 12]
    finally:
        client.close()


def test_the_structured_form_reads_a_stand_in(connector, db):
    """A stand-in attaches the file `database`; the rest of the path is the same."""
    auth = structured(host="localhost", database=db, password=PASSWORD)
    ctx = context(connector, [PASSWORD])
    client = connector.connect(auth, ctx)
    try:
        assert [row["id"] for page in connector.request(client, {"method": "query", "arguments": {
            "query": "SELECT id FROM public.orders ORDER BY id"}}, ctx) for row in page] == [10, 11, 12]
    finally:
        client.close()


# * --------------------
# * runs: SourceRunner
# * --------------------

# (total as text: steps read JSON numbers as doubles, text casts exactly)
ORDERS = ("SELECT id, customer_id, total::VARCHAR AS total, updated_at FROM public.orders "
          "WHERE updated_at >= $since AND updated_at < $until + INTERVAL 1 DAY")


def orders_stream(**extra):
    return page_stream("orders", sdk("raw_orders", query=ORDERS, params={"since": "{{ window.start }}",
                                                                         "until": "{{ window.end }}"}),
                       "SELECT (record->>'id')::BIGINT AS id, (record->>'total')::DECIMAL(18,2) AS total, "
                       "(record->>'updated_at')::TIMESTAMPTZ::DATE AS day, window_start FROM raw_orders",
                       primary_key=["id"], **extra)


def test_windows_bind_their_days_and_bookmarks_advance(connector, db, caplog):
    caplog.set_level(logging.INFO, logger="streamwright.network")
    metrics = RunMetrics()
    output = run(pg_source(orders_stream(incremental={"cursor_field": "day", "start": str(TODAY - 3 * DAY),
                                                      "window": "1d"})), db, metrics=metrics)
    assert records(output, "orders") == [
        {"id": 10, "total": decimal.Decimal("12.50"), "day": str(TODAY - 2 * DAY),
         "window_start": str(TODAY - 2 * DAY)},
        {"id": 11, "total": decimal.Decimal(BIG), "day": str(TODAY - DAY), "window_start": str(TODAY - DAY)}]
    assert output.states[-1]["bookmarks"]["orders"] == {"{}": str(TODAY - DAY)}
    stats = metrics.summary()["streams"][0]
    assert stats["requests"] == {"raw_orders": 4}  # one query per day: TODAY-3 .. TODAY
    lines = [record for record in caplog.records if getattr(record, "event", None) == "db_query"]
    assert len(lines) == 4 and [line.records for line in lines] == [0, 1, 1, 0]
    assert lines[1].getMessage() == ("stream 'orders', request 'raw_orders', window %s..%s: postgres query: 1 row(s), "
                                     "%.2f s" % (TODAY - 2 * DAY, TODAY - 2 * DAY, lines[1].duration_ms / 1000.0))
    assert (lines[1].connector, lines[1].call) == ("postgres", "database.query")
    assert db not in caplog.text


def test_batches_of_ids_are_bound_as_lists(connector, db):
    lines = sdk("raw_lines", query="SELECT * FROM public.order_lines WHERE order_id = ANY($ids::BIGINT[])",
                params={"ids": "{{ partition.ids }}"})
    lines["partitions"] = [{"name": "ids", "from": "raw_orders", "field": "id", "batch_size": 2}]
    stream = {"name": "lines", "requests": [sdk("raw_orders", query="SELECT id FROM public.orders ORDER BY id"),
                                            lines],
              "transform": {"mode": "run", "steps": [{"name": "lines", "select": (
                  "SELECT (record->>'order_id')::BIGINT AS order_id, (record->>'line_number')::INTEGER AS line, "
                  "(record->>'unit_price')::DECIMAL(12,2) AS price, json_array_length(partition->'ids') AS batch "
                  "FROM raw_lines")}]},
              "export": {"lines": {"step": "lines", "primary_key": ["order_id", "line"]}}}
    metrics = RunMetrics()
    output = run(pg_source(stream), db, metrics=metrics)
    assert sorted((record["order_id"], record["line"], record["price"], record["batch"])
                  for record in records(output, "lines")) == [
        (10, 1, decimal.Decimal("6.25"), 2), (10, 2, 0, 2), (11, 1, decimal.Decimal("99.99"), 2), (12, 1, 1, 1)]
    assert metrics.summary()["streams"][0]["requests"] == {"raw_orders": 1, "raw_lines": 2}


def test_the_table_method_runs_with_partitions(connector, db):
    stream = page_stream("customers", sdk("raw_customers", "table", schema="public", table="customers",
                                          columns=["id", "status"],
                                          where=[{"column": "status", "value": "{{ partition.status }}"}]),
                         "SELECT (record->>'id')::BIGINT AS id, record->>'status' AS status FROM raw_customers",
                         primary_key=["id"], partitions=[{"name": "status", "values": ["closed", "active"]}])
    assert records(run(pg_source(stream), db), "customers") == [
        {"id": 2, "status": "closed"}, {"id": 1, "status": "active"}, {"id": 3, "status": "active"}]


def test_a_failing_query_fails_its_partition_without_retries(connector, db):
    stream = page_stream("orders", sdk("raw_orders", query="SELECT * FROM public.missing"),
                         "SELECT record->>'id' AS id FROM raw_orders")
    with pytest.raises(SourceError, match="Table with name missing does not exist"):
        run(pg_source(stream), db)


# * -------------------------
# * the example and the CLI
# * -------------------------

def test_the_postgres_demo_runs_against_a_stand_in(connector, tmp_path, monkeypatch, caplog):
    """The demo's structured auth: the stand-in attaches the file `database` (config), its password a secret."""
    today = datetime.date.today()
    dsn = standin(tmp_path / "shop.duckdb", today)
    monkeypatch.setenv("STREAMWRIGHT_SECRET_PG_PASSWORD", PASSWORD)
    out, summary = tmp_path / "out", tmp_path / "summary.json"
    assert cli.main(["run", EXAMPLE, "--set", "start_date=%s" % (today - 3 * DAY), "--set", "pg_database=%s" % dsn,
                     "--allow-connector", "postgres", "--output", "jsonl:%s" % out, "--summary", str(summary)]) == 0
    written = {}
    for name in os.listdir(str(out)):
        if name != "state.json":
            with open(str(out / name)) as stream:
                written[name.split(".")[0]] = [json.loads(line, parse_float=decimal.Decimal) for line in stream]
    assert sorted(written) == ["customers", "order_lines", "orders"]
    assert [(record["customer_id"], record["email"]) for record in written["customers"]] == [
        (1, "ann@example.com"), (3, None)]
    assert [(record["order_id"], record["total"], record["updated_on"]) for record in written["orders"]] == [
        (10, decimal.Decimal("12.5"), str(today - 2 * DAY)), (11, decimal.Decimal(BIG), str(today - DAY))]
    assert sorted((record["order_id"], record["line_number"]) for record in written["order_lines"]) == [
        (10, 1), (10, 2), (11, 1)]
    with open(str(summary)) as stream:
        text = stream.read()
    streams = dict((item["name"], item) for item in json.loads(text)["streams"])
    assert streams["order_lines"]["requests"] == {"raw_orders": 4, "raw_lines": 1}
    assert PASSWORD not in text and PASSWORD not in caplog.text


def test_the_postgres_demo_needs_the_connector_allowed_and_its_secret(connector, tmp_path, monkeypatch, caplog):
    out = "jsonl:%s" % (tmp_path / "out")
    monkeypatch.setenv("STREAMWRIGHT_SECRET_PG_PASSWORD", PASSWORD)
    pg_database = "pg_database=%s" % standin(tmp_path / "shop.duckdb")
    assert cli.main(["run", EXAMPLE, "--set", pg_database, "--allow-connector", "files", "--output", out]) == 2
    assert "connector 'postgres' is not in the allowed list" in caplog.text
    caplog.clear()
    monkeypatch.delenv("STREAMWRIGHT_SECRET_PG_PASSWORD")
    assert cli.main(["run", EXAMPLE, "--set", pg_database, "--allow-connector", "postgres", "--output", out]) != 0
    assert "pg_password" in caplog.text


def test_streamwright_validate_and_connectors(capsys, tmp_path):
    assert cli.main(["validate", EXAMPLE]) == 0
    assert cli.main(["connectors"]) == 0
    assert "postgres" in capsys.readouterr().out.split()
    import yaml
    with open(os.path.join(EXAMPLE, "source.yaml")) as stream:
        document = yaml.safe_load(stream)
    with open(os.path.join(EXAMPLE, "streams", "orders.yaml")) as stream:
        orders = yaml.safe_load(stream)
    orders["requests"][0]["arguments"]["query"] = "SELECT * FROM public.orders; DELETE FROM public.orders"
    document["streams"] = [dict(orders, name="orders")]
    document["auth"]["password"] = "Sup3r-pw"  # a literal passes the static check (see connect)
    bad = tmp_path / "source.yaml"
    bad.write_text(yaml.safe_dump(document, sort_keys=False))
    assert cli.main(["validate", str(bad)]) == 1
    text = capsys.readouterr().out
    assert "must be a single statement" in text and "it cannot use DELETE" in text


# * ---------------------------------------------------
# * a real Postgres (only with STREAMWRIGHT_TEST_PG_DSN set)
# * ---------------------------------------------------

@pytest.mark.skipif(not os.environ.get("STREAMWRIGHT_TEST_PG_DSN"), reason="set STREAMWRIGHT_TEST_PG_DSN to a throwaway Postgres")
def test_a_real_postgres():
    import duckdb
    dsn = os.environ["STREAMWRIGHT_TEST_PG_DSN"]
    name = "streamwright_test_%s" % uuid.uuid4().hex[:8]
    setup = duckdb.connect()
    setup.execute("INSTALL postgres")
    setup.execute("LOAD postgres")
    setup.execute("ATTACH %s AS pg (TYPE postgres)" % reader.sql_string(dsn))
    setup.execute("CREATE TABLE pg.public.%s (id BIGINT, name VARCHAR, amount DECIMAL(14,2), day DATE)" % name)
    setup.execute("INSERT INTO pg.public.%s VALUES (1, 'a', 1.50, DATE '2026-10-01'), (2, ?, 123456789012.34, "
                  "DATE '2026-10-02')" % name, [INJECTION])
    # an unconstrained numeric (DuckDB would create numeric(18,3)): its value must not become a DOUBLE
    setup.execute("CALL postgres_execute('pg', 'CREATE TABLE public.%s_n (raw numeric, wide numeric(50,2)); INSERT "
                  "INTO public.%s_n VALUES (%s, 123456789012345678901234567890.12)')" % (name, name, BIG))
    setup.execute("DETACH pg")  # (or DuckDB's cached schema hides the table from the connector's own attach)
    component = PostgresConnector()
    ctx = context(component, [dsn])
    client = component.connect({"dsn": dsn, "statement_timeout": "30s"}, ctx)
    try:
        found = [record for page in component.request(client, {"method": "query", "arguments": {
            "query": "SELECT * FROM public.%s WHERE day >= $since AND name = $name" % name,
            "params": {"since": datetime.date(2026, 10, 1), "name": INJECTION}}}, ctx) for record in page]
        assert found == [{"id": 2, "name": INJECTION, "amount": decimal.Decimal("123456789012.34"),
                          "day": "2026-10-02"}]
        assert [record for page in component.request(client, {"method": "query", "arguments": {
            "query": "SELECT * FROM public.%s_n" % name}}, ctx) for record in page] == [
            {"raw": BIG, "wide": "123456789012345678901234567890.12"}]
        # the DSN is in a secret, not the attach path; and the system views are refused anyway
        assert client.connection.execute("SELECT path FROM duckdb_databases() WHERE database_name = 'src'"
                                         ).fetchone()[0] in ("", None)
        for statement in LEAKS[:3]:
            with pytest.raises(ConnectorError) as caught:
                list(component.request(client, {"method": "query", "arguments": {"query": statement}}, ctx))
            assert caught.value.code == "QUERY_REFUSED"
        assert [record["id"] for page in component.request(client, {"method": "table", "arguments": {
            "table": name, "columns": ["id"], "where": [{"column": "id", "op": "IN", "value": ["1"],
                                                         "type": "BIGINT"}]}}, ctx) for record in page] == [1]
        with pytest.raises(Exception) as caught:
            client.connection.execute("DELETE FROM public.%s" % name)
        assert component.error(caught.value).code == "READ_ONLY"
        with pytest.raises(ConnectorError, match="it cannot use DELETE"):
            list(component.request(client, {"method": "query", "arguments": {
                "query": "DELETE FROM public.%s" % name}}, ctx))
    finally:
        client.close()
        setup.execute("ATTACH %s AS pg (TYPE postgres)" % reader.sql_string(dsn))
        assert setup.execute("SELECT count(*) FROM pg.public.%s" % name).fetchone()[0] == 2
        setup.execute("DROP TABLE pg.public.%s" % name)
        setup.execute("DROP TABLE pg.public.%s_n" % name)
        setup.close()


@pytest.mark.skipif(not os.environ.get("STREAMWRIGHT_TEST_PG_HOST"), reason="set STREAMWRIGHT_TEST_PG_HOST, STREAMWRIGHT_TEST_PG_USER and "
                    "STREAMWRIGHT_TEST_PG_DATABASE (and STREAMWRIGHT_TEST_PG_PASSWORD) to a throwaway Postgres")
def test_a_real_postgres_with_the_structured_form(caplog):
    env = os.environ
    database, user = env["STREAMWRIGHT_TEST_PG_DATABASE"], env["STREAMWRIGHT_TEST_PG_USER"]
    name = "streamwright's test x=1 dbname=evil"
    auth = {"host": env["STREAMWRIGHT_TEST_PG_HOST"], "port": env.get("STREAMWRIGHT_TEST_PG_PORT") or 5432, "database": database,
            "user": user, "sslmode": env.get("STREAMWRIGHT_TEST_PG_SSLMODE") or "prefer",
            "options": {"application_name": name, "connect_timeout": "10"}, "statement_timeout": "30s"}
    password = env.get("STREAMWRIGHT_TEST_PG_PASSWORD")
    if password:
        auth["password"] = password
    component = PostgresConnector()
    ctx = context(component, [password] if password else [])
    client = component.connect(auth, ctx)
    try:
        assert client.connection.execute("SELECT path FROM duckdb_databases() WHERE database_name = 'src'"
                                         ).fetchone()[0] in ("", None)
        assert list(component.request(client, {"method": "query", "arguments": {"query": "SELECT 1 AS one"}},
                                      ctx)) == [[{"one": 1}]]
        # every value arrived as itself: the application name with its quote, spaces and `=`, the timeout
        assert client.connection.execute(
            "SELECT * FROM postgres_query('src', 'SELECT current_setting(''application_name'') AS a, "
            "current_setting(''statement_timeout'') AS t, current_database() AS d, current_user AS u')").fetchone() == (
            name, "30s", database, user)
    finally:
        client.close()
    # a database name holding a quote and a keyword is one (missing) database, not a second keyword
    with pytest.raises(ConnectorError, match="does not exist") as caught:
        component.connect(dict(auth, database=database + "' dbname=postgres"), ctx)
    if password:
        assert password not in str(caught.value) and password not in caplog.text
