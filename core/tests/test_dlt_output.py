import datetime
import importlib.util
import json
import logging
import os

import pytest

from streamwright.core import cli
from streamwright.core.outputs import dlt_output
from streamwright.core.engine import sql
from streamwright.core.outputs.dlt_output import column_hints
from streamwright.core.runtime.testing import page_stream

needs_dlt = pytest.mark.skipif(importlib.util.find_spec("dlt") is None or importlib.util.find_spec("duckdb") is None,
                               reason="needs dlt and duckdb (pip install 'streamwright[dlt]' 'dlt[duckdb]')")
needs_delta = pytest.mark.skipif(
    importlib.util.find_spec("dlt") is None or importlib.util.find_spec("deltalake") is None,
    reason="needs dlt and deltalake (pip install 'dlt[deltalake]')")
TODAY = datetime.date.today()
DAYS = [(TODAY - datetime.timedelta(days=n)).isoformat() for n in (2, 1, 0)]
ORDERS = """SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day,
                   (record->>'amount')::DOUBLE AS amount, (record->'tags')::VARCHAR[] AS tags
            FROM %s"""
INCREMENTAL = {"cursor_field": "day", "start": "-2d", "window": "1d", "lookback": "1d"}


def test_column_hints_follow_column_types():
    sandbox = sql.Sandbox()
    try:
        sandbox.create_raw("records")
        columns = sandbox.describe("""SELECT 'x' AS s, 1 AS i, 1.5::DOUBLE AS n,
            2.5::DECIMAL(9, 2) AS m, true AS b, DATE '2026-01-01' AS d, TIMESTAMPTZ '2026-01-01 10:00:00+00' AS t,
            {'k': 1} AS o, [1] AS a, record AS u, sum(m) OVER () AS total FROM records""")
        schema = sql.table_schema(columns)
    finally:
        sandbox.close()
    hints = dict((name, {"data_type": kind, "nullable": True}) for name, kind in (
        ("s", "text"), ("i", "bigint"), ("n", "double"), ("m", "double"), ("b", "bool"), ("d", "date"),
        ("t", "timestamp"), ("o", "json"), ("a", "json"), ("total", "double")))  # u, JSON of any kind, gets no hint
    assert column_hints(schema) == hints
    assert column_hints({"type": "object"}) == {}
    # with the step's columns (write_columns), DECIMAL(p,s) columns are dlt decimals: exact
    decimals = dlt_output._decimal_types(columns)
    assert decimals == {"m": (9, 2), "total": (38, 2)}
    assert column_hints(schema, decimals) == dict(hints, m={"data_type": "decimal", "precision": 9, "scale": 2,
                                                            "nullable": True},
                                                  total={"data_type": "decimal", "precision": 38, "scale": 2,
                                                         "nullable": True})


def shop(api, orders_key=True, transform=False, transform_key=True):
    orders = page_stream("orders", {"http": {"path": "/orders", "params": {"day": "{{ window.start }}"}}},
                         ORDERS % "records", records={"path": "data"}, primary_key=["id"] if orders_key else None,
                         incremental=dict(INCREMENTAL))
    # JSON values (record->'meta'): dlt makes columns of their fields
    events = page_stream("events", {"http": {"path": "/events"}},
                         "SELECT (record->>'id')::BIGINT AS id, record->'meta' AS meta FROM records",
                         records={"path": "data"})
    source = {"kind": "source", "name": "shop", "spec": {"secrets": {"token": {"type": "string"}}},
              "auth": {"type": "bearer", "token": "{{ secrets.token }}"}, "http": {"base_url": api.url},
              "streams": [orders, events]}
    if transform:  # a run-mode stream that reads the orders again and sums them by day
        export = {"step": "daily_sales_rows", "primary_key": ["day"]}
        if not transform_key:
            del export["primary_key"]
        source["streams"].append({
            "name": "sales_reports", "incremental": dict(INCREMENTAL),
            "requests": [{"name": "raw_orders", "records": {"path": "data"},
                          "http": {"path": "/orders", "params": {"day": "{{ window.start }}"}}}],
            "transform": {"mode": "run", "steps": [
                {"name": "order_rows", "select": ORDERS % "raw_orders"},
                {"name": "daily_sales_rows", "select": "SELECT day, sum(amount) AS amount, count(*) AS orders "
                                                       "FROM order_rows GROUP BY day"}]},
            "export": {"daily_sales": export}})
    return source


def orders(order_id=None):
    """Each day has one order, with its own id (from the day) unless `order_id` is given."""
    return lambda request: (200, {"data": [{
        "id": order_id or int(request["params"]["day"].replace("-", "")), "day": request["params"]["day"],
        "amount": 10.5, "tags": ["new"]}]})


@pytest.fixture
def streamwright_run(api, tmp_path, monkeypatch):
    """streamwright_run(output, *arguments): `streamwright run` of the shop source; dlt's files stay inside tmp_path."""
    monkeypatch.chdir(tmp_path)  # dlt also reads .dlt/ from the working directory
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "warehouse.duckdb"))
    monkeypatch.setenv("DESTINATION__FILESYSTEM__BUCKET_URL", (tmp_path / "lake").as_uri())
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))  # where each run's dlt folder goes
    api.routes[("GET", "/orders")] = orders()
    api.routes[("GET", "/events")] = lambda request: (200, {"data": [{"id": 1, "meta": {"source": "web"}}]})

    def run(output="dlt:duckdb:analytics", *arguments, **options):
        path = tmp_path / "shop.yaml"
        path.write_text(json.dumps(shop(api, **options)))
        return cli.main(["run", str(path), "--output", output] + list(arguments))
    return run


def query(tmp_path, sql, database="warehouse.duckdb"):
    import duckdb
    connection = duckdb.connect(str(tmp_path / database), read_only=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def requested_days(api):
    days = [request["params"]["day"] for request in api.calls("/orders")]
    api.requests.clear()
    return days


@needs_dlt
def test_runs_load_into_the_destination_and_resume_from_it(streamwright_run, api, tmp_path):
    assert streamwright_run() == 0
    assert requested_days(api) == DAYS
    rows = query(tmp_path, "select id, day, amount, tags from analytics.orders order by id")
    assert [(row[1].isoformat(), row[2], json.loads(row[3])) for row in rows] == [(d, 10.5, ["new"]) for d in DAYS]
    types = dict((name, kind) for name, kind, *_ in query(tmp_path, "describe analytics.orders"))
    assert (types["id"], types["day"], types["amount"], types["tags"]) == ("BIGINT", "DATE", "DOUBLE", "JSON")
    assert query(tmp_path, "select id, meta__source from analytics.events") == [(1, "web")]  # JSON: dlt's columns
    assert os.listdir(str(tmp_path / "tmp")) == []  # the run's staged records and dlt folder are gone

    assert streamwright_run() == 0  # the state saved with the load: yesterday, read again with 1 day of lookback
    assert requested_days(api) == DAYS[1:]
    assert len(query(tmp_path, "select id from analytics.orders")) == 3  # merged on the primary key
    assert len(query(tmp_path, "select id from analytics.events")) == 1  # full refresh without a key: replaced


@needs_dlt
def test_transform_exports_load_as_tables_merged_on_their_key(streamwright_run, api, tmp_path):
    assert streamwright_run(transform=True) == 0
    rows = query(tmp_path, "select day, amount, orders from analytics.daily_sales order by day")
    assert [(row[0].isoformat(), row[1], row[2]) for row in rows] == [(day, 10.5, 1) for day in DAYS]
    assert streamwright_run(transform=True) == 0  # the days read again are computed again: their rows are replaced
    assert requested_days(api)[-2:] == DAYS[1:]
    assert len(query(tmp_path, "select day from analytics.daily_sales")) == 3
    assert os.listdir(str(tmp_path / "tmp")) == []  # the run's DuckDB folder is gone too


@needs_dlt
def test_unkeyed_exports_of_incremental_streams_are_appended(streamwright_run, api, tmp_path, caplog):
    assert streamwright_run(transform=True, transform_key=False) == 0
    with caplog.at_level(logging.WARNING, logger="streamwright.output"):
        assert streamwright_run(transform=True, transform_key=False) == 0
    # the second run reads yesterday (lookback) and today again: their totals are added, the first day is kept
    days = [row[0].isoformat() for row in query(tmp_path, "select day from analytics.daily_sales order by day")]
    assert days == [DAYS[0], DAYS[1], DAYS[1], DAYS[2], DAYS[2]]
    assert "stream 'daily_sales' has no primary_key, so dlt appends it" in caplog.text


@needs_dlt
def test_each_destination_keeps_its_own_state(streamwright_run, api, tmp_path, monkeypatch):
    def into(database):
        monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / database))
    into("a.duckdb")
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 0
    assert requested_days(api) == DAYS
    into("b.duckdb")
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "events") == 0  # b's state has no orders bookmark
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 0
    assert requested_days(api) == DAYS  # a new warehouse starts from `start`, whatever the other one loaded
    into("a.duckdb")
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 0
    assert requested_days(api) == DAYS[1:]  # a resumes from its own bookmark
    assert not os.path.exists(str(tmp_path / "dlt" / "pipelines"))  # no dlt working folder outlives a run


@needs_dlt
def test_failed_runs_and_loads_leave_nothing_behind(streamwright_run, api, tmp_path, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 0
    requested_days(api)
    api.routes[("GET", "/orders")] = lambda request: (500, {"error": "down"})
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 1  # the source failed

    load, failures = Pipeline.load, [RuntimeError("the destination is unavailable")]

    def flaky_load(pipeline, *args, **kwargs):
        if failures:
            raise failures.pop(0)
        return load(pipeline, *args, **kwargs)
    monkeypatch.setattr(Pipeline, "load", flaky_load)
    api.routes[("GET", "/orders")] = orders(500)
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 1  # the load failed
    assert os.listdir(str(tmp_path / "tmp")) == []

    requested_days(api)
    api.routes[("GET", "/orders")] = orders(600)
    assert streamwright_run("dlt:duckdb:analytics", "--stream", "orders") == 0
    assert requested_days(api) == DAYS[1:]  # from the last completed load, so nothing is skipped
    ids = sorted(row[0] for row in query(tmp_path, "select id from analytics.orders"))
    assert ids[0] == 600 and 500 not in ids and len(ids) == 4


@needs_dlt
def test_a_state_file_replaces_the_destination_state(streamwright_run, api, tmp_path):
    assert streamwright_run() == 0
    requested_days(api)
    assert streamwright_run("dlt:duckdb:analytics", "--state", str(tmp_path / "missing.json")) == 0
    assert requested_days(api) == DAYS  # a missing state file means starting over, as the warning says


@needs_dlt
def test_incremental_streams_without_a_key_are_appended_with_a_warning(streamwright_run, api, tmp_path, caplog):
    assert streamwright_run(orders_key=False) == 0
    assert streamwright_run(orders_key=False) == 0
    assert "stream 'orders' has no primary_key, so dlt appends it" in caplog.text
    assert len(query(tmp_path, "select id from analytics.orders")) == 5  # 3, then yesterday and today again


@needs_dlt
def test_partial_replace_streams_are_omitted_from_dlt_load(api, tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "warehouse.duckdb"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [page_stream(
        "orders", {"http": {"path": "/orders", "params": {"account": "{{ partition.account }}"}}},
        "SELECT partition->>'account' AS account, record->>'id' AS id FROM records", records={"path": "data"},
        partitions=[{"name": "account", "values": ["a", "b"]}], on_partition_error="skip")]}
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(source))

    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [{"id": request["params"]["account"] + "1"}]})
    assert cli.main(["run", str(path), "--output", "dlt:duckdb:analytics"]) == 0
    api.routes[("GET", "/orders")] = lambda request: (
        (500, {}) if request["params"]["account"] == "b" else (200, {"data": [{"id": "a2"}]}))
    with caplog.at_level(logging.WARNING, logger="streamwright.output"):
        assert cli.main(["run", str(path), "--output", "dlt:duckdb:analytics"]) == 0

    assert query(tmp_path, "SELECT account, id FROM analytics.orders ORDER BY account") == [("a", "a1"), ("b", "b1")]
    assert "stream 'orders' skipped partitions, so its table keeps the rows of the last complete run" in caplog.text


@needs_dlt
def test_the_filesystem_destination_needs_delta_tables_to_merge(streamwright_run, api, monkeypatch, caplog):
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args: None if name == "deltalake" else
                        find_spec(name, *args))
    assert streamwright_run("dlt:filesystem:lake") == 1
    assert 'merges on primary_key only in Delta tables: pip install "dlt[deltalake]"' in caplog.text
    assert api.calls("/orders") == []
    assert streamwright_run("dlt:filesystem:lake", "--stream", "events") == 0  # no primary key: plain files


@needs_delta
def test_the_filesystem_destination_merges_delta_tables(streamwright_run, api, tmp_path):
    import deltalake
    assert streamwright_run("dlt:filesystem:lake", "--stream", "orders") == 0
    assert streamwright_run("dlt:filesystem:lake", "--stream", "orders") == 0
    table = deltalake.DeltaTable(str(tmp_path / "lake" / "lake" / "orders")).to_pyarrow_table()
    assert sorted(table.column("id").to_pylist()) == sorted(int(day.replace("-", "")) for day in DAYS)


@needs_dlt
def test_staged_records_are_removed_even_if_closing_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "w.duckdb"))
    output = dlt_output.DltOutput("duckdb", None, {"name": "shop", "streams": []})
    output.write_schema("orders", {"type": "object"}, ["id"])
    output.write_record("orders", {"id": 1})

    class Broken(object):
        def close(self):
            raise OSError("disk full")
    path, _, schema, keys = output.streams["orders"]
    output.streams["orders"][1].close()
    output.streams["orders"] = (path, Broken(), schema, keys)
    with pytest.raises(OSError):
        output.close(failed=True)
    assert not os.path.exists(output.directory)


def test_dlt_and_destination_problems_are_input_errors(api, tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(shop(api)))

    def missing():
        raise ValueError('--output dlt:... needs dlt: pip install "streamwright[dlt]"')
    with monkeypatch.context() as patched:
        patched.setattr(dlt_output, "_import_dlt", missing)
        assert cli.main(["run", str(path), "--output", "dlt:duckdb"]) == 2
    assert 'pip install "streamwright[dlt]"' in caplog.text
    assert cli.main(["run", str(path), "--output", "dlt:"]) == 2
    assert "--output dlt needs a destination" in caplog.text
    if importlib.util.find_spec("dlt") is not None:
        monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            assert cli.main(["run", str(path), "--output", "dlt:no_such_destination"]) == 2
        assert "--output dlt:no_such_destination:" in caplog.text
    assert api.requests == []
