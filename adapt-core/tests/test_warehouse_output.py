import datetime
import importlib.util
import json
import logging
import os

import pytest

from adapt.core import cli
from adapt.core.engine import sql
from adapt.core.outputs.output import open_output
from adapt.core.runtime.testing import page_stream


TODAY = datetime.date.today()
DAYS = [(TODAY - datetime.timedelta(days=n)).isoformat() for n in (2, 1, 0)]
ORDERS = """SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day,
                   (record->>'amount')::DECIMAL(9, 2) AS amount,
                   record->'items' AS items, record->'details' AS details,
                   record->>'name' AS name,
                   (record->>'updated_at')::TIMESTAMPTZ AS updated_at%s
            FROM %s"""
INCREMENTAL = {"cursor_field": "day", "start": "-2d", "window": "1d", "lookback": "1d"}


def _source(api, key=True, incremental=True, transform=False, extra=None, raw=False, raw_key=False,
            transform_key=True):
    params = {"day": "{{ window.start }}"} if incremental else {"day": DAYS[0]}
    stream = page_stream("orders", {"http": {"path": "/orders", "params": params}}, ORDERS % (extra or "", "records"),
                         records={"path": "data"}, primary_key=["id"] if key else None)
    if incremental:
        stream["incremental"] = dict(INCREMENTAL)
    if raw:  # records as returned: one JSON column (keyed on a column taken from them)
        stream = page_stream("raw_events", {"http": {"path": "/raw"}}, "SELECT %srecord FROM records" % (
            "record->>'id' AS id, " if raw_key else ""), records={"path": "data"},
            primary_key=["id"] if raw_key else None)
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [stream]}
    if transform:  # a run-mode stream that reads the orders again and sums them by day
        export = {"step": "daily_sales_rows", "primary_key": ["day"]}
        if not transform_key:
            del export["primary_key"]
        source["streams"].append({
            "name": "sales_reports", "incremental": dict(INCREMENTAL),
            "requests": [{"name": "raw_orders", "records": {"path": "data"},
                          "http": {"path": "/orders", "params": {"day": "{{ window.start }}"}}}],
            "transform": {"mode": "run", "steps": [
                {"name": "order_rows", "select": ORDERS % ("", "raw_orders")},
                {"name": "daily_sales_rows",
                 "select": "SELECT day, sum(amount) AS amount FROM order_rows GROUP BY day"}]},
            "export": {"daily_sales": export}})
    return source


def _order(day, order_id=None, amount="12.34", name="first"):
    return {"id": order_id or int(day.replace("-", "")), "day": day, "amount": amount, "items": [{"sku": "a"}],
            "details": {"channel": "web"}, "name": name, "updated_at": day + "T10:00:00+00:00"}


@pytest.fixture
def wh_run(api, tmp_path, monkeypatch):
    (tmp_path / "stage").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "stage"))
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [_order(request["params"]["day"])]})
    api.routes[("GET", "/raw")] = lambda request: (200, {"data": [{"id": 1, "meta": {"a": 1}}]})

    def run(output, *arguments, **options):
        path = tmp_path / "shop.yaml"
        path.write_text(json.dumps(_source(api, **options)))
        return cli.main(["run", str(path), "--output", output] + list(arguments))
    return run


def _query(path, sql):
    import duckdb
    connection = duckdb.connect(str(path), read_only=True)
    try:
        connection.execute("SET TimeZone = 'UTC'")
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _days(api):
    days = [request["params"]["day"] for request in api.calls("/orders")]
    api.requests.clear()
    return days


def test_duckdb_loads_typed_columns_raw_records_state_and_resumes(wh_run, api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    assert wh_run("duckdb:%s:client_a" % database) == 0
    assert _days(api) == DAYS
    rows = _query(database, "SELECT id, day, amount, items, details, name, updated_at FROM client_a.orders ORDER BY id")
    assert [(row[1].isoformat(), str(row[2]), json.loads(row[3]), json.loads(row[4]), row[5],
             row[6].isoformat()) for row in rows] == [
                 (day, "12.34", [{"sku": "a"}], {"channel": "web"}, "first", day + "T10:00:00+00:00")
                 for day in DAYS]
    types = dict((name, kind) for name, kind, *_ in _query(database, "DESCRIBE client_a.orders"))
    assert (types["id"], types["day"], types["amount"], types["items"], types["details"], types["updated_at"]) == (
        "BIGINT", "DATE", "DECIMAL(9,2)", "JSON", "JSON", "TIMESTAMP WITH TIME ZONE")
    assert os.listdir(str(tmp_path / "stage")) == []

    assert wh_run("duckdb:%s:client_a" % database) == 0
    assert _days(api) == DAYS[1:]
    assert len(_query(database, "SELECT id FROM client_a.orders")) == 3

    assert wh_run("duckdb:%s:client_a" % database, "--stream", "raw_events", raw=True) == 0
    assert _query(database, "SELECT record FROM client_a.raw_events") == [('{"id":1,"meta":{"a":1}}',)]


def test_merge_keeps_last_duplicate_and_replaces_changed_values(wh_run, api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        _order(request["params"]["day"], order_id=1, amount="10.00", name="old"),
        _order(request["params"]["day"], order_id=1, amount="20.00", name="new")]})
    assert wh_run("duckdb:%s" % database) == 0
    assert _query(database, "SELECT id, amount::VARCHAR, name FROM shop.orders") == [(1, "20.00", "new")]

    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        _order(request["params"]["day"], order_id=1, amount="30.00", name="changed")]})
    assert wh_run("duckdb:%s" % database) == 0
    assert _query(database, "SELECT id, amount::VARCHAR, name FROM shop.orders") == [(1, "30.00", "changed")]


def test_replace_and_append_without_keys(wh_run, api, tmp_path, caplog):
    replace_db = tmp_path / "replace.duckdb"
    assert wh_run("duckdb:%s" % replace_db, key=False, incremental=False) == 0
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [_order(request["params"]["day"], order_id=9)]})
    assert wh_run("duckdb:%s" % replace_db, key=False, incremental=False) == 0
    assert _query(replace_db, "SELECT id FROM shop.orders") == [(9,)]

    append_db = tmp_path / "append.duckdb"
    with caplog.at_level(logging.WARNING, logger="adapt.output"):
        assert wh_run("duckdb:%s" % append_db, key=False) == 0
        assert wh_run("duckdb:%s" % append_db, key=False) == 0
    assert "stream 'orders' has no primary_key, so duckdb appends it" in caplog.text
    assert len(_query(append_db, "SELECT id FROM shop.orders")) == 5


def test_schema_evolution_and_type_conflict_roll_back(wh_run, api, tmp_path, caplog):
    database = tmp_path / "warehouse.duckdb"
    assert wh_run("duckdb:%s" % database) == 0
    assert wh_run("duckdb:%s" % database, extra=", record->>'coupon' AS coupon") == 0
    assert "coupon" in dict((name, kind) for name, kind, *_ in _query(database, "DESCRIBE shop.orders"))
    before = _query(database, "SELECT count(*), max(coupon) FROM shop.orders")
    code = wh_run("duckdb:%s" % database, extra=", (record->>'coupon')::BIGINT AS coupon")
    assert code == 1
    assert "shop.orders column 'coupon' has type VARCHAR, but this run writes BIGINT" in caplog.text
    assert _query(database, "SELECT count(*), max(coupon) FROM shop.orders") == before


def test_transform_state_schemas_and_failed_runs(wh_run, api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    assert wh_run("duckdb:%s:client_a" % database, transform=True) == 0
    assert _query(database, "SELECT day, amount::VARCHAR FROM client_a.daily_sales ORDER BY day") == [
        (datetime.date.fromisoformat(day), "12.34") for day in DAYS]
    assert wh_run("duckdb:%s:client_a" % database, transform=True) == 0
    assert len(_query(database, "SELECT day FROM client_a.daily_sales")) == 3
    _days(api)

    assert wh_run("duckdb:%s:client_b" % database) == 0
    assert _days(api) == DAYS
    api.routes[("GET", "/orders")] = lambda request: (500, {"error": "down"})
    assert wh_run("duckdb:%s:client_a" % database) == 1
    assert len(_query(database, "SELECT day FROM client_a.orders")) == 3


def test_unkeyed_exports_are_appended_when_their_stream_is_incremental(wh_run, api, tmp_path, caplog):
    source = _source(api, transform=True, transform_key=False)
    assert sql.incremental_exports(source) == {"orders", "daily_sales"}
    del source["streams"][1]["incremental"]
    assert sql.incremental_exports(source) == {"orders"}  # streams no longer read each other: nothing is inherited
    output = open_output("duckdb:%s" % (tmp_path / "check.duckdb"), source)
    try:
        assert [output.disposition("daily_sales", []), output.disposition("orders", []),
                output.disposition("daily_sales", ["day"])] == ["replace", "append", "merge"]
    finally:
        output.close(failed=True)
    database = tmp_path / "warehouse.duckdb"
    assert wh_run("duckdb:%s:client_a" % database, transform=True, transform_key=False) == 0
    with caplog.at_level(logging.WARNING, logger="adapt.output"):
        assert wh_run("duckdb:%s:client_a" % database, transform=True, transform_key=False) == 0
    # the second run reads yesterday (lookback) and today again: their totals are added, the first day is kept
    days = [row[0].isoformat() for row in _query(database, "SELECT day FROM client_a.daily_sales ORDER BY day")]
    assert days == [DAYS[0], DAYS[1], DAYS[1], DAYS[2], DAYS[2]]
    assert "stream 'daily_sales' has no primary_key" in caplog.text
    # with a key, its rows are merged
    database = tmp_path / "keyed.duckdb"
    assert wh_run("duckdb:%s:client_a" % database, transform=True) == 0
    assert wh_run("duckdb:%s:client_a" % database, transform=True) == 0
    assert len(_query(database, "SELECT day FROM client_a.daily_sales")) == 3


def test_transform_two_exports_load_to_duckdb_and_merge_on_second_run(api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    path = tmp_path / "shop.yaml"
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [{
        "name": "reports",
        "requests": [{"name": "order_raw", "http": {"path": "/orders"}, "records": {"path": "data"}}],
        "transform": {"mode": "run", "steps": [
            {"name": "orders", "select": "SELECT (record->>'id')::BIGINT AS id, "
                                        "(record->>'day')::DATE AS day, record->>'name' AS name, "
                                        "(record->>'amount')::DECIMAL(9, 2) AS amount FROM order_raw"},
            {"name": "daily", "select": "SELECT day, sum(amount) AS amount FROM orders GROUP BY day"},
            {"name": "names", "select": "SELECT id, name FROM orders"}]},
        "export": {
            "daily_sales": {"step": "daily", "primary_key": ["day"]},
            "order_names": {"step": "names", "primary_key": ["id"]}}}]}
    path.write_text(json.dumps(source))
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        _order("2026-10-01", order_id=1, amount="10.00", name="old"),
        _order("2026-10-02", order_id=2, amount="20.00", name="second")]})
    assert cli.main(["run", str(path), "--output", "duckdb:%s:client_a" % database]) == 0
    assert _query(database, "SELECT day, amount::VARCHAR FROM client_a.daily_sales ORDER BY day") == [
        (datetime.date(2026, 10, 1), "10.00"), (datetime.date(2026, 10, 2), "20.00")]
    assert _query(database, "SELECT id, name FROM client_a.order_names ORDER BY id") == [(1, "old"), (2, "second")]

    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        _order("2026-10-01", order_id=1, amount="30.00", name="changed"),
        _order("2026-10-03", order_id=3, amount="40.00", name="third")]})
    assert cli.main(["run", str(path), "--output", "duckdb:%s:client_a" % database]) == 0
    assert _query(database, "SELECT day, amount::VARCHAR FROM client_a.daily_sales ORDER BY day") == [
        (datetime.date(2026, 10, 1), "30.00"), (datetime.date(2026, 10, 2), "20.00"),
        (datetime.date(2026, 10, 3), "40.00")]
    assert _query(database, "SELECT id, name FROM client_a.order_names ORDER BY id") == [
        (1, "changed"), (2, "second"), (3, "third")]


def test_bad_specs_are_input_errors(wh_run, api, tmp_path, caplog):
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(_source(api)))
    assert cli.main(["run", str(path), "--output", "duckdb:"]) == 2
    assert "--output duckdb needs a path" in caplog.text
    assert cli.main(["run", str(path), "--output", "duckdb:%s:bad-name" % (tmp_path / "w.duckdb")]) == 2
    assert "schema 'bad-name' is not valid" in caplog.text
    assert cli.main(["run", str(path), "--output", "nope:x"]) == 2
    assert "ducklake:CATALOG[:SCHEMA]" in caplog.text

    assert cli.main(["run", str(path), "--output", "duckdb:%s" % (tmp_path / "shop.duckdb")]) == 2
    assert "schema 'shop' has the name of the database file shop.duckdb" in caplog.text


def test_duckdb_expands_home_before_reading_state(api, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = _source(api, key=False)
    source["streams"][0]["incremental"]["lookback"] = "0d"
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(source))

    def route(request):
        day = request["params"]["day"]
        return 200, {"data": [] if day == DAYS[-1] else [_order(day)]}
    api.routes[("GET", "/orders")] = route

    output = "duckdb:~/w.duckdb:client_a"
    assert cli.main(["run", str(path), "--output", output]) == 0
    assert _days(api) == DAYS
    assert cli.main(["run", str(path), "--output", output]) == 0
    assert _days(api) == [DAYS[-1]]
    assert _query(home / "w.duckdb", "SELECT count(*), count(distinct id) FROM client_a.orders") == [(2, 2)]


def test_records_as_returned_merge_on_a_key_column(wh_run, api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    api.routes[("GET", "/raw")] = lambda request: (200, {"data": [
        {"id": 1, "value": "old"}, {"id": 1, "value": "new"}]})
    assert wh_run("duckdb:%s" % database, "--stream", "raw_events", raw=True, raw_key=True) == 0
    assert _query(database, "SELECT id, json_extract_string(record, '$.value') FROM shop.raw_events") == [
        ("1", "new")]

    api.routes[("GET", "/raw")] = lambda request: (200, {"data": [{"id": 1, "value": "changed"}]})
    assert wh_run("duckdb:%s" % database, "--stream", "raw_events", raw=True, raw_key=True) == 0
    assert _query(database, "SELECT json_extract_string(record, '$.value') FROM shop.raw_events") == [("changed",)]


def test_json_columns_store_missing_values_as_sql_null(wh_run, api, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        dict(_order(request["params"]["day"], order_id=1), details=None, items=[{"sku": None}])]})
    assert wh_run("duckdb:%s" % database) == 0
    rows = _query(database, "SELECT details IS NULL, items::VARCHAR FROM shop.orders")
    assert rows == [(True, '[{"sku":null}]')]
    assert _query(database, "SELECT count(details) FROM shop.orders") == [(0,)]


def test_uuid_columns_are_stored_as_text(wh_run, tmp_path):
    database = tmp_path / "warehouse.duckdb"
    uuid = "5b8e6c2f-4a2e-4d3a-9b1a-2f0e1c3d4b5a"
    assert wh_run("duckdb:%s" % database, extra=", '%s'::UUID AS u" % uuid) == 0
    types = dict((name, kind) for name, kind, *_ in _query(database, "DESCRIBE shop.orders"))
    assert types["u"] == "VARCHAR"
    assert _query(database, "SELECT DISTINCT u FROM shop.orders") == [(uuid,)]


def test_partial_replace_stream_keeps_last_complete_rows(api, tmp_path, monkeypatch, caplog):
    (tmp_path / "stage").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "stage"))
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [page_stream(
        "orders", {"http": {"path": "/orders", "params": {"account": "{{ partition.account }}"}}},
        "SELECT partition->>'account' AS account, record->>'id' AS id FROM records", records={"path": "data"},
        partitions=[{"name": "account", "values": ["a", "b"]}], on_partition_error="skip")]}
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(source))
    database = tmp_path / "warehouse.duckdb"

    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [{"id": request["params"]["account"] + "1"}]})
    assert cli.main(["run", str(path), "--output", "duckdb:%s" % database]) == 0
    api.routes[("GET", "/orders")] = lambda request: (
        (500, {}) if request["params"]["account"] == "b" else (200, {"data": [{"id": "a2"}]}))
    with caplog.at_level(logging.WARNING, logger="adapt.output"):
        assert cli.main(["run", str(path), "--output", "duckdb:%s" % database]) == 0

    assert _query(database, "SELECT account, id FROM shop.orders ORDER BY account") == [("a", "a1"), ("b", "b1")]
    assert "stream 'orders' skipped partitions, so its table keeps the rows of the last complete run" in caplog.text


def _ducklake_available():
    """Whether DuckLake can run here: installed now, or installed on first use (CI downloads it)."""
    if importlib.util.find_spec("duckdb") is None:
        return False
    import duckdb
    from adapt.core.outputs.warehouse import _load_ducklake
    connection = duckdb.connect(":memory:")
    try:
        _load_ducklake(connection)
        return True
    except Exception:
        return False
    finally:
        connection.close()


needs_ducklake = pytest.mark.skipif(not _ducklake_available(), reason="the DuckLake extension cannot be installed")


@needs_ducklake
def test_ducklake_loads_and_resumes(wh_run, api, tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.ducklake"
    data = tmp_path / "ducklake-data"
    data.mkdir()
    monkeypatch.setenv("ADAPT_DUCKLAKE_DATA_PATH", str(data))
    assert wh_run("ducklake:%s:client_a" % catalog) == 0
    _days(api)
    assert wh_run("ducklake:%s:client_a" % catalog) == 0
    assert _days(api) == DAYS[1:]
    import duckdb
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("LOAD ducklake")
        connection.execute("ATTACH 'ducklake:%s' AS lake (DATA_PATH '%s')" % (
            str(catalog).replace("'", "''"), str(data).replace("'", "''")))
        assert len(connection.execute("SELECT id FROM lake.client_a.orders").fetchall()) == 3
    finally:
        connection.close()


@needs_ducklake
def test_ducklake_expands_home_for_catalog_data_path_and_state(wh_run, api, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "ducklake-data").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ADAPT_DUCKLAKE_DATA_PATH", "~/ducklake-data")
    assert wh_run("ducklake:~/catalog.ducklake:client_a") == 0
    assert _days(api) == DAYS
    assert wh_run("ducklake:~/catalog.ducklake:client_a") == 0
    assert _days(api) == DAYS[1:]


# * ---------------------------------------------------
# * DuckLake on object storage (S3) and Postgres catalogs
# * ---------------------------------------------------

KEY_ID = "AKIAKEYIDVALUE1"
SECRET = "s3cr3t-Value/With+Chars"
TOKEN = "session-token-value-42"
PASSWORD = "pg-pass-word-99"
LAKE_ENV = ["ADAPT_DUCKLAKE_DATA_PATH", "ADAPT_DUCKLAKE_DATA_INLINING_ROW_LIMIT", "ADAPT_DUCKLAKE_CATALOG_PASSWORD",
            "ADAPT_DUCKLAKE_CATALOG_SCHEMA", "ADAPT_DUCKLAKE_S3_KEY_ID", "ADAPT_DUCKLAKE_S3_SECRET",
            "ADAPT_DUCKLAKE_S3_SESSION_TOKEN", "ADAPT_DUCKLAKE_S3_REGION", "ADAPT_DUCKLAKE_S3_ENDPOINT",
            "ADAPT_DUCKLAKE_S3_URL_STYLE", "ADAPT_DUCKLAKE_S3_USE_SSL"]
LOCALSTACK = {"ADAPT_DUCKLAKE_DATA_PATH": "s3://adapt-warehouse/u1/", "ADAPT_DUCKLAKE_S3_ENDPOINT": "localhost:4566",
              "ADAPT_DUCKLAKE_S3_KEY_ID": KEY_ID, "ADAPT_DUCKLAKE_S3_SECRET": SECRET,
              "ADAPT_DUCKLAKE_S3_URL_STYLE": "path", "ADAPT_DUCKLAKE_S3_USE_SSL": "false"}
SOURCE = {"name": "shop", "streams": []}


@pytest.fixture
def lake_env(monkeypatch):
    for name in LAKE_ENV:
        monkeypatch.delenv(name, raising=False)

    def setenv(values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    return setenv


class _Recorder(object):
    """A DuckDB connection that records what it is asked (extensions are installed; ATTACH can fail)."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.closed = False

    def execute(self, query, parameters=None):
        self.calls.append((query, parameters))
        if self.fail and query.startswith("ATTACH"):
            raise self.fail
        return self

    def fetchone(self):
        return (True,)

    def close(self):
        self.closed = True

    def queries(self):
        return [query for query, _ in self.calls]


@pytest.fixture
def recorder(monkeypatch):
    import duckdb
    made = []

    def connect(*arguments, **options):
        made.append(_Recorder(getattr(connect, "fail", None)))
        return made[-1]
    monkeypatch.setattr(duckdb, "connect", connect)
    connect.made = made
    return connect


def test_ducklake_local_catalog_attaches_as_before(lake_env, recorder, tmp_path):
    lake_env({"ADAPT_DUCKLAKE_DATA_PATH": str(tmp_path / "data")})
    output = open_output("ducklake:%s:client_a" % (tmp_path / "c.ducklake"), source=SOURCE)
    output._connect()
    queries = recorder.made[0].queries()
    assert "LOAD ducklake" in queries
    assert not any("httpfs" in query or "postgres" in query or "SECRET" in query for query in queries)
    assert "ATTACH 'ducklake:%s' AS \"adapt_ducklake\" (DATA_PATH '%s')" % (
        tmp_path / "c.ducklake", tmp_path / "data") in queries
    assert queries[-1] == 'USE "adapt_ducklake"'


def test_ducklake_s3_data_path_sets_httpfs_and_a_bound_secret_before_attach(lake_env, recorder, tmp_path, caplog):
    lake_env(dict(LOCALSTACK, ADAPT_DUCKLAKE_S3_REGION="us-east-1", ADAPT_DUCKLAKE_S3_SESSION_TOKEN=TOKEN))
    output = open_output("ducklake:%s" % (tmp_path / "c.ducklake"), source=SOURCE)
    with caplog.at_level(logging.DEBUG, logger="adapt.output"):
        output._connect()
    calls = recorder.made[0].calls
    queries = [query for query, _ in calls]
    assert queries.index("LOAD httpfs") < queries.index(next(q for q in queries if q.startswith("CREATE"))) < \
        queries.index(next(q for q in queries if q.startswith("ATTACH")))
    secret = next(call for call in calls if call[0].startswith("CREATE"))
    assert secret == ("CREATE OR REPLACE TEMPORARY SECRET adapt_ducklake_s3 (TYPE s3, KEY_ID ?, SECRET ?, "
                      "SESSION_TOKEN ?, REGION ?, ENDPOINT ?, URL_STYLE ?, USE_SSL ?, SCOPE ?)",
                      [KEY_ID, SECRET, TOKEN, "us-east-1", "localhost:4566", "path", False, "s3://adapt-warehouse/"])
    attach = next(query for query in queries if query.startswith("ATTACH"))
    assert attach == ("ATTACH 'ducklake:%s' AS \"adapt_ducklake\" (DATA_PATH 's3://adapt-warehouse/u1/', "
                      "DATA_INLINING_ROW_LIMIT 0)" % (tmp_path / "c.ducklake"))
    for value in (KEY_ID, SECRET, TOKEN):
        assert not any(value in query for query in queries)
        assert value not in caplog.text
    assert "endpoint=localhost:4566" in caplog.text and "key_id=***" in caplog.text


def test_ducklake_s3_opt_in_without_s3_data_path_and_inlining_override(lake_env, recorder, tmp_path):
    lake_env({"ADAPT_DUCKLAKE_S3_ENDPOINT": "minio:9000", "ADAPT_DUCKLAKE_S3_USE_SSL": "TRUE",
              "ADAPT_DUCKLAKE_DATA_INLINING_ROW_LIMIT": "25"})
    output = open_output("ducklake:%s" % (tmp_path / "c.ducklake"), source=SOURCE)
    output._connect()
    calls = recorder.made[0].calls
    assert ("CREATE OR REPLACE TEMPORARY SECRET adapt_ducklake_s3 (TYPE s3, ENDPOINT ?, USE_SSL ?)",
            ["minio:9000", True]) in calls
    assert "ATTACH 'ducklake:%s' AS \"adapt_ducklake\" (DATA_INLINING_ROW_LIMIT 25)" % (
        tmp_path / "c.ducklake") in [query for query, _ in calls]


def test_ducklake_s3_data_path_without_settings_loads_httpfs_only(lake_env, recorder, tmp_path):
    lake_env({"ADAPT_DUCKLAKE_DATA_PATH": "s3://bucket/lake/"})
    open_output("ducklake:%s" % (tmp_path / "c.ducklake"), source=SOURCE)._connect()
    queries = recorder.made[0].queries()
    assert "LOAD httpfs" in queries and not any("SECRET" in query for query in queries)


@pytest.mark.parametrize("values, message", [
    ({"ADAPT_DUCKLAKE_S3_USE_SSL": "yes"}, "ADAPT_DUCKLAKE_S3_USE_SSL: expected true or false"),
    ({"ADAPT_DUCKLAKE_S3_URL_STYLE": "virtual"}, "ADAPT_DUCKLAKE_S3_URL_STYLE: expected one of: path, vhost"),
    ({"ADAPT_DUCKLAKE_S3_ENDPOINT": "http://localhost:4566"}, "ADAPT_DUCKLAKE_S3_ENDPOINT: expected a host"),
    ({"ADAPT_DUCKLAKE_S3_REGION": "us east"}, "ADAPT_DUCKLAKE_S3_REGION: expected a region"),
    ({"ADAPT_DUCKLAKE_S3_KEY_ID": KEY_ID}, "set both or neither"),
    ({"ADAPT_DUCKLAKE_S3_SECRET": SECRET}, "set both or neither"),
    ({"ADAPT_DUCKLAKE_S3_SESSION_TOKEN": TOKEN}, "SESSION_TOKEN needs"),
    ({"ADAPT_DUCKLAKE_DATA_PATH": "s3://"}, "ADAPT_DUCKLAKE_DATA_PATH: expected s3://BUCKET/PREFIX/"),
    ({"ADAPT_DUCKLAKE_DATA_INLINING_ROW_LIMIT": "-1"}, "expected a whole number >= 0"),
    ({"ADAPT_DUCKLAKE_CATALOG_SCHEMA": "lake meta"}, "ADAPT_DUCKLAKE_CATALOG_SCHEMA 'lake meta' is not valid"),
    ({"ADAPT_DUCKLAKE_CATALOG_PASSWORD": PASSWORD}, "is for a postgres catalog"),
])
def test_ducklake_settings_are_checked_without_showing_credentials(lake_env, tmp_path, values, message):
    lake_env(values)
    with pytest.raises(ValueError) as error:
        open_output("ducklake:%s" % (tmp_path / "c.ducklake"), source=SOURCE)
    assert message in str(error.value)
    for value in (KEY_ID, SECRET, TOKEN, PASSWORD):
        assert value not in str(error.value)


@pytest.mark.parametrize("spec, catalog, schema", [
    ("ducklake:postgres:dbname=lake host=localhost user=adapt", "postgres:dbname=lake host=localhost user=adapt",
     "shop"),
    ("ducklake:postgres:dbname=lake host=localhost user=adapt:client_a",
     "postgres:dbname=lake host=localhost user=adapt", "client_a"),
    ("ducklake:postgres:postgresql://adapt@localhost:5432/lake", "postgres:postgresql://adapt@localhost:5432/lake",
     "shop"),
    ("ducklake:postgres:host=~db", "postgres:host=~db", "shop"),
])
def test_ducklake_postgres_catalog_spec_keeps_the_whole_connection_string(lake_env, spec, catalog, schema):
    output = open_output(spec, source=SOURCE)
    assert (output.path, output.schema, output.postgres) == (catalog, schema, True)


def test_ducklake_postgres_catalog_loads_postgres_and_binds_the_password(lake_env, recorder, caplog):
    lake_env(dict(LOCALSTACK, ADAPT_DUCKLAKE_CATALOG_PASSWORD=PASSWORD, ADAPT_DUCKLAKE_CATALOG_SCHEMA="lake_meta"))
    output = open_output("ducklake:postgres:dbname=lake host=localhost user=adapt:client_a", source=SOURCE)
    with caplog.at_level(logging.DEBUG, logger="adapt.output"):
        output._connect(read_only=True)
    calls = recorder.made[0].calls
    queries = [query for query, _ in calls]
    assert queries.index("LOAD postgres") < queries.index(next(q for q in queries if q.startswith("ATTACH")))
    assert ("CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD ?)", [PASSWORD]) in calls
    assert ("ATTACH 'ducklake:postgres:dbname=lake host=localhost user=adapt' AS \"adapt_ducklake\" ("
            "DATA_PATH 's3://adapt-warehouse/u1/', METADATA_SCHEMA 'lake_meta', DATA_INLINING_ROW_LIMIT 0, "
            "READ_ONLY)") in queries
    for value in (KEY_ID, SECRET, PASSWORD):
        assert not any(value in query for query in queries)
        assert value not in caplog.text


@pytest.mark.parametrize("dsn", ["dbname=lake password=x user=adapt", "postgresql://adapt@h/lake?password=x",
                                 "PASSWORD = x"])
def test_ducklake_postgres_catalog_refuses_a_password_in_the_connection_string(lake_env, dsn):
    with pytest.raises(ValueError) as error:
        open_output("ducklake:postgres:%s" % dsn, source=SOURCE)
    assert "ADAPT_DUCKLAKE_CATALOG_PASSWORD" in str(error.value) and "=x" not in str(error.value)


def test_ducklake_postgres_catalog_without_a_lake_yet_has_no_state(lake_env, recorder):
    import duckdb
    recorder.fail = duckdb.InvalidInputException(
        "Existing DuckLake at metadata catalog \"postgres:dbname=lake\" does not exist - and creating a new "
        "DuckLake is explicitly disabled")
    assert open_output("ducklake:postgres:dbname=lake", source=SOURCE).initial_state() is None
    assert recorder.made[0].closed
    recorder.fail = duckdb.IOException("Unable to connect to Postgres")
    with pytest.raises(duckdb.IOException):
        open_output("ducklake:postgres:dbname=lake", source=SOURCE).initial_state()


def test_ducklake_errors_mask_credentials(lake_env, recorder, tmp_path):
    import duckdb
    lake_env(LOCALSTACK)
    recorder.fail = duckdb.IOException("HTTP 403: the key %s with secret %s was refused" % (KEY_ID, SECRET))
    with pytest.raises(RuntimeError) as error:
        open_output("ducklake:%s" % (tmp_path / "c.ducklake"), source=SOURCE)._connect()
    assert str(error.value) == "IO Error: HTTP 403: the key *** with secret *** was refused" or \
        str(error.value) == "HTTP 403: the key *** with secret *** was refused"
    assert error.value.__suppress_context__
