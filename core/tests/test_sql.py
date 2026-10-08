import datetime
import json
import os
import time
from decimal import Decimal

import pytest
import yaml

from streamwright.core import cli
from streamwright.core.engine import sql
from streamwright.core.engine.runner import SourceError, SourceRunner, partition_key
from streamwright.core.runtime.testing import MemoryOutput, page_stream

TODAY = datetime.date(2026, 10, 3)

CAMPAIGNS = [
    {"campaign": {"id": "11", "name": "O'Brien", "status": "ENABLED"},
     "metrics": {"clicks": "40", "impressions": "1000", "cost_micros": "12345678"}, "segments": {"date": "2026-10-02"}},
    {"campaign": {"id": "12", "name": "Generic", "status": "PAUSED"}, "segments": {"date": "2026-10-02"}},
]

SELECT = """
SELECT partition->>'account_id'                                      AS account_id,
       record->>'$.campaign.id'                                      AS campaign_id,
       record->>'$.campaign.name'                                    AS campaign_name,
       CASE record->>'$.campaign.status' WHEN 'ENABLED' THEN 'active' ELSE 'paused' END AS status,
       (record->>'$.segments.date')::DATE                            AS date,
       coalesce((record->>'$.metrics.clicks')::BIGINT, 0)            AS clicks,
       coalesce((record->>'$.metrics.impressions')::BIGINT, 0)       AS impressions,
       round(coalesce((record->>'$.metrics.cost_micros')::BIGINT, 0) / 1e6, 2) AS cost,
       round(clicks / nullif(impressions, 0), 4)                     AS ctr,
       config->>'currency'                                           AS currency,
       window_start, window_end
FROM records
"""


INCREMENTAL = {"cursor_field": "date", "start": "2026-10-02", "window": "1d"}


def source(api, request=None, select=SELECT, primary_key=("account_id", "campaign_id", "date"), incremental=INCREMENTAL,
           **stream):
    """A page-mode stream `performance`: its request `records` (GET /campaigns per account, and day), one step."""
    params = {"account": "{{ partition.account_id }}"}
    if incremental:
        stream["incremental"] = dict(incremental)
        params["day"] = "{{ window.start }}"
    stream.setdefault("partitions", [{"name": "account_id", "values": "{{ config.account_ids }}"}])
    return {"kind": "source", "name": "demo", "auth": {"type": "bearer", "token": "{{ secrets.token }}"},
            "http": {"base_url": api.url}, "streams": [page_stream(
                "performance", request or {"http": {"path": "/campaigns", "params": params}}, select,
                records={"path": "data"}, primary_key=primary_key, **stream)]}


def shaped(sandbox, query, records, scope=None):
    """The records a step makes from one page of `records` (the table of its request, `records`)."""
    sandbox.create_raw("records")
    text, _, _ = sandbox.parse(query, ["records"])
    sandbox.run_page("records", records, scope or {}, [("shaped", text, None, None)])
    return list(sandbox.table_records("shaped", sandbox.table_columns("shaped")))


def problem(query):
    """The first problem sql.check_source finds in a step that reads one request, `records`, or None."""
    found = sql.check_source({"streams": [page_stream("shaped", {"http": {"path": "/x"}}, query)]})
    return found[0][1] if found else None


def run(source_, config=None, streams=None):
    output = MemoryOutput()
    config = dict({"account_ids": ["a1"], "currency": "USD"}, **(config or {}))
    SourceRunner(source_, config, {"token": "secret-token-1"}, output=output, today=TODAY,
                 sleep=lambda seconds: None).run(streams)
    return output


def test_steps_shape_each_page(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": CAMPAIGNS})
    output = run(source(api))
    records = [record for _, record in output.records]
    assert records[:2] == [
        {"account_id": "a1", "campaign_id": "11", "campaign_name": "O'Brien", "status": "active",
         "date": "2026-10-02", "clicks": 40, "impressions": 1000, "cost": 12.35, "ctr": 0.04, "currency": "USD",
         "window_start": "2026-10-02", "window_end": "2026-10-02"},
        {"account_id": "a1", "campaign_id": "12", "campaign_name": "Generic", "status": "paused",
         "date": "2026-10-02", "clicks": 0, "impressions": 0, "cost": 0.0, "ctr": None, "currency": "USD",
         "window_start": "2026-10-02", "window_end": "2026-10-02"}]  # missing metrics are null, then 0
    assert len(records) == 4  # two daily windows (2026-10-02 and today)
    schema, keys = output.schemas["performance"]
    assert keys == ["account_id", "campaign_id", "date"]
    assert schema["properties"]["date"] == {"type": ["null", "string"], "format": "date"}
    assert schema["properties"]["clicks"] == {"type": ["null", "integer"]}
    assert schema["properties"]["cost"] == {"type": ["null", "number"]}
    assert list(schema["properties"])[:3] == ["account_id", "campaign_id", "campaign_name"]
    assert output.states[-1]["bookmarks"]["performance"] == {partition_key({"account_id": "a1"}): "2026-10-02"}


def test_steps_read_exploded_records_and_supply_partitions(api):
    api.routes[("GET", "/accounts")] = lambda request: (200, {"data": [
        {"id": "a1", "campaigns": [{"id": "11"}, {"id": "12"}]}, {"id": "a2", "campaigns": [{"id": "21"}]}]})
    api.routes[("GET", "/ads")] = lambda request: (200, {"data": [
        {"ad": request["params"]["account"] + "/" + request["params"]["campaign"]}]})
    accounts = page_stream("campaigns", {"http": {"path": "/accounts"}},
                           "SELECT record->>'$.id' AS campaign_id, partition->>'x' AS nothing FROM records",
                           records={"path": "data", "explode": "campaigns"})
    ads = page_stream("ads", {"http": {"path": "/ads", "params": {"account": "x",
                                                                  "campaign": "{{ partition.campaign_id }}"}}},
                      "SELECT record->>'ad' AS ad FROM records", records={"path": "data"},
                      partitions=[{"from_stream": "campaigns", "fields": ["campaign_id"]}])
    doc = source(api)
    doc["streams"] = [accounts, ads]
    output = run(doc, streams=["ads"])
    assert [record for stream, record in output.records if stream == "campaigns"] == [
        {"campaign_id": "11", "nothing": None}, {"campaign_id": "12", "nothing": None},
        {"campaign_id": "21", "nothing": None}]
    assert [record for stream, record in output.records if stream == "ads"] == [
        {"ad": "x/11"}, {"ad": "x/12"}, {"ad": "x/21"}]


def test_step_values_become_plain_data(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"id": 1}]})
    query = """SELECT 1.50::DECIMAL(10, 2) AS amount, DATE '2026-10-02' AS day,
                      TIMESTAMPTZ '2026-09-18 09:07:20+05:30' AS changed, record AS raw, record->'$.missing' AS gone,
                      {'a': [1, 2], 'b': DATE '2026-10-01'} AS nested, ['x', 'y'] AS items,
                      '00000000-0000-0000-0000-000000000001'::UUID AS uid, 'ab'::BLOB AS bytes,
                      INTERVAL 90 SECOND AS waited, true AS ok,
                      '12345678901234567890.123456'::DECIMAL(38, 6) AS precise, (2::HUGEINT ** 100)::HUGEINT AS huge,
                      [0.10::DECIMAL(9, 2)] AS cents
               FROM records"""
    doc = source(api, select=query, primary_key=[], incremental=None)
    output = run(doc)
    record = output.records[0][1]
    assert record == {
        "amount": Decimal("1.50"), "day": "2026-10-02", "changed": "2026-09-18T03:37:20+00:00", "raw": {"id": 1},
        "gone": None, "nested": {"a": [1, 2], "b": "2026-10-01"}, "items": ["x", "y"],
        "uid": "00000000-0000-0000-0000-000000000001", "bytes": "YWI=", "waited": 90.0, "ok": True,
        "precise": Decimal("12345678901234567890.123456"), "huge": 2 ** 100, "cents": [Decimal("0.10")]}
    # DECIMAL values are exact (decimal.Decimal), integers Python's ints, HUGEINT too
    assert [type(record[name]) for name in ("amount", "precise", "huge")] == [Decimal, Decimal, int]
    assert type(record["cents"][0]) is Decimal
    properties = output.schemas["performance"][0]["properties"]
    assert (properties["raw"], properties["nested"], properties["items"], properties["changed"]["format"],
            properties["ok"], properties["amount"], properties["huge"]) == (
        {}, {"type": ["null", "object"]}, {"type": ["null", "array"]}, "date-time", {"type": ["null", "boolean"]},
        {"type": ["null", "number"]}, {"type": ["null", "integer"]})


@pytest.mark.parametrize("query,expected", [
    ("SELECT * FROM read_csv('/etc/hosts')", "table function(s) read_csv are not allowed"),
    ("SELECT * FROM read_json('https://example.com/x.json')", "table function(s) read_json are not allowed"),
    ("SELECT * FROM enable_logging(storage := 'stdout')", "table function(s) enable_logging are not allowed"),
    ("SELECT * FROM duckdb_settings()", "table function(s) duckdb_settings are not allowed"),
    ("SELECT current_setting('temp_directory') AS t FROM records", "function(s) current_setting are not allowed"),
    ("SELECT * FROM information_schema.tables", "can only read records, shaped, not tables (with a schema)"),
    ("SELECT * FROM other_stream", "can only read records, shaped, not other_stream"),
    # a WITH query with a quoted name could otherwise stand for a table with a schema
    ('WITH ".pg_settings" AS (SELECT 1 AS x) SELECT name FROM pg_catalog.pg_settings',
     "WITH queries need plain names (letters, digits and _), not .pg_settings"),
    ("SELECT * FROM (WITH records AS (SELECT 1 AS x) SELECT * FROM records)", "WITH queries cannot be named records"),
    ("SELECT 1 AS id, 2 AS Id FROM records", "columns named more than once: Id, id (names ignore case)"),
    ("SELECT name, setting FROM pg_settings", "can only read records, shaped, not pg_settings"),
    # a WITH query named like a built-in view would let other references to that name read the view
    ("SELECT * FROM (WITH pg_settings AS (SELECT 1 AS x) SELECT * FROM pg_settings), (SELECT name FROM pg_settings)",
     "WITH queries cannot be named pg_settings"),
    ("WITH a AS (SELECT name FROM pg_settings), pg_settings AS (SELECT 1) SELECT * FROM a",
     "WITH queries cannot be named pg_settings"),
    ("WITH PG_Settings AS (SELECT setting FROM pg_settings) SELECT * FROM PG_Settings",
     "WITH queries cannot be named pg_settings"),
    ("PIVOT records ON (record->>'k') USING count(*)", "found: CREATE, SELECT (list the values of a PIVOT"),
    ("SELECT get_block_size('memory') AS b FROM records", "function(s) get_block_size are not allowed"),
    ("SELECT write_log('x') AS w FROM records", "function(s) write_log are not allowed"),
    ("SELECT 1 AS x; DROP TABLE records", "one SELECT statement, found: SELECT, DROP"),
    ("DELETE FROM records", "one SELECT statement, found: DELETE"),
    ("COPY (SELECT 1) TO '/tmp/out.csv'", "one SELECT statement, found: COPY"),
    ("ATTACH '/tmp/other.duckdb'", "one SELECT statement, found: ATTACH"),
    ("SET enable_external_access = true", "one SELECT statement, found: SET"),
    ("SELECT ? AS x FROM records", "parameters are config inputs named in the query, `$name`: not ? or $1"),
    ("SELECT $1 AS x FROM records", "parameters are config inputs named in the query, `$name`: not ? or $1"),
    ("SELECT recrod->>'id' AS id FROM records", 'Referenced column "recrod" not found'),
    ("SELECT 1, 2 AS x FROM records", "column '1' is not a valid field name"),
    ("SELECT 1 AS x, 2 AS x FROM records", "columns named more than once: x"),
    ("SELEC 1", "syntax error"),
])
def test_a_step_is_one_locked_down_query(query, expected):
    assert expected in problem(query)


def test_named_parameters_are_part_of_a_query():
    sandbox = sql.Sandbox()
    try:
        text, reads, parameters = sandbox.parse("SELECT $a AS a, $b + $a AS c FROM records WHERE '$x' <> $b",
                                                ["records"])
        assert reads == {"records"} and sorted(parameters) == ["a", "a", "b", "b"]
        # each use gets its type (a subquery DuckDB does not fold, or a plain cast); the values stay parameters
        types = {"a": "VARCHAR", "b": "BIGINT[]"}
        assert sandbox.typed(text, parameters, types) == (
            "SELECT (SELECT CAST($a AS VARCHAR)) AS a, (SELECT CAST($b AS BIGINT[])) + (SELECT CAST($a AS VARCHAR)) "
            "AS c FROM records WHERE '$x' <> (SELECT CAST($b AS BIGINT[]))")
        assert sandbox.typed(text, parameters, types, scalar=False) == (
            "SELECT (CAST($a AS VARCHAR)) AS a, (CAST($b AS BIGINT[])) + (CAST($a AS VARCHAR)) AS c FROM records "
            "WHERE '$x' <> (CAST($b AS BIGINT[]))")
        # the tokenizer counts UTF-8 bytes
        text, _, parameters = sandbox.parse("SELECT 'éü日本' AS x, $a AS a /* ☃ $z */ FROM records", ["records"])
        assert sandbox.typed(text, parameters, types, scalar=False) == (
            "SELECT 'éü日本' AS x, (CAST($a AS VARCHAR)) AS a /* ☃ $z */ FROM records")
        # right after IN, a cast: `x IN $b` is DuckDB's list membership, and `x IN (...)` a list of values
        text, _, parameters = sandbox.parse("SELECT 1 AS x FROM records WHERE 1 IN $b OR 2 not in /* c */ $b",
                                            ["records"])
        assert sandbox.typed(text, parameters, types) == (
            "SELECT 1 AS x FROM records WHERE 1 IN CAST((SELECT CAST($b AS BIGINT[])) AS BIGINT[]) OR 2 not in "
            "/* c */ CAST((SELECT CAST($b AS BIGINT[])) AS BIGINT[])")
        assert sandbox.typed(text, parameters, types, scalar=False) == (
            "SELECT 1 AS x FROM records WHERE 1 IN CAST((CAST($b AS BIGINT[])) AS BIGINT[]) OR 2 not in /* c */ "
            "CAST((CAST($b AS BIGINT[])) AS BIGINT[])")
    finally:
        sandbox.close()


def test_steps_cannot_read_python_objects_and_spill_to_a_private_folder():
    pyarrow = pytest.importorskip("pyarrow")
    secrets_table = pyarrow.table({"secret": ["top-secret-value"]})  # what a query must not see
    sandbox = sql.Sandbox()
    try:
        with pytest.raises(sql.SqlError, match="can only read records, not secrets_table"):
            sandbox.parse("SELECT * FROM secrets_table", ["records"])
        with pytest.raises(sql.SqlError, match="secrets_table does not exist"):  # and DuckDB itself cannot see it
            sandbox.describe("SELECT * FROM secrets_table")
        settings = sandbox.connection.execute(
            "SELECT current_setting('python_enable_replacements'), current_setting('temp_directory')").fetchone()
        assert settings == (False, sandbox.folder) and os.path.basename(sandbox.folder).startswith("streamwright-sql-")
        assert os.path.isdir(sandbox.folder) and secrets_table.num_rows == 1
    finally:
        sandbox.close()
    assert not os.path.exists(sandbox.folder)  # removed with the run's data


def test_allowed_table_functions_and_ctes():
    sandbox = sql.Sandbox()
    try:
        query = """WITH items AS (SELECT value AS item FROM records, json_each(record->'$.items'))
                   SELECT item->>'$.n' AS n, x FROM items, unnest([1]) AS t(x), range(1) ORDER BY n"""
        assert shaped(sandbox, query, [{"items": [{"n": "b"}, {"n": "a"}]}]) == [
            {"n": "a", "x": 1}, {"n": "b", "x": 1}]
        pivot = "PIVOT (SELECT record->>'k' AS k FROM records) ON k IN ('a', 'b') USING count(*)"
        assert shaped(sandbox, pivot, [{"k": "a"}, {"k": "b"}, {"k": "a"}]) == [{"a": 2, "b": 1}]
        # names are compared as DuckDB does, without case
        assert shaped(sandbox, "WITH X AS (SELECT count(*) AS n FROM RECORDS) SELECT n FROM x", [{}, {}]) == [
            {"n": 2}]
    finally:
        sandbox.close()


def test_rows_per_page_are_limited():
    sandbox = sql.Sandbox(max_rows=10)
    try:
        assert len(shaped(sandbox, "SELECT * FROM range(10)", [{}])) == 10
        with pytest.raises(sql.SqlError, match="step 'shaped': the query made more than 10 rows from one page"):
            shaped(sandbox, "SELECT * FROM range(11)", [{}])
    finally:
        sandbox.close()


def test_schemas_match_values_and_json_is_data():
    jsonschema = pytest.importorskip("jsonschema")
    sandbox = sql.Sandbox()
    try:
        (record,) = shaped(sandbox, """SELECT {'raw': record} AS wrapped, [record->'$.x'] AS listed,
            MAP {'k': record} AS mapped, union_value(num := 2) AS u, INTERVAL 90 SECOND AS waited,
            {'a': 1, 'b': 'x'} AS pair, 0.0 / 0.0 AS nan, 1.0 / 0.0 AS inf, record AS raw,
            {'e': 'a)b'::ENUM('x, y', 'a)b'), 'j': record->'$.x'} AS enum_and_json,
            {'n': 1, 'at': [TIMESTAMPTZ '2026-01-02 03:04:05+00']} AS pair_of_list FROM records""", [{"x": {"n": 1}}])
        assert record == {"wrapped": {"raw": {"x": {"n": 1}}}, "listed": [{"n": 1}], "mapped": {"k": {"x": {"n": 1}}},
                          "u": 2, "waited": 90.0, "pair": {"a": 1, "b": "x"}, "nan": None, "inf": None,
                          "raw": {"x": {"n": 1}}, "enum_and_json": {"e": "a)b", "j": {"n": 1}},
                          "pair_of_list": {"n": 1, "at": ["2026-01-02T03:04:05+00:00"]}}
        jsonschema.validate(record, sql.table_schema(sandbox.table_columns("shaped")),
                            cls=jsonschema.Draft7Validator)
        json.dumps(record, allow_nan=False)
    finally:
        sandbox.close()
    # a step is a table, which cannot hold unnamed structs
    assert problem("SELECT 1 AS n, row(1, 'x') AS pair, [(1, [DATE '2026-01-01'])] AS pairs FROM records") == (
        "column(s) pair, pairs hold unnamed structs (row(...) or (a, b)), which a step's table cannot store: name "
        "their fields, {'a': ..., 'b': ...}")


def test_queries_have_a_time_limit():
    sandbox = sql.Sandbox(timeout=0.2)
    try:
        with pytest.raises(sql.SqlError, match="step 'shaped': the query took longer than 0.2s"):
            shaped(sandbox, "SELECT count(*) AS n FROM range(100000000000) a, records", [{}], {"today": TODAY})
        assert shaped(sandbox, "SELECT count(*) AS n FROM records", [{}, {}]) == [{"n": 2}]
    finally:
        sandbox.close()


def test_the_time_limit_also_stops_a_statement_started_after_it():
    sandbox = sql.Sandbox(timeout=0.1)
    try:
        def work():
            time.sleep(0.3)  # the limit passes between two statements: the next one must still stop
            return sandbox.connection.execute("SELECT sum(hash(i) % 7) FROM range(300000000) t(i)").fetchall()
        started = time.time()
        with pytest.raises(sql.SqlError, match="took longer than 0.1s"):
            sandbox._timed(work)
        assert time.time() - started < 2.0
        assert sandbox.connection.execute("SELECT 42").fetchone() == (42,)  # no interrupt reaches the next query
    finally:
        sandbox.close()


def test_bad_values_fail_the_page_or_become_null(api, caplog):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"clicks": "many"}]})
    doc = source(api, select="SELECT (record->>'clicks')::BIGINT AS clicks FROM records", primary_key=[],
                 incremental=None)
    with pytest.raises(SourceError, match=r"partition \{\"account_id\": \"a1\"\}: step 'performance': Conversion "
                                          r"Error"):
        run(doc)
    doc["streams"][0]["on_partition_error"] = "skip"
    assert run(doc).records == [] and "skipping partition" in caplog.text
    doc["streams"][0]["transform"]["steps"][0]["select"] = ("SELECT TRY_CAST(record->>'clicks' AS BIGINT) AS clicks "
                                                            "FROM records")
    assert [record for _, record in run(doc).records] == [{"clicks": None}]


def test_step_problems_stop_the_run_before_any_request(api, tmp_path, capsys, caplog, monkeypatch):
    doc = source(api, primary_key=["account_id", "campaign"])
    doc["streams"][0]["incremental"]["cursor_field"] = "day"
    doc["streams"].append(page_stream("ads", {"http": {"path": "/ads"}}, "SELECT 1 AS id FROM records",
                                      partitions=[{"from_stream": "performance", "fields": ["campaign"]}]))
    problems = sql.check_source(doc)
    assert [path for path, _ in problems] == [
        ("streams", 0, "export", "performance", "primary_key", 1), ("streams", 0, "incremental", "cursor_field"),
        ("streams", 1, "partitions", 0, "fields", 0)]
    assert "primary_key 'campaign' is not a column of step 'performance' (columns: account_id, campaign_id" in \
        problems[0][1]
    assert "'campaign' is not a column of export 'performance' of stream 'performance'" in problems[2][1]
    with pytest.raises(SourceError, match=r"stream 'performance': export.performance.primary_key\[1\]: primary_key "
                                          r"'campaign' is not a column"):
        run(doc)
    doc["streams"][1]["transform"]["steps"][0]["select"] = "SELECT nope FROM records"
    with pytest.raises(SourceError, match=r"stream 'ads': transform.steps\[0\].select: Binder Error"):
        run(doc)
    assert api.requests == []

    path = tmp_path / "source.yaml"
    doc["spec"] = {"config": {"account_ids": {"type": "list", "items": "string"},
                              "currency": {"type": "string"}},
                   "secrets": {"token": {"type": "string"}}}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    assert cli.main(["run", str(path), "--set", "account_ids=a1", "--set", "currency=USD"]) == 2
    assert "stream 'ads': transform.steps[0].select: Binder Error" in caplog.text and api.requests == []
    assert cli.main(["validate", str(path)]) == 1
    out = capsys.readouterr().out
    line = next(i for i, text in enumerate(path.read_text().splitlines(), 1) if "SELECT nope" in text)
    assert "%s:%d:" % (path, line) in out and "error [sql-check] streams[1].transform.steps[0].select: Binder " \
                                              "Error" in out


def test_step_output_is_json_ready(api, tmp_path, capsys, monkeypatch):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": CAMPAIGNS})
    path = tmp_path / "source.yaml"
    doc = source(api)
    doc["spec"] = {"config": {"account_ids": {"type": "list", "items": "string"},
                              "currency": {"type": "string"}},
                   "secrets": {"token": {"type": "string"}}}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    out = tmp_path / "out"
    assert cli.main(["run", str(path), "--set", "account_ids=a1", "--set", "currency=USD",
                     "--output", "csv:%s" % out]) == 0
    files = sorted(p.name for p in out.iterdir())
    assert files[-1] == "state.json"
    with open(str(out / files[0])) as stream:
        lines = stream.read().splitlines()
    assert lines[0].startswith("account_id,campaign_id,campaign_name,status,date,clicks")
    assert lines[1].startswith("a1,11,O'Brien,active,2026-10-02,40,1000,12.35,0.04,USD")
    assert json.loads((out / "state.json").read_text())["bookmarks"]["performance"]
