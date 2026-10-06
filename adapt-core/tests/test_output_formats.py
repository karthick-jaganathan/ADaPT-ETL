"""Output formats (tsv:DIR, parquet:DIR), every output's summary() and the lines outputs log when they close."""

import csv
import datetime
import importlib.util
import json
import logging
import os
import stat
from decimal import Decimal

import pytest

from adapt.core import cli
from adapt.core.outputs.output import ParquetOutput, _size, log_summary, open_output, parquet_type
from adapt.core.engine.runner import SourceRunner
from adapt.core.runtime.testing import page_stream


needs_dlt = pytest.mark.skipif(importlib.util.find_spec("dlt") is None,
                               reason="needs dlt (pip install 'adapt-core[dlt]' 'dlt[duckdb]')")
TODAY = datetime.date(2026, 10, 3)
CURSOR = {"type": "cursor", "token_path": "next", "param": "cursor"}
ID = "SELECT (record->>'id')::BIGINT AS id FROM records"


def write_source(tmp_path, source):
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(source))  # (JSON is YAML)
    return str(path)


def files_in(folder):
    """The paths of the files in a folder and its subfolders, relative to it."""
    return sorted(os.path.relpath(os.path.join(root, name), str(folder))
                  for root, _, names in os.walk(str(folder)) for name in names)


def query(sql, path=None):
    """Rows of a query in a DuckDB of its own (UTC); `path` replaces {path} in it, quoted."""
    import duckdb
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        if path is not None:
            sql = sql.replace("{path}", "'%s'" % str(path).replace("'", "''"))
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def rows_in(database, table):
    """The number of rows of a table (SCHEMA.TABLE) of a DuckDB file."""
    import duckdb
    connection = duckdb.connect(str(database), read_only=True)
    try:
        return connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
    finally:
        connection.close()


def two_exports(api, **top):
    """A page-mode stream read in two pages (ids 1, 2 then 3), with two exports: items and doubled."""
    pages = {"": {"data": [{"id": 1}, {"id": 2}], "next": "p2"}, "p2": {"data": [{"id": 3}]}}
    api.routes[("GET", "/items")] = lambda request: (200, pages[request["params"].get("cursor", "")])
    stream = page_stream("items", {"http": {"path": "/items"}, "paginator": CURSOR}, ID, records={"path": "data"})
    stream["transform"]["steps"].append({"name": "doubled_rows",
                                         "select": "SELECT id * 2 AS doubled, 'x' || id AS label FROM items"})
    stream["export"]["doubled"] = {"step": "doubled_rows"}
    return dict({"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [stream]}, **top)


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """The folder where outputs stage records (tempfile's), which must be empty once they close."""
    folder = tmp_path / "stage"
    folder.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(folder))
    return folder


# * ---
# * tsv
# * ---

ROWS = """SELECT (record->>'id')::BIGINT AS id, record->>'name' AS name, record->>'note' AS note,
                 (record->'tags')::VARCHAR[] AS tags, record->'meta' AS meta, (record->>'flag')::BOOLEAN AS flag,
                 (record->>'price')::DOUBLE AS price
          FROM records"""


def test_tsv_files_have_a_header_csv_values_and_quote_tabs_newlines_and_quotes(api, tmp_path, stage):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [
        {"id": 1, "name": "tab\there", "note": "line 1\nline 2\r\nline 3", "tags": ["a", "b"], "meta": {"k": 1},
         "flag": True, "price": 1.5},
        {"id": 2, "name": 'say "hi"', "note": None, "tags": [], "meta": None, "flag": False, "price": None}]})
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url},
              "streams": [page_stream("items", {"http": {"path": "/items"}}, ROWS, records={"path": "data"})]}
    path = write_source(tmp_path, source)
    for kind in ("tsv", "csv"):
        assert cli.main(["run", path, "--output", "%s:%s" % (kind, tmp_path / kind)]) == 0
    (tsv,) = os.listdir(str(tmp_path / "tsv"))
    assert tsv.startswith("items.") and tsv.endswith(".tsv")
    with open(str(tmp_path / "tsv" / tsv), "rb") as stream:
        text = stream.read().decode("utf-8")
    assert text == ('id\tname\tnote\ttags\tmeta\tflag\tprice\r\n'
                    '1\t"tab\there"\t"line 1\nline 2\r\nline 3"\ta,b\t"{""k"": 1}"\tTrue\t1.5\r\n'
                    '2\t"say ""hi"""\t\t\t\tFalse\t\r\n')
    (written,) = os.listdir(str(tmp_path / "csv"))
    with open(str(tmp_path / "csv" / written), newline="") as stream:
        rows = list(csv.reader(stream))
    with open(str(tmp_path / "tsv" / tsv), newline="") as stream:
        assert list(csv.reader(stream, dialect="excel-tab")) == rows  # the values of csv files, tab-separated
    assert rows[1] == ["1", "tab\there", "line 1\nline 2\r\nline 3", "a,b", '{"k": 1}', "True", "1.5"]
    assert os.listdir(str(stage)) == []


# * -------
# * parquet
# * -------

TYPED = """SELECT (record->>'id')::BIGINT AS id, (record->>'amount')::DECIMAL(9, 2) AS amount,
                  (record->>'day')::DATE AS day, (record->>'at')::TIMESTAMPTZ AS at,
                  (record->>'active')::BOOLEAN AS active, (record->>'ratio')::DOUBLE AS ratio,
                  record->>'name' AS name, record->'details' AS details, (record->'tags')::VARCHAR[] AS tags,
                  {'channel': record->>'channel'} AS place, (record->>'big')::HUGEINT AS big,
                  (record->>'uid')::UUID AS uid, to_seconds((record->>'wait')::BIGINT) AS wait,
                  (record->>'local')::TIMESTAMP AS local, (record->>'small')::UTINYINT AS small
           FROM records"""
UID = "5b8e6c2f-4a2e-4d3a-9b1a-2f0e1c3d4b5a"
BIG = 10 ** 38 - 1  # the largest DECIMAL(38,0): exact, where a DOUBLE would round it


def typed_source(api, big=BIG):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [
        {"id": 1, "amount": "12.34", "day": "2026-10-01", "at": "2026-10-01T10:00:00+02:00", "active": True,
         "ratio": 0.5, "name": "first", "details": {"a": [1, None], "b": "text"}, "tags": ["x", "y"],
         "channel": "web", "big": str(big), "uid": UID, "wait": 90, "local": "2026-10-01 10:00:00", "small": 7},
        {"id": 2, "details": "plain text", "big": str(-big)}]})
    return {"kind": "source", "name": "shop", "http": {"base_url": api.url},
            "streams": [page_stream("items", {"http": {"path": "/items"}}, TYPED, records={"path": "data"})]}


def test_parquet_files_keep_exact_types(api, tmp_path, stage, caplog):
    out = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        assert cli.main(["run", write_source(tmp_path, typed_source(api)), "--output", "parquet:%s" % out]) == 0
    (name,) = os.listdir(str(out))  # (a full-refresh stream: no state)
    assert name.startswith("items.") and name.endswith(".parquet")
    path = out / name
    types = dict(row[:2] for row in query("DESCRIBE SELECT * FROM read_parquet({path})", path))
    assert types == {
        "id": "BIGINT", "amount": "DECIMAL(9,2)", "day": "DATE", "at": "TIMESTAMP WITH TIME ZONE",
        "active": "BOOLEAN", "ratio": "DOUBLE", "name": "VARCHAR", "details": "VARCHAR", "tags": "VARCHAR",
        "place": "VARCHAR", "big": "DECIMAL(38,0)", "uid": "VARCHAR", "wait": "DOUBLE", "local": "TIMESTAMP",
        "small": "UTINYINT"}
    rows = query("SELECT * FROM read_parquet({path}) ORDER BY id", path)
    assert rows == [
        (1, Decimal("12.34"), datetime.date(2026, 10, 1),
         datetime.datetime(2026, 10, 1, 8, 0, tzinfo=datetime.timezone.utc), True, 0.5, "first",
         '{"a":[1,null],"b":"text"}', '["x","y"]', '{"channel":"web"}', Decimal(BIG), UID, 90.0,
         datetime.datetime(2026, 10, 1, 10, 0), 7),
        (2, None, None, None, None, None, None, '"plain text"', None, '{"channel":null}', Decimal(-BIG), None, None,
         None, None)]
    # JSON values are plain text columns (not Parquet's JSON type), and the file is compressed with zstd
    assert query("SELECT converted_type, logical_type FROM parquet_schema({path}) WHERE name IN ('details', 'tags')",
                 path) == [("UTF8", None), ("UTF8", None)]
    assert query("SELECT DISTINCT compression FROM parquet_metadata({path})", path) == [("ZSTD",)]
    assert "wrote %s: 2 records, %s" % (path, _size(os.path.getsize(str(path)))) in caplog.text
    assert os.listdir(str(stage)) == []


def test_parquet_type_follows_the_warehouse_types():
    for kind, expected in (("BIGINT", "BIGINT"), ("DECIMAL(18,4)", "DECIMAL(18,4)"), ("DATE", "DATE"),
                           ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE"), ("TIMESTAMP_NS", "TIMESTAMP"),
                           ("BOOLEAN", "BOOLEAN"), ("double", "DOUBLE"), ("HUGEINT", "DECIMAL(38,0)"),
                           ("UHUGEINT", "DECIMAL(38,0)"), ("UBIGINT", "UBIGINT"), ("UUID", "VARCHAR"),
                           ("ENUM('a', 'b')", "VARCHAR"), ("BLOB", "VARCHAR"), ("INTERVAL", "DOUBLE"),
                           ("JSON", "VARCHAR"), ("INTEGER[]", "VARCHAR"), ("VARCHAR[3]", "VARCHAR"),
                           ("STRUCT(a INTEGER)", "VARCHAR"), ("MAP(VARCHAR, INTEGER)", "VARCHAR")):
        assert parquet_type(kind) == expected, kind


def test_a_hugeint_too_large_for_parquet_fails_the_run_and_writes_nothing(api, tmp_path, stage, caplog):
    out = tmp_path / "out"
    path = write_source(tmp_path, typed_source(api, big=2 ** 127 - 1))  # 39 digits
    assert cli.main(["run", path, "--output", "parquet:%s" % out]) == 1
    assert "export 'items': cannot write its Parquet file: Conversion Error" in caplog.text
    assert "HUGEINT and UHUGEINT columns are DECIMAL(38,0) in Parquet files" in caplog.text
    assert os.listdir(str(out)) == [] and os.listdir(str(stage)) == []


@pytest.mark.parametrize("kind", ["parquet", "tsv"])
def test_page_mode_exports_keep_every_page_in_order(api, tmp_path, stage, kind):
    out = tmp_path / "out"
    assert cli.main(["run", write_source(tmp_path, two_exports(api)), "--output", "%s:%s" % (kind, out)]) == 0
    assert len(api.calls("/items")) == 2
    names = sorted(os.listdir(str(out)))
    assert [name.split(".")[0] for name in names] == ["doubled", "items"]
    assert all(name.endswith("." + kind) for name in names)
    if kind == "parquet":
        assert query("SELECT * FROM read_parquet({path})", out / names[1]) == [(1,), (2,), (3,)]
        assert query("SELECT * FROM read_parquet({path})", out / names[0]) == [(2, "x1"), (4, "x2"), (6, "x3")]
        assert dict(row[:2] for row in query("DESCRIBE SELECT * FROM read_parquet({path})", out / names[0])) == {
            "doubled": "BIGINT", "label": "VARCHAR"}
    else:
        assert (out / names[1]).read_text() == "id\n1\n2\n3\n"  # (CRLF, read as text)
        assert (out / names[0]).read_text() == "doubled\tlabel\n2\tx1\n4\tx2\n6\tx3\n"
    assert os.listdir(str(stage)) == []


def test_file_name_templates_name_tsv_and_parquet_files(api, tmp_path, stage):
    path = write_source(tmp_path, two_exports(api, spec={"config": {"client": {"type": "string"}}}))
    out = tmp_path / "parquet"
    for _ in range(2):  # the same names on the next run replace the files
        assert cli.main(["run", path, "--set", "client=acme", "--output", "parquet:%s" % out,
                         "--file-name", "{{ config.client }}/{{ export }}.parquet"]) == 0
    assert files_in(out) == [os.path.join("acme", "doubled.parquet"), os.path.join("acme", "items.parquet")]
    assert query("SELECT id FROM read_parquet({path})", out / "acme" / "items.parquet") == [(1,), (2,), (3,)]
    assert cli.main(["run", path, "--set", "client=acme", "--output", "tsv:%s" % (tmp_path / "tsv"),
                     "--file-name", "{{ source }}/{{ export | upper }}.tsv"]) == 0
    assert files_in(tmp_path / "tsv") == [os.path.join("shop", "DOUBLED.tsv"), os.path.join("shop", "ITEMS.tsv")]
    assert os.listdir(str(stage)) == []


@pytest.mark.parametrize("kind", ["parquet", "tsv"])
def test_failed_runs_write_no_files(api, tmp_path, stage, kind):
    source = two_exports(api)
    api.routes[("GET", "/items")] = lambda request: (
        (400, {"error": "bad cursor"}) if request["params"].get("cursor") else
        (200, {"data": [{"id": 1}], "next": "p2"}))  # the first page is written, then the run fails
    out = tmp_path / "out"
    assert cli.main(["run", write_source(tmp_path, source), "--output", "%s:%s" % (kind, out)]) == 1
    assert len(api.calls("/items")) == 2
    assert os.listdir(str(out)) == [] and os.listdir(str(stage)) == []


def test_parquet_output_from_python_types_columns_from_the_schema(tmp_path, stage, caplog):
    caplog.set_level(logging.DEBUG, logger="adapt.output")
    output = ParquetOutput(str(tmp_path / "out"))
    output.write_schema("typed", {"type": "object", "properties": {
        "id": {"type": ["null", "integer"]}, "at": {"type": ["null", "string"], "format": "date-time"},
        "tags": {"type": ["null", "array"]}}}, ["id"])
    output.write_schema("raw", {"type": "object"}, [])  # no properties: the record is one JSON column
    output.write_record("typed", {"id": 1, "at": "2026-10-01T10:00:00+00:00", "tags": ["a"]})
    output.write_record("raw", {"a": 1, "b": [True, None]})
    output.write_state({"bookmarks": {"typed": {"{}": "2026-10-02"}}})
    staged = output.staging.directory
    assert sorted(os.listdir(staged)) == ["0.jsonl", "1.jsonl"]  # staged on disk while the run goes
    hidden = sorted(os.listdir(str(tmp_path / "out")))
    assert len(hidden) == 2 and all(name.startswith(".") and name.endswith(".parquet.part") for name in hidden)
    output.close()
    assert not os.path.exists(staged)
    entries = output.summary()
    typed, raw = entries[0]["path"], entries[1]["path"]
    assert query("SELECT * FROM read_parquet({path})", typed) == [
        (1, datetime.datetime(2026, 10, 1, 10, 0, tzinfo=datetime.timezone.utc), '["a"]')]
    assert query("SELECT * FROM read_parquet({path})", raw) == [('{"a":1,"b":[true,null]}',)]
    state = tmp_path / "out" / "state.json"
    assert json.loads(state.read_text()) == {"bookmarks": {"typed": {"{}": "2026-10-02"}}}
    # DuckDB writes the files: they get the permissions of the output's other files
    assert stat.S_IMODE(os.stat(typed).st_mode) == stat.S_IMODE(os.stat(str(state)).st_mode)
    # adapt.output: the files written (INFO), and where the records were staged and how the files were typed (DEBUG)
    lines = [(record.name, record.levelname, record.getMessage()) for record in caplog.records]
    (typed_part,) = [name for name in hidden if name.startswith(".typed.")]
    assert ("adapt.output", "DEBUG", "export 'typed': staging its records in %s" % os.path.join(
        staged, "0.jsonl")) in lines
    assert ("adapt.output", "DEBUG", "export 'typed': DuckDB writes %s from %s, columns id BIGINT, at TIMESTAMP "
            "WITH TIME ZONE, tags VARCHAR" % (tmp_path / "out" / typed_part, os.path.join(staged, "0.jsonl"))) in lines
    assert ("adapt.output", "DEBUG", "removing the staged records in %s" % staged) in lines
    assert ("adapt.output", "INFO", "wrote %s: 1 record, %s" % (typed, _size(os.path.getsize(typed)))) in lines
    assert set(name for name, _, _ in lines) == {"adapt.output"}


# * ------------------------------------
# * summary() and the lines of closing
# * ------------------------------------

def shop(api):
    """orders: incremental (3 days, a record each), keyed; eventLog: full refresh, 1 record, no key."""
    api.routes[("GET", "/orders")] = lambda request: (200, {"data": [
        {"id": int(request["params"]["day"].replace("-", "")), "day": request["params"]["day"]}]})
    api.routes[("GET", "/events")] = lambda request: (200, {"data": [{"id": 1}]})
    orders = page_stream("orders", {"http": {"path": "/orders", "params": {"day": "{{ window.start }}"}}},
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day FROM records",
                         records={"path": "data"}, primary_key=["id"],
                         incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"})
    events = page_stream("eventLog", {"http": {"path": "/events"}}, ID, records={"path": "data"})
    return {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [orders, events]}


def run(source, output, failed=False):
    """Runs the source into an output, closes it and returns its summary()."""
    SourceRunner(source, {}, {}, output=output, today=TODAY, sleep=lambda seconds: None).run()
    output.close(failed=failed)
    return output.summary()


def closing_lines(caplog):
    """(message, fields) of the lines outputs log when they close (the event "output")."""
    return [(record.getMessage(), dict((key, getattr(record, key)) for key in (
        "export", "records", "path", "bytes", "table") if hasattr(record, key)))
        for record in caplog.records if getattr(record, "event", None) == "output"]


@pytest.mark.parametrize("kind", ["jsonl", "csv", "tsv", "parquet"])
def test_file_outputs_summarize_their_files(api, tmp_path, stage, caplog, kind):
    source, out = shop(api), tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output("%s:%s" % (kind, out), source))
    paths = dict((entry["export"], entry["path"]) for entry in entries)
    sizes = dict((name, os.path.getsize(path)) for name, path in paths.items())
    state = str(out / "state.json")
    assert entries == [
        {"export": "orders", "records": 3, "path": paths["orders"], "bytes": sizes["orders"], "state": state},
        {"export": "eventLog", "records": 1, "path": paths["eventLog"], "bytes": sizes["eventLog"], "state": state}]
    for name, path in paths.items():
        assert os.path.dirname(path) == str(out) and os.path.basename(path).startswith(name + ".")
        assert path.endswith("." + kind)
    assert sorted(os.listdir(str(out))) == sorted([os.path.basename(path) for path in paths.values()] + [
        "state.json"])
    assert closing_lines(caplog) == [
        ("wrote %s: 3 records, %s" % (paths["orders"], _size(sizes["orders"])),
         {"export": "orders", "records": 3, "path": paths["orders"], "bytes": sizes["orders"]}),
        ("wrote %s: 1 record, %s" % (paths["eventLog"], _size(sizes["eventLog"])),
         {"export": "eventLog", "records": 1, "path": paths["eventLog"], "bytes": sizes["eventLog"]})]
    assert os.listdir(str(stage)) == []


def test_singer_output_summarizes_its_messages(api, capsys, caplog):
    source = shop(api)
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output("singer", source))
    assert entries == [{"export": "orders", "records": 3, "state": "stdout"},
                       {"export": "eventLog", "records": 1, "state": "stdout"}]
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [message["type"] for message in messages].count("RECORD") == 4
    assert closing_lines(caplog) == [("wrote 'orders' to stdout: 3 records", {"export": "orders", "records": 3}),
                                     ("wrote 'eventLog' to stdout: 1 record", {"export": "eventLog", "records": 1})]


def _ducklake_available():
    """Whether DuckLake can run here: installed now, or installed on first use (as in test_warehouse_output)."""
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


@pytest.mark.parametrize("kind", ["duckdb", pytest.param("ducklake", marks=pytest.mark.skipif(
    not _ducklake_available(), reason="the DuckLake extension cannot be installed"))])
def test_warehouse_outputs_summarize_their_tables(api, tmp_path, stage, monkeypatch, caplog, kind):
    source, database = shop(api), tmp_path / ("warehouse." + kind)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("ADAPT_DUCKLAKE_DATA_PATH", str(tmp_path / "data"))
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output("%s:%s:client_a" % (kind, database), source))
    assert entries == [
        {"export": "orders", "records": 3, "table": "client_a.orders", "disposition": "merge",
         "primary_key": ["id"], "state": "client_a._adapt_state"},
        {"export": "eventLog", "records": 1, "table": "client_a.eventLog", "disposition": "replace",
         "primary_key": [], "state": "client_a._adapt_state"}]
    assert closing_lines(caplog) == [
        ("loaded client_a.orders: 3 rows (merge on id)", {"export": "orders", "records": 3,
                                                          "table": "client_a.orders"}),
        ("loaded client_a.eventLog: 1 row (replace)", {"export": "eventLog", "records": 1,
                                                       "table": "client_a.eventLog"})]
    if kind == "duckdb":
        assert [rows_in(database, entry["table"]) for entry in entries] == [3, 1]
    assert os.listdir(str(stage)) == []


def test_duckdb_summary_marks_tables_kept_from_the_last_complete_run(api, tmp_path, stage, caplog):
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [page_stream(
        "orders", {"http": {"path": "/orders", "params": {"account": "{{ partition.account }}"}}},
        "SELECT partition->>'account' AS account, record->>'id' AS id FROM records", records={"path": "data"},
        partitions=[{"name": "account", "values": ["a", "b"]}], on_partition_error="skip")]}
    api.routes[("GET", "/orders")] = lambda request: (
        (404, {}) if request["params"]["account"] == "b" else (200, {"data": [{"id": "a1"}]}))
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output("duckdb:%s" % (tmp_path / "warehouse.duckdb"), source))
    assert entries == [{"export": "orders", "records": 1, "table": "shop.orders", "disposition": "skipped",
                        "primary_key": []}]
    assert closing_lines(caplog) == [
        ("did not load shop.orders: 1 row (its stream skipped partitions: the table keeps the rows of the last "
         "complete run)", {"export": "orders", "records": 1, "table": "shop.orders"})]


@needs_dlt
def test_dlt_output_summarizes_its_tables_as_dlt_names_them(api, tmp_path, stage, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)  # dlt reads .dlt/ from the working directory
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "warehouse.duckdb"))
    source = shop(api)
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output("dlt:duckdb:analytics", source))
    state = "analytics._dlt_pipeline_state"
    assert entries == [
        {"export": "orders", "records": 3, "table": "analytics.orders", "disposition": "merge",
         "primary_key": ["id"], "state": state},
        {"export": "eventLog", "records": 1, "table": "analytics.event_log", "disposition": "replace",
         "primary_key": [], "state": state}]
    assert closing_lines(caplog) == [
        ("loaded analytics.orders: 3 rows (merge on id)", {"export": "orders", "records": 3,
                                                           "table": "analytics.orders"}),
        ("loaded analytics.event_log: 1 row (replace)", {"export": "eventLog", "records": 1,
                                                         "table": "analytics.event_log"})]
    assert [rows_in(tmp_path / "warehouse.duckdb", entry["table"]) for entry in entries] == [3, 1]
    assert os.listdir(str(stage)) == []


@pytest.mark.parametrize("spec", ["singer", "jsonl:{tmp}/out", "parquet:{tmp}/out", "duckdb:{tmp}/w.duckdb"])
def test_failed_runs_summarize_only_what_was_written(api, tmp_path, stage, capsys, caplog, spec):
    source = shop(api)
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        entries = run(source, open_output(spec.format(tmp=tmp_path), source), failed=True)
    if spec == "singer":  # its messages went out while the run went
        assert entries == [{"export": "orders", "records": 3, "state": "stdout"},
                           {"export": "eventLog", "records": 1, "state": "stdout"}]
    else:
        assert entries == []
    assert closing_lines(caplog) == []
    assert os.listdir(str(stage)) == []


def test_log_summary_lines(caplog):
    with caplog.at_level(logging.INFO, logger="adapt.output"):
        log_summary([
            {"export": "campaigns", "records": 1234, "path": "/out/campaigns.jsonl", "bytes": 42189},
            {"export": "campaigns", "records": 29, "table": "acme_google.campaigns", "disposition": "merge",
             "primary_key": ["customer_id", "campaign_id"], "state": "acme_google._adapt_state"},
            {"export": "stats", "records": 0, "table": "acme_google.stats", "disposition": "append",
             "primary_key": []},
            {"export": "events", "records": 1, "table": "acme_google.events", "disposition": "replace",
             "primary_key": []},
            {"export": "campaigns", "records": 2, "state": "stdout"}])
    assert [record.getMessage() for record in caplog.records] == [
        "wrote /out/campaigns.jsonl: 1,234 records, 41.2 KB",
        "loaded acme_google.campaigns: 29 rows (merge on customer_id, campaign_id)",
        "loaded acme_google.stats: 0 rows (append)",
        "loaded acme_google.events: 1 row (replace)",
        "wrote 'campaigns' to stdout: 2 records"]
    assert all(record.levelno == logging.INFO and record.name == "adapt.output" and record.event == "output"
               for record in caplog.records)


def test_sizes_are_written_for_people():
    assert [_size(count) for count in (0, 1, 1023, 1024, 42189, 1024 ** 2 - 1, 5 * 1024 ** 2, 3 * 1024 ** 3,
                                       2 * 1024 ** 4)] == [
        "0 bytes", "1 byte", "1,023 bytes", "1.0 KB", "41.2 KB", "1.0 MB", "5.0 MB", "3.0 GB", "2.0 TB"]
