"""The files connector: formats, globs, pages, exact values, roots, missing files, and runs through SourceRunner."""

import datetime
import decimal
import gzip
import json
import logging
import os

import pytest
import yaml

pytest.importorskip("duckdb")
pytest.importorskip("streamwright.connectors.files.connector")

from streamwright.connectors.files import reader  # noqa: E402
from streamwright.connectors.files.connector import FilesConnector  # noqa: E402
from streamwright.connectors.files.reader import AccessError, ReadError, Reader, allowed_roots, format_of  # noqa: E402
from streamwright.core import cli
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.logs import RunMetrics  # noqa: E402
from streamwright.core.outputs.output import open_output  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "readers", "files_demo")
TODAY = datetime.date(2026, 10, 3)
BIG = "12345678901234567890.123456"


# * -------
# * helpers
# * -------

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def parquet(path, select):
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("COPY (%s) TO '%s' (FORMAT parquet)" % (select, path))
    finally:
        connection.close()
    return str(path)


def real(path):
    return os.path.realpath(str(path))


@pytest.fixture
def connector():
    """A FilesConnector registered for the test (pages of 1,000 records, as installed)."""
    component = FilesConnector()
    components.register(component)
    yield component
    components.unregister("files")


@pytest.fixture
def small_pages():
    """A FilesConnector that reads pages of 2 records."""
    component = FilesConnector(page_size=2)
    components.register(component)
    yield component
    components.unregister("files")


def context(connector, where=None, metrics=None):
    result = ConnectorContext(connector, Redactor(), metrics=metrics)
    result.where = where
    return result


def read(connector, root, arguments, roots=None):
    """The records of one `file.read` call (all pages)."""
    client = connector.connect({"roots": [str(item) for item in (roots or [root])]}, context(connector))
    try:
        return [record for page in connector.request(client, {"service": "file", "method": "read",
                                                              "arguments": arguments}, context(connector))
                for record in page]
    finally:
        client.close()


def sdk(name, **arguments):
    return {"name": name, "sdk": "files", "service": "file", "method": "read", "arguments": arguments}


def file_source(*streams, **top):
    return dict({"kind": "source", "name": "files", "auth": {"provider": "files", "roots": ["{{ config.root }}"]},
                 "streams": list(streams)}, **top)


def run(source, root, output=None, config=None, state=None, metrics=None, streams=None):
    output = output if output is not None else MemoryOutput()
    SourceRunner(source, dict({"root": str(root)}, **(config or {})), {}, state=state, output=output, today=TODAY,
                 sleep=lambda seconds: None, allowed_connectors=["files"], metrics=metrics).run(streams)
    return output


def records(output, name):
    return [record for stream, record in output.records if stream == name]


def orders_stream(path="data/{{ partition.region }}.csv", **extra):
    return page_stream("orders", sdk("raw_orders", path=path),
                       "SELECT (record->>'id')::BIGINT AS id, record->>'amount' AS amount, "
                       "partition->>'region' AS region FROM raw_orders", primary_key=["id"], **extra)


def regions(*values):
    return [{"name": "region", "values": list(values)}]


# * ----------------------------
# * formats and the record shape
# * ----------------------------

def test_csv_values_are_the_files_text_and_empty_cells_null(connector, tmp_path):
    write(tmp_path / "a.csv", "id,amount,day,name\n1,%s,2026-10-01,a\n2,,2026-10-02,\"b,c\"\n" % BIG)
    assert read(connector, tmp_path, {"path": "a.csv"}) == [
        {"id": "1", "amount": BIG, "day": "2026-10-01", "name": "a"},
        {"id": "2", "amount": None, "day": "2026-10-02", "name": "b,c"}]
    typed = read(connector, tmp_path, {"path": "a.csv", "options": {"columns": {
        "id": "BIGINT", "amount": "DECIMAL(38,6)", "day": "DATE", "name": "VARCHAR"}}})
    assert typed[0] == {"id": 1, "amount": decimal.Decimal(BIG), "day": "2026-10-01", "name": "a"}


def test_tsv_and_csv_options(connector, tmp_path):
    write(tmp_path / "a.tsv", "id\tname\n1\tx\n")
    assert read(connector, tmp_path, {"path": "a.tsv"}) == [{"id": "1", "name": "x"}]
    write(tmp_path / "b.txt", "# a comment line\nid;name\n1;NA\n")
    assert read(connector, tmp_path, {"path": "b.txt", "format": "csv", "options": {
        "delimiter": ";", "skip": 1, "nullstr": "NA", "header": True}}) == [{"id": "1", "name": None}]
    write(tmp_path / "c.csv", "1,x\n")
    assert read(connector, tmp_path, {"path": "c.csv", "options": {"header": False}}) == [
        {"column0": "1", "column1": "x"}]


def test_json_array_document_and_jsonl_records_are_kept_as_written(connector, tmp_path):
    write(tmp_path / "a.json", '[{"id": 1, "v": %s}, {"id": 2, "v": 0.1, "n": {"x": [1, 2]}}]' % BIG)
    assert read(connector, tmp_path, {"path": "a.json"}) == [
        {"id": 1, "v": decimal.Decimal(BIG)}, {"id": 2, "v": decimal.Decimal("0.1"), "n": {"x": [1, 2]}}]
    write(tmp_path / "d.json", '{"data": [{"id": 1}], "next": null}')
    assert read(connector, tmp_path, {"path": "d.json"}) == [{"data": [{"id": 1}], "next": None}]
    write(tmp_path / "l.jsonl", '{"id": 1, "v": 1.50}\n\n{"id": 2}\n')
    assert read(connector, tmp_path, {"path": "l.jsonl"}) == [{"id": 1, "v": decimal.Decimal("1.50")}, {"id": 2}]
    assert read(connector, tmp_path, {"path": "l.jsonl", "options": {"filename": True}})[1] == {
        "id": 2, "filename": "l.jsonl"}
    write(tmp_path / "lines.json", '{"id": 1}\n{"id": 2}\n')
    assert read(connector, tmp_path, {"path": "lines.json", "options": {"format": "newline_delimited"}}) == [
        {"id": 1}, {"id": 2}]


def test_compressed_files_take_their_format_from_the_name_before_the_compression(connector, tmp_path):
    with gzip.open(str(tmp_path / "a.jsonl.gz"), "wt") as stream:
        stream.write('{"id": 1}\n')
    assert format_of(str(tmp_path / "a.jsonl.gz")) == "jsonl"
    assert read(connector, tmp_path, {"path": "a.jsonl.gz"}) == [{"id": 1}]
    assert read(connector, tmp_path, {"path": "a.jsonl.gz", "options": {"compression": "gzip"}}) == [{"id": 1}]
    with gzip.open(str(tmp_path / "a.csv.gz"), "wt") as stream:
        stream.write("id\n7\n")
    assert read(connector, tmp_path, {"path": "a.csv.gz"}) == [{"id": "7"}]


def test_parquet_columns_become_exact_json_values(connector, tmp_path):
    parquet(tmp_path / "p.parquet", "SELECT 1::BIGINT AS id, %s::DECIMAL(38,6) AS amount, "
                                    "18446744073709551615::UBIGINT AS big, DATE '2026-10-01' AS day, "
                                    "TIMESTAMP '2026-10-01 01:02:03' AS at, 'x' AS s, [1, 2] AS l, "
                                    "{'k': 2.50::DECIMAL(4,2)} AS st, NULL::VARCHAR AS n" % BIG)
    assert read(connector, tmp_path, {"path": "p.parquet"}) == [
        {"id": 1, "amount": decimal.Decimal(BIG), "big": 18446744073709551615, "day": "2026-10-01",
         "at": "2026-10-01 01:02:03", "s": "x", "l": [1, 2], "st": {"k": decimal.Decimal("2.5")}, "n": None}]
    assert read(connector, tmp_path, {"path": "p.parquet", "options": {"filename": True}})[0]["filename"] == "p.parquet"


def test_bad_formats_and_unreadable_files_are_connector_errors(connector, tmp_path):
    write(tmp_path / "a.xml", "<a/>")
    with pytest.raises(ConnectorError, match="cannot tell the format of .*a.xml from its extension: set `format`"):
        read(connector, tmp_path, {"path": "a.xml"})
    write(tmp_path / "a.csv", "id\n1\n")
    with pytest.raises(ConnectorError, match="cannot read .*a.csv as parquet") as caught:
        read(connector, tmp_path, {"path": "a.csv", "format": "parquet"})
    assert caught.value.code == "READ_ERROR" and not caught.value.retryable
    with pytest.raises(ConnectorError, match="parquet files have no option 'header'"):
        read(connector, tmp_path, {"path": "*.csv", "format": "parquet", "options": {"header": True}})


# * -----------------
# * globs and paging
# * -----------------

def test_globs_read_every_matching_file_in_name_order(connector, tmp_path):
    write(tmp_path / "data" / "b.csv", "id\n2\n")
    write(tmp_path / "data" / "a.csv", "id\n1\n")
    write(tmp_path / "data" / "sub" / "c.csv", "id\n3\n")
    write(tmp_path / "data" / "skip.jsonl", '{"id": 9}\n')
    assert read(connector, tmp_path, {"path": "data/*.csv"}) == [{"id": "1"}, {"id": "2"}]
    assert read(connector, tmp_path, {"path": "data/**/*.csv"}) == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert read(connector, tmp_path, {"path": "data/*", "format": "csv", "options": {"filename": True}})[:2] == [
        {"id": "1", "filename": os.path.join("data", "a.csv")}, {"id": "2", "filename": os.path.join("data", "b.csv")}]
    # a list (e.g. a batch_size partition), absolute paths inside a root, each file read once
    assert read(connector, tmp_path, {"path": [str(tmp_path / "data" / "b.csv"), "data/*.csv"]}) == [
        {"id": "2"}, {"id": "1"}]


def test_files_are_read_in_pages_of_at_most_page_size_rows(small_pages, tmp_path):
    write(tmp_path / "a.csv", "id\n" + "".join("%d\n" % number for number in range(5)))
    client = small_pages.connect({"roots": [str(tmp_path)]}, context(small_pages))
    pages = list(small_pages.request(client, {"method": "read", "arguments": {"path": "a.csv"}},
                                     context(small_pages)))
    assert [len(page) for page in pages] == [2, 2, 1]
    assert reader.PAGE_SIZE == 1000


def test_large_files_land_in_pages_of_1000_records(connector, tmp_path):
    write(tmp_path / "big.csv", "id\n" + "".join("%d\n" % number for number in range(2500)))
    stream = page_stream("items", sdk("raw_items", path="big.csv"),
                         "SELECT count(*) AS n, min((record->>'id')::BIGINT) AS id FROM raw_items")
    metrics = RunMetrics()
    output = run(file_source(stream), tmp_path, metrics=metrics)
    assert [record["n"] for record in records(output, "items")] == [1000, 1000, 500]
    assert [record["id"] for record in records(output, "items")] == [0, 1000, 2000]
    stats = metrics.summary()["streams"][0]
    assert stats["pages"] == 3 and stats["records_read"] == 2500 and stats["requests"] == {"raw_items": 1}


# * -----------------------
# * roots: what can be read
# * -----------------------

@pytest.fixture
def guarded(tmp_path, monkeypatch):
    """(root, outside secret file, reads): a root with links leading outside it; `reads` records every file read."""
    root, outside = tmp_path / "root", tmp_path / "outside"
    secret = write(outside / "secret.csv", "id\n1\n")
    write(root / "data" / "east.csv", "id,amount\n1,1\n")
    os.symlink(secret, str(root / "link.csv"))
    os.symlink(str(outside), str(root / "linked"))
    reads = []
    pages = Reader.pages

    def spy(self, path, *args, **kwargs):
        reads.append(path)
        return pages(self, path, *args, **kwargs)
    monkeypatch.setattr(Reader, "pages", spy)
    return root, secret, reads


@pytest.mark.parametrize("path,message", [
    ("../outside/secret.csv", "goes up a folder"),
    ("data/../../outside/secret.csv", "goes up a folder"),
    ("SECRET", "is outside the roots"),
    ("link.csv", "is outside the roots"),
    ("*.csv", "leads outside the roots files can be read from .* through a symbolic link"),
    ("linked/*.csv", "is outside the roots"),
    ("linked/secret.csv", "is outside the roots"),
    ("s3://bucket/a.csv", "is a URL: the files connector reads local files only"),
    (["data/east.csv", "../outside/secret.csv"], "goes up a folder"),
])
def test_paths_outside_the_roots_are_refused_before_any_read(connector, guarded, path, message):
    root, secret, reads = guarded
    path = [item.replace("SECRET", secret) for item in path] if isinstance(path, list) else path.replace(
        "SECRET", secret)
    with pytest.raises(ConnectorError, match=message) as caught:
        read(connector, root, {"path": path})
    assert reads == []
    assert caught.value.code == "ACCESS_DENIED"


def test_the_reader_reads_only_inside_its_roots_and_stays_locked(guarded):
    root, secret, _ = guarded
    with Reader(allowed_roots([str(root)])) as client:
        assert list(client.pages(real(root / "data" / "east.csv"), "csv")) == [[{"id": "1", "amount": "1"}]]
        for path in (real(secret), str(root / "link.csv")):  # DuckDB refuses them too
            with pytest.raises(ReadError, match="Permission Error"):
                list(client.pages(path, "csv"))
        with pytest.raises(Exception):
            client.connection.execute("SET enable_external_access = true")
        folder = client.folder
    assert client.closed and not os.path.exists(folder)
    with pytest.raises(ReadError, match="closed"):
        list(client.pages(real(root / "data" / "east.csv"), "csv"))


def test_roots_must_be_existing_folders(connector, tmp_path):
    link = tmp_path / "link"
    os.symlink(str(tmp_path), str(link))
    assert allowed_roots([str(link), str(tmp_path)]) == [real(tmp_path)]
    with pytest.raises(ConnectorError, match="no such folder"):
        connector.connect({"roots": [str(tmp_path / "missing")]}, context(connector))
    with pytest.raises(ConnectorError, match="must be a non-empty list"):
        connector.connect({"roots": []}, context(connector))
    with pytest.raises(ConnectorError, match="must be a list of folders"):
        connector.connect({"roots": str(tmp_path)}, context(connector))
    with pytest.raises(AccessError, match="no folder"):
        allowed_roots([])


def test_a_path_naming_a_folder_is_an_error(connector, tmp_path):
    (tmp_path / "data").mkdir()
    with pytest.raises(ConnectorError, match="is a folder: name its files"):
        read(connector, tmp_path, {"path": "data"})


def test_relative_paths_are_relative_to_the_first_root_and_absolute_ones_may_use_any(connector, tmp_path):
    write(tmp_path / "one" / "a.csv", "id\n1\n")
    write(tmp_path / "two" / "a.csv", "id\n2\n")
    roots = [tmp_path / "one", tmp_path / "two"]
    assert read(connector, tmp_path, {"path": "a.csv"}, roots=roots) == [{"id": "1"}]
    assert read(connector, tmp_path, {"path": str(tmp_path / "two" / "a.csv")}, roots=roots) == [{"id": "2"}]
    with pytest.raises(ConnectorError, match="outside the roots"):
        read(connector, tmp_path, {"path": str(tmp_path / "two" / "a.csv")}, roots=roots[:1])


# * -------------
# * missing files
# * -------------

def test_a_missing_file_is_skipped_with_a_warning_or_fails_with_on_missing_error(connector, tmp_path, caplog):
    write(tmp_path / "data" / "east.csv", "id,amount\n1,1\n")
    source = file_source(orders_stream(partitions=regions("east", "north")))
    output = run(source, tmp_path)
    assert [record["id"] for record in records(output, "orders")] == [1]
    assert "files: no file matches 'data/north.csv'; skipping it (on_missing: skip)" in caplog.text
    assert read(connector, tmp_path, {"path": "data/*.parquet"}) == []
    source["streams"][0]["requests"][0]["arguments"]["on_missing"] = "error"
    with pytest.raises(SourceError, match="no file matches 'data/north.csv'"):
        run(source, tmp_path)
    source["streams"][0]["on_partition_error"] = "skip"
    output = run(source, tmp_path)
    assert [record["id"] for record in records(output, "orders")] == [1] and output.partial == ["orders"]


# * --------------------
# * runs: SourceRunner
# * --------------------

def test_partitions_read_a_file_each_steps_get_exact_values_and_reads_are_logged(connector, tmp_path, caplog):
    write(tmp_path / "data" / "east.csv", "id,amount\n1,%s\n2,2.50\n" % BIG)
    write(tmp_path / "data" / "west.csv", "id,amount\n3,7\n")
    caplog.set_level(logging.INFO, logger="streamwright.network")
    metrics = RunMetrics()
    output = run(file_source(orders_stream(partitions=regions("east", "west"))), tmp_path, metrics=metrics)
    assert records(output, "orders") == [{"id": 1, "amount": BIG, "region": "east"},
                                         {"id": 2, "amount": "2.50", "region": "east"},
                                         {"id": 3, "amount": "7", "region": "west"}]
    lines = [record for record in caplog.records if record.name == "streamwright.network"
             and getattr(record, "event", None) == "file_read"]
    size = os.path.getsize(str(tmp_path / "data" / "east.csv"))
    assert lines[0].getMessage() == ("stream 'orders', request 'raw_orders', partition {\"region\": \"east\"}: files "
                                     "read %s: 2 row(s), %d bytes, %.2f s" % (
                                         real(tmp_path / "data" / "east.csv"), size, lines[0].duration_ms / 1000.0))
    assert (lines[0].path, lines[0].records, lines[0].bytes, lines[0].connector) == (
        real(tmp_path / "data" / "east.csv"), 2, size, "files")
    stats = metrics.summary()["streams"][0]
    assert stats["records_read"] == 3 and stats["requests"] == {"raw_orders": 2}  # one call per file


def test_windows_read_a_file_per_day_and_bookmarks_advance(connector, tmp_path):
    for day in ("2026-10-01", "2026-10-03"):
        write(tmp_path / "daily" / (day + ".jsonl"), '{"id": %d, "day": "%s"}\n' % (int(day[-1]), day))
    stream = page_stream("daily", sdk("raw_daily", path="daily/{{ window.start }}.jsonl"),
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day, window_start "
                         "FROM raw_daily", primary_key=["id"],
                         incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"})
    output = run(file_source(stream), tmp_path)
    assert records(output, "daily") == [{"id": 1, "day": "2026-10-01", "window_start": "2026-10-01"},
                                        {"id": 3, "day": "2026-10-03", "window_start": "2026-10-03"}]
    assert output.states[-1]["bookmarks"]["daily"] == {"{}": "2026-10-02"}


def test_run_mode_joins_files_batches_ids_and_explodes_records(connector, tmp_path):
    write(tmp_path / "ids.csv", "id\n1\n2\n3\n")
    write(tmp_path / "details" / "1-2.jsonl", '{"id": 1, "tags": [{"tag": "a"}, {"tag": "b"}]}\n'
                                              '{"id": 2, "tags": [{"tag": "c"}]}\n')
    write(tmp_path / "details" / "3.jsonl", '{"id": 3, "tags": []}\n')
    parquet(tmp_path / "names.parquet", "SELECT * FROM (VALUES (1, 'one'), (2, 'two'), (3, 'three')) t(id, name)")
    tags = sdk("raw_tags", path="details/{{ partition.ids | join('-') }}.jsonl")
    tags.update(partitions=[{"name": "ids", "from": "raw_ids", "field": "id", "batch_size": 2}],
                records={"explode": "tags"})
    stream = {"name": "tags", "requests": [sdk("raw_ids", path="ids.csv"),
                                           sdk("raw_names", path="names.parquet", format="parquet"), tags],
              "transform": {"mode": "run", "steps": [{"name": "tags", "select": (
                  "SELECT (t.record->>'id')::BIGINT AS id, n.record->>'name' AS name, t.record->>'tag' AS tag, "
                  "t.partition->'ids' AS ids FROM raw_tags t JOIN raw_names n "
                  "ON (n.record->>'id') = (t.record->>'id')")}]},
              "export": {"tags": {"step": "tags", "primary_key": ["tag"]}}}
    output = run(file_source(stream), tmp_path)
    assert records(output, "tags") == [{"id": 1, "name": "one", "tag": "a", "ids": ["1", "2"]},
                                       {"id": 1, "name": "one", "tag": "b", "ids": ["1", "2"]},
                                       {"id": 2, "name": "two", "tag": "c", "ids": ["1", "2"]}]


def test_a_batch_of_file_names_reads_each_file_of_the_batch(connector, tmp_path):
    write(tmp_path / "files.csv", "file\nparts/a.csv\nparts/b.csv\nparts/c.csv\n")
    for name, number in (("a", 1), ("b", 2), ("c", 3)):
        write(tmp_path / "parts" / (name + ".csv"), "id\n%d\n" % number)
    parts = sdk("raw_parts", path="{{ partition.files }}", options={"filename": True})
    parts["partitions"] = [{"name": "files", "from": "raw_files", "field": "file", "batch_size": 2}]
    stream = {"name": "parts", "requests": [sdk("raw_files", path="files.csv"), parts],
              "transform": {"mode": "run", "steps": [{"name": "parts", "select": (
                  "SELECT (record->>'id')::BIGINT AS id, record->>'filename' AS file, "
                  "json_array_length(partition->'files') AS batch FROM raw_parts")}]},
              "export": {"parts": {"step": "parts", "primary_key": ["id"]}}}
    metrics = RunMetrics()
    output = run(file_source(stream), tmp_path, metrics=metrics)
    assert records(output, "parts") == [{"id": 1, "file": os.path.join("parts", "a.csv"), "batch": 2},
                                        {"id": 2, "file": os.path.join("parts", "b.csv"), "batch": 2},
                                        {"id": 3, "file": os.path.join("parts", "c.csv"), "batch": 1}]
    assert metrics.summary()["streams"][0]["requests"] == {"raw_files": 1, "raw_parts": 3}


def test_records_explode_lists_inside_each_record(connector, tmp_path):
    write(tmp_path / "a.json", '{"page": 1, "data": [{"id": 1}, {"id": 2}]}')
    write(tmp_path / "b.json", '{"page": 2, "data": [{"id": 3}]}')
    stream = page_stream("items", sdk("raw_items", path="*.json"),
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'page')::INTEGER AS page FROM raw_items",
                         records={"explode": "data"})
    assert records(run(file_source(stream), tmp_path), "items") == [{"id": 1, "page": 1}, {"id": 2, "page": 1},
                                                                    {"id": 3, "page": 2}]


@pytest.mark.parametrize("kind", ["jsonl", "parquet"])
def test_file_reads_write_to_file_outputs(connector, tmp_path, kind):
    write(tmp_path / "data" / "east.csv", "id,amount\n1,%s\n2,2.50\n" % BIG)
    out = tmp_path / "out"
    output = open_output("%s:%s" % (kind, out))
    stream = page_stream("orders", sdk("raw_orders", path="data/east.csv"),
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'amount')::DECIMAL(38,6) AS amount "
                         "FROM raw_orders", primary_key=["id"])
    run(file_source(stream), tmp_path, output=output)
    output.close()
    written = [name for name in os.listdir(str(out)) if name.startswith("orders.")]
    assert len(written) == 1
    if kind == "jsonl":
        with open(str(out / written[0])) as stream_:
            assert [json.loads(line, parse_float=decimal.Decimal)["amount"] for line in stream_] == [
                decimal.Decimal(BIG), decimal.Decimal("2.5")]
    else:
        import duckdb
        assert duckdb.connect().execute("SELECT id, amount FROM read_parquet(?) ORDER BY id",
                                        [str(out / written[0])]).fetchall() == [
            (1, decimal.Decimal(BIG)), (2, decimal.Decimal("2.5"))]


def test_paths_known_only_at_run_time_are_checked_before_their_read(connector, tmp_path):
    write(tmp_path / "ok.csv", "id\n1\n")
    write(tmp_path / "names.csv", "name\nok\n/etc/hosts\n")
    stream = page_stream("items", sdk("raw_items", path="{{ partition.name }}.csv"),
                         "SELECT record->>'id' AS id FROM raw_items",
                         partitions=[{"name": "name", "from_stream": "names", "field": "name"}])
    names = page_stream("names", sdk("raw_names", path="names.csv"), "SELECT record->>'name' AS name FROM raw_names")
    with pytest.raises(SourceError, match="'/etc/hosts.csv' is outside the roots"):
        run(file_source(names, stream), tmp_path)


# * ------
# * checks
# * ------

def check(connector, **arguments):
    return connector.check_request({"service": "file", "method": "read", "arguments": arguments})


def test_a_read_request_is_valid(connector):
    assert check(connector, path="data/{{ partition.region }}.csv") == []
    assert check(connector, path="{{ partition.files }}", format="csv", on_missing="error",
                 options={"header": True, "columns": {"id": "BIGINT", "amount": "DECIMAL(12,2)"},
                          "nullstr": ["", "NA"]}) == []
    assert check(connector, path=["a.csv", "b/*.parquet"]) == []
    assert connector.check_request({"method": "read", "arguments": {"path": "a.csv"}}) == []  # service: file


@pytest.mark.parametrize("arguments,expected", [
    ({}, "file.read needs `path`"),
    ({"path": "a.csv", "url": "x"}, "file.read does not take `url` (arguments: path, format, options, on_missing, "
                                    "match, recursive)"),
    ({"path": "{{ secrets.token }}/a.csv"}, "arguments cannot use secrets"),
    ({"path": "../a.csv"}, "path '../a.csv' goes up a folder (..)"),
    ({"path": "http://example.com/a.csv"}, "is a URL: the files connector reads local files only (object storage: "
                                           "the s3 and gcs connectors)"),
    ({"path": "s3://bucket/*.csv"}, "is a URL: the files connector reads local files only"),
    ({"path": ""}, "path '' is empty"),
    ({"path": []}, "`path` is an empty list"),
    ({"path": 3}, "`path` must be a file path"),
    ({"path": "a.csv", "format": "xlsx"}, "unknown `format` 'xlsx' (formats: auto, csv, tsv, json, jsonl, parquet)"),
    ({"path": "a.csv", "options": {"sheet": 1}}, "these files have no option 'sheet'"),
    ({"path": "a", "format": "parquet", "options": {"header": True}}, "parquet files have no option 'header'"),
    ({"path": "a.csv", "options": {"header": "yes"}}, "option 'header': expected true or false"),
    ({"path": "a.csv", "options": {"skip": -1}}, "option 'skip': expected a whole number >= 0"),
    ({"path": "a.csv", "options": {"compression": "zip"}}, "option 'compression': expected one of"),
    ({"path": "a.csv", "options": {"columns": []}}, "option 'columns': expected a mapping"),
    ({"path": "a.csv", "options": {"columns": {"id": "INT; DROP"}}}, "option 'columns': not a column type: id"),
    ({"path": "a.csv", "options": {"delimiter": "{{ config.d }}"}}, "literal values, not references"),
    ({"path": "a.csv", "options": []}, "`options` must be a mapping"),
    ({"path": "a.csv", "on_missing": "ignore"}, "unknown `on_missing` policy 'ignore' (policies: skip, error)"),
])
def test_read_requests_are_checked(connector, arguments, expected):
    found = check(connector, **arguments)
    assert any(expected in message for message in found), found


def test_unknown_services_and_methods_are_refused(connector):
    assert connector.check_request({"service": "disk", "method": "read", "arguments": {}}) == [
        "files: service 'disk' is not supported (supported: file)"]
    assert connector.check_request({"service": "file", "method": "write", "arguments": {}}) == [
        "files: file.write is not supported (supported: read)"]
    assert connector.check_request({"method": "read", "arguments": ["a.csv"]}) == [
        "files: `arguments` must be a mapping"]


@pytest.mark.parametrize("auth,expected", [
    ({"roots": ["{{ config.data_root }}"]}, []),
    ({"roots": "{{ config.roots }}"}, []),
    ({"roots": ["/data", "relative/folder"]}, []),
    ({}, ["auth: provider 'files' needs 'roots'"]),
    ({"roots": ["/data"], "token": "x"}, ["auth: provider 'files' does not support 'token' (supported: roots)"]),
    ({"roots": ["/data"], "s3": {"key_id": "{{ secrets.k }}"}}, ["auth: provider 'files' does not support 's3' "
                                                                 "(supported: roots)"]),
    ({"roots": []}, ["auth: provider 'files': `roots` must be a non-empty list of folders"]),
    ({"roots": "/data"}, ["auth: provider 'files': `roots` must be a list of folders, e.g. "
                          "[\"{{ config.data_root }}\"]"]),
    ({"roots": [""]}, ["auth: provider 'files': `roots` must be a list of folders (non-empty texts)"]),
    ({"roots": ["{{ secrets.root }}"]}, ["auth: provider 'files': `roots` cannot use secrets: roots are folders, not "
                                         "credentials"]),
    ({"roots": ["/data", "s3://bucket/prefix/"]}, ["auth: provider 'files': `roots`: 's3://bucket/prefix/' is a URL: "
                                                   "roots are local folders; the files connector reads local files "
                                                   "only (object storage: the s3 and gcs connectors)"]),
])
def test_auth_is_checked(connector, auth, expected):
    assert connector.check_auth(auth) == expected


def test_check_source_reports_connector_problems_without_reading_files(connector, tmp_path):
    source = file_source(orders_stream("missing/{{ partition.region }}.csv", partitions=regions("east")))
    assert components.check_source(source, ["files"]) == []
    source["streams"][0]["requests"][0]["arguments"]["format"] = "xlsx"
    source["auth"]["roots"] = "/data"
    assert components.check_source(source, ["files"]) == [
        "auth: provider 'files': `roots` must be a list of folders, e.g. [\"{{ config.data_root }}\"]",
        "stream 'orders': requests[0]: files: unknown `format` 'xlsx' (formats: auto, csv, tsv, json, jsonl, parquet)"]


def test_errors_map_to_connector_errors(connector):
    import duckdb
    assert connector.error(duckdb.IOException("boom")).code == "READ_ERROR"
    assert connector.error(AccessError("no")).code == "ACCESS_DENIED"
    assert connector.error(ValueError("x")) is None


# * -------------------------
# * the example and the CLI
# * -------------------------

def test_the_files_demo_runs_offline(tmp_path, caplog):
    out, summary = tmp_path / "out", tmp_path / "summary.json"
    data = os.path.join(EXAMPLE, "data")
    assert cli.main(["run", EXAMPLE, "--set", "data_root=%s" % data, "--allow-connector", "files",
                     "--output", "jsonl:%s" % out, "--summary", str(summary)]) == 0
    written = {}
    for name in os.listdir(str(out)):
        if name != "state.json":
            with open(str(out / name)) as stream:
                written[name.split(".")[0]] = [json.loads(line) for line in stream]
    assert sorted(written) == ["customers", "orders", "products"]
    assert [record["customer_id"] for record in written["customers"]] == ["cu_1", "cu_2", "cu_3"]
    assert [(record["order_id"], record["region"]) for record in written["orders"]] == [
        (1001, "east"), (1002, "east"), (1003, "east"), (2001, "west"), (2002, "west")]
    assert written["products"][2] == {"product_id": 3, "product_name": "Backpack", "unit_price": 149,
                                      "launched_on": "2026-03-10"}
    with open(str(summary)) as stream:
        streams = dict((item["name"], item) for item in json.load(stream)["streams"])
    assert streams["orders"]["requests"] == {"raw_orders": 2} and streams["products"]["records_read"] == 3


def test_the_files_demo_needs_the_connector_allowed_and_its_roots(tmp_path, caplog):
    out = "jsonl:%s" % (tmp_path / "out")
    assert cli.main(["run", EXAMPLE, "--set", "data_root=%s" % os.path.join(EXAMPLE, "data"),
                     "--allow-connector", "google_ads", "--output", out]) == 2
    assert "connector 'files' is not in the allowed list" in caplog.text
    caplog.clear()
    # a root that holds only the orders: the products file is not in it (skipped with a warning), and the root's
    # parent cannot be reached
    assert cli.main(["run", EXAMPLE, "--set", "data_root=%s" % os.path.join(EXAMPLE, "data", "orders"),
                     "--allow-connector", "files", "--stream", "products", "--output", out]) == 0
    assert "files: no file matches 'products.parquet'; skipping it" in caplog.text
    assert cli.main(["run", EXAMPLE, "--set", "data_root=%s" % os.path.join(EXAMPLE, "data", "missing"),
                     "--allow-connector", "files", "--stream", "products", "--output", out]) != 0
    assert "files: cannot connect: files: roots: %s: no such folder" % os.path.join(EXAMPLE, "data", "missing") \
        in caplog.text


def test_streamwright_validate_and_connectors(capsys, tmp_path):
    assert cli.main(["validate", EXAMPLE]) == 0
    assert cli.main(["connectors"]) == 0
    assert "files" in capsys.readouterr().out.split()
    bad = tmp_path / "source.yaml"
    with open(os.path.join(EXAMPLE, "source.yaml")) as stream:
        document = yaml.safe_load(stream)
    with open(os.path.join(EXAMPLE, "streams", "products.yaml")) as stream:
        products = yaml.safe_load(stream)
    products["requests"][0]["arguments"]["format"] = "xlsx"
    document["streams"] = [dict(products, name="products")]
    bad.write_text(yaml.safe_dump(document, sort_keys=False))
    assert cli.main(["validate", str(bad)]) == 1
    assert "unknown `format` 'xlsx'" in capsys.readouterr().out
