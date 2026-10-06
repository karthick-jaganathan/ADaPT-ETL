"""
DECIMAL and HUGEINT values stay exact from SQL steps to every output: records keep decimal.Decimal values (and
Python's ints), JSON (Singer messages, jsonl files) writes them as exact numbers in plain notation, csv and tsv files
as the same text, and DuckDB, DuckLake, Parquet and dlt store them in DECIMAL columns.
"""

import csv
import datetime
import decimal
import importlib.util
import json
import os
from decimal import Decimal

import pytest

from adapt.core import cli
from adapt.core.outputs import dlt_output
from adapt.core.outputs.output import open_output
from adapt.core.engine.runner import SourceRunner
from adapt.core.runtime.templates import decimal_text, render, to_json, to_text
from adapt.core.runtime.testing import MemoryOutput, page_stream


needs_dlt = pytest.mark.skipif(importlib.util.find_spec("dlt") is None,
                               reason="needs dlt (pip install 'adapt-core[dlt]' 'dlt[duckdb]')")
TODAY = datetime.date(2026, 10, 3)


# * -----------------------------------------
# * a decimal's text: exact, plain notation
# * -----------------------------------------

@pytest.mark.parametrize("value,text", [
    ("2.60", "2.6"), ("100.00", "100"), ("12345678901234567890.123456", "12345678901234567890.123456"),
    ("0.00", "0"), ("-0.00", "0"), ("-1.50", "-1.5"), ("-100", "-100"), ("0.10", "0.1"), ("0.000001", "0.000001"),
    ("1E+2", "100"), ("1E-7", "0.0000001"), ("10", "10"),
    ("99999999999999999999999999999999999999", "99999999999999999999999999999999999999"),
    ("-99999999999999999999999999999999.999999", "-99999999999999999999999999999999.999999")])
def test_a_decimal_is_written_exactly_in_plain_notation(value, text):
    with decimal.localcontext() as context:
        context.prec = 6  # (nothing is rounded to the context's precision)
        assert decimal_text(Decimal(value)) == text
        assert to_text(Decimal(value)) == text
        assert to_json(Decimal(value)) == text  # a JSON number, never a string
        assert to_json({"a": [Decimal(value)]}) == '{"a": [%s]}' % text
    assert json.loads(text, parse_float=Decimal) == Decimal(value)


PLAIN = {"z": "text", "a": [1, 2.5, None, True, {"k": "v"}], "u": "\u00e9\n\"", "i": 10 ** 30, "f": 2.6, "e": 1e-07}


def test_to_json_writes_values_without_decimals_as_json_dumps_does():
    assert to_json(PLAIN) == json.dumps(PLAIN)
    assert to_json(PLAIN, sort_keys=True) == json.dumps(PLAIN, sort_keys=True)
    dated = dict(PLAIN, day=datetime.date(2026, 10, 1))
    assert to_json(dated, sort_keys=True, default=to_text) == json.dumps(dated, sort_keys=True, default=to_text)
    with pytest.raises(TypeError, match="Object of type date is not JSON serializable"):
        to_json(dated)


def test_to_json_writes_decimals_anywhere_as_exact_numbers():
    value = {"money": Decimal("2.60"), "nested": {"list": [Decimal("100.00"), {"x": Decimal("-0.50")}]},
             "huge": Decimal("12345678901234567890.123456"), "nan": Decimal("NaN"), "inf": Decimal("-Infinity"),
             "text": "2.60", "float": 2.6, "day": datetime.date(2026, 10, 1)}
    assert to_json(value, sort_keys=True, default=to_text) == (
        '{"day": "2026-10-01", "float": 2.6, "huge": 12345678901234567890.123456, "inf": null, "money": 2.6, '
        '"nan": null, "nested": {"list": [100, {"x": -0.5}]}, "text": "2.60"}')
    assert to_json({"z": Decimal("1.0"), "a": [Decimal("0.00")]}) == '{"z": 1, "a": [0]}'  # (the keys' order)
    # typical money reads as it did when DECIMAL values were floats
    assert to_json({"money": Decimal("2.60"), "cents": Decimal("0.10")}) == json.dumps({"money": 2.6, "cents": 0.1})


def test_templates_write_decimals_as_their_text_and_single_references_as_numbers():
    scopes = {"partition": {"amount": Decimal("2.60"), "id": Decimal("12345678901234567890"),
                            "precise": Decimal("12345678901234567890.123456"),
                            "tiers": [Decimal("1.0"), Decimal("2.50")]}}
    assert render("/prices/{{ partition.amount }}/{{ partition.precise }}?id={{ partition.id }}", scopes) == (
        "/prices/2.6/12345678901234567890.123456?id=12345678901234567890")
    # exactly one reference is a JSON number (an int when whole), which request bodies and connectors take
    rendered = render({"amount": "{{ partition.amount }}", "id": "{{ partition.id }}",
                       "tiers": "{{ partition.tiers }}", "joined": "{{ partition.tiers | join('/') }}"}, scopes)
    assert rendered == {"amount": 2.6, "id": 12345678901234567890, "tiers": [1, 2.5], "joined": "1/2.5"}
    assert [type(rendered["amount"]), type(rendered["id"])] == [float, int]
    assert to_text({"amount": Decimal("2.60")}) == '{"amount": 2.6}'
    assert to_text([Decimal("2.60"), Decimal("100.00")]) == "2.6,100"


# * -------------------------------------------------------
# * values that a double would round, in every output
# * -------------------------------------------------------

API_ROWS = [  # text, so that the API's JSON keeps every digit
    {"id": 1, "money": "2.60", "precise": "12345678901234567890.123456", "big": str(2 ** 53 + 1),
     "huge": "123456789012345678901234567890", "whole": "100.00", "zero": "0.00", "ratio": 2.6},
    {"id": 2, "money": "99999999999999999999999.99", "precise": "-0.000001", "big": str(2 ** 62),
     "huge": "876543210987654321098765432110", "whole": "-1.50", "zero": None, "ratio": None}]
ITEMS = """SELECT (record->>'id')::BIGINT AS id, (record->>'money')::DECIMAL(26, 2) AS money,
                  (record->>'precise')::DECIMAL(38, 6) AS precise, (record->>'big')::BIGINT AS big,
                  (record->>'huge')::HUGEINT AS huge, (record->>'whole')::DECIMAL(9, 2) AS whole,
                  (record->>'zero')::DECIMAL(9, 2) AS zero, (record->>'ratio')::DOUBLE AS ratio
           FROM records"""
TOTALS = "SELECT count(*) AS n, sum(money) AS money, sum(precise) AS precise, sum(big) AS big, sum(huge) AS huge " \
         "FROM items"
RECORDS = {
    "items": [
        {"id": 1, "money": Decimal("2.60"), "precise": Decimal("12345678901234567890.123456"), "big": 2 ** 53 + 1,
         "huge": 123456789012345678901234567890, "whole": Decimal("100.00"), "zero": Decimal("0.00"), "ratio": 2.6},
        {"id": 2, "money": Decimal("99999999999999999999999.99"), "precise": Decimal("-0.000001"), "big": 2 ** 62,
         "huge": 876543210987654321098765432110, "whole": Decimal("-1.50"), "zero": None, "ratio": None}],
    "totals": [{"n": 2, "money": Decimal("100000000000000000000002.59"),
                "precise": Decimal("12345678901234567890.123455"), "big": 2 ** 53 + 1 + 2 ** 62, "huge": 10 ** 30}]}
JSON_TEXT = {  # (keys sorted)
    "items": [
        '{"big": 9007199254740993, "huge": 123456789012345678901234567890, "id": 1, "money": 2.6, '
        '"precise": 12345678901234567890.123456, "ratio": 2.6, "whole": 100, "zero": 0}',
        '{"big": 4611686018427387904, "huge": 876543210987654321098765432110, "id": 2, '
        '"money": 99999999999999999999999.99, "precise": -0.000001, "ratio": null, "whole": -1.5, "zero": null}'],
    "totals": ['{"big": 4620693217682128897, "huge": 1000000000000000000000000000000, '
               '"money": 100000000000000000000002.59, "n": 2, "precise": 12345678901234567890.123455}']}
CSV_ROWS = {
    "items": [["id", "money", "precise", "big", "huge", "whole", "zero", "ratio"],
              ["1", "2.6", "12345678901234567890.123456", "9007199254740993", "123456789012345678901234567890", "100",
               "0", "2.6"],
              ["2", "99999999999999999999999.99", "-0.000001", "4611686018427387904",
               "876543210987654321098765432110", "-1.5", "", ""]],
    "totals": [["n", "money", "precise", "big", "huge"],
               ["2", "100000000000000000000002.59", "12345678901234567890.123455", "4620693217682128897",
                "1000000000000000000000000000000"]]}
COLUMNS = {
    "items": [("id", "BIGINT"), ("money", "DECIMAL(26,2)"), ("precise", "DECIMAL(38,6)"), ("big", "BIGINT"),
              ("huge", "HUGEINT"), ("whole", "DECIMAL(9,2)"), ("zero", "DECIMAL(9,2)"), ("ratio", "DOUBLE")],
    "totals": [("n", "BIGINT"), ("money", "DECIMAL(38,2)"), ("precise", "DECIMAL(38,6)"), ("big", "HUGEINT"),
               ("huge", "HUGEINT")]}


def exact_source(api, huge=True):
    """
    One page of API_ROWS, exported as `items` (keyed on id) and `totals`, their sums; `huge`: with the HUGEINT column
    `huge`, beyond BIGINT.
    """
    api.routes[("GET", "/items")] = lambda request: (200, {"data": API_ROWS})
    items, totals = ITEMS, TOTALS
    if not huge:
        items = items.replace("(record->>'huge')::HUGEINT AS huge, ", "")
        totals = totals.replace(", sum(huge) AS huge", "")
    stream = page_stream("items", {"http": {"path": "/items"}}, items, records={"path": "data"}, primary_key=["id"])
    stream["transform"]["steps"].append({"name": "totals", "select": totals})
    stream["export"]["totals"] = {"step": "totals"}
    return {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [stream]}


def expected_rows(name, columns):
    """The records of an export as tuples of these columns' values, typed as `columns` are (DECIMAL: Decimal)."""
    return [tuple(Decimal(record[column]) if kind.startswith("DECIMAL") and isinstance(record[column], int)
                  else record[column] for column, kind in columns) for record in RECORDS[name]]


def write_source(tmp_path, source):
    path = tmp_path / "shop.yaml"
    path.write_text(json.dumps(source))  # (JSON is YAML)
    return str(path)


def read_table(connection, name):
    """(columns as (name, type), rows ordered by the first column) of a table, without dlt's own columns."""
    columns = [(row[0], row[1]) for row in connection.execute("DESCRIBE SELECT * FROM %s" % name).fetchall()
               if not row[0].startswith("_dlt_")]
    rows = connection.execute("SELECT %s FROM %s ORDER BY 1" % (
        ", ".join('"%s"' % column for column, _ in columns), name)).fetchall()
    return columns, rows


def duckdb_table(database, name):
    import duckdb
    connection = duckdb.connect(str(database), read_only=True)
    try:
        return read_table(connection, name)
    finally:
        connection.close()


def test_records_keep_exact_decimals_and_integers(api):
    output = MemoryOutput()
    SourceRunner(exact_source(api), {}, {}, output=output, today=TODAY, sleep=lambda seconds: None).run()
    for name in ("items", "totals"):
        assert [record for export, record in output.records if export == name] == RECORDS[name]
        assert output.columns[name] == COLUMNS[name]
    item, totals = output.records[0][1], output.records[-1][1]
    assert [type(item[key]) for key in ("money", "precise", "big", "huge", "ratio")] == [
        Decimal, Decimal, int, int, float]
    assert [type(totals[key]) for key in ("money", "precise", "big", "huge")] == [Decimal, Decimal, int, int]


@pytest.mark.parametrize("kind", ["singer", "jsonl"])
def test_json_outputs_write_exact_numbers(api, tmp_path, capsys, kind):
    out = tmp_path / "out"
    arguments = ["run", write_source(tmp_path, exact_source(api))]
    assert cli.main(arguments + (["--output", "jsonl:%s" % out] if kind == "jsonl" else [])) == 0
    texts = {}
    if kind == "singer":
        for line in capsys.readouterr().out.splitlines():
            message = json.loads(line)
            if message["type"] == "RECORD":  # (keys sorted: the record comes first)
                end = line.index(', "stream": "%s"' % message["stream"])
                texts.setdefault(message["stream"], []).append(line[len('{"record": '):end])
    else:
        for name in os.listdir(str(out)):
            texts[name.split(".")[0]] = (out / name).read_text().splitlines()
    assert texts == JSON_TEXT
    for name, lines in texts.items():  # (floats: their shortest text)
        assert [json.loads(line, parse_float=Decimal) for line in lines] == [dict(
            (key, Decimal(repr(value)) if isinstance(value, float) else value) for key, value in record.items())
            for record in RECORDS[name]]


@pytest.mark.parametrize("kind", ["csv", "tsv"])
def test_csv_and_tsv_files_write_the_exact_text(api, tmp_path, kind):
    out = tmp_path / "out"
    assert cli.main(["run", write_source(tmp_path, exact_source(api)), "--output", "%s:%s" % (kind, out)]) == 0
    rows = {}
    for name in os.listdir(str(out)):
        with open(str(out / name), newline="") as stream:
            rows[name.split(".")[0]] = list(csv.reader(stream, dialect="excel-tab" if kind == "tsv" else "excel"))
    assert rows == CSV_ROWS


def test_parquet_files_hold_exact_decimals(api, tmp_path):
    import duckdb
    out = tmp_path / "out"
    assert cli.main(["run", write_source(tmp_path, exact_source(api)), "--output", "parquet:%s" % out]) == 0
    connection = duckdb.connect(":memory:")
    try:
        for name in os.listdir(str(out)):
            export = name.split(".")[0]
            columns, rows = read_table(connection, "read_parquet('%s')" % str(out / name).replace("'", "''"))
            # HUGEINT columns are DECIMAL(38,0) in Parquet files
            assert columns == [(column, "DECIMAL(38,0)" if kind == "HUGEINT" else kind)
                               for column, kind in COLUMNS[export]]
            assert rows == expected_rows(export, columns)
    finally:
        connection.close()


def _ducklake_available():
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
def test_duckdb_and_ducklake_tables_hold_exact_decimals(api, tmp_path, monkeypatch, kind):
    source, database, data = exact_source(api), tmp_path / ("warehouse." + kind), tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ADAPT_DUCKLAKE_DATA_PATH", str(data))
    output = open_output("%s:%s" % (kind, database), source)
    SourceRunner(source, {}, {}, output=output, today=TODAY, sleep=lambda seconds: None).run()
    output.close()
    for name in ("items", "totals"):
        if kind == "duckdb":
            columns, rows = duckdb_table(database, "shop.%s" % name)
        else:
            import duckdb
            connection = duckdb.connect(":memory:")
            try:
                connection.execute("LOAD ducklake")
                connection.execute("ATTACH 'ducklake:%s' AS lake (DATA_PATH '%s')" % (
                    str(database).replace("'", "''"), str(data).replace("'", "''")))
                columns, rows = read_table(connection, "lake.shop.%s" % name)
            finally:
                connection.close()
        assert columns == COLUMNS[name]
        assert rows == expected_rows(name, columns)


@needs_dlt
def test_dlt_loads_exact_decimals(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # dlt reads .dlt/ from the working directory
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "warehouse.duckdb"))
    source = exact_source(api, huge=False)  # (dlt's integer columns are BIGINT)
    output = open_output("dlt:duckdb", source)
    SourceRunner(source, {}, {}, output=output, today=TODAY, sleep=lambda seconds: None).run()
    output.close()
    for name in ("items", "totals"):
        columns, rows = duckdb_table(tmp_path / "warehouse.duckdb", "shop.%s" % name)
        assert columns == [(column, "BIGINT" if kind == "HUGEINT" else kind) for column, kind in COLUMNS[name]
                           if column != "huge"]
        assert rows == expected_rows(name, columns)


@needs_dlt
def test_dlt_tables_keep_the_type_of_a_column_they_have(tmp_path, monkeypatch):
    """A DECIMAL column that an earlier load made a double (as loads did before decimals were exact) stays one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("DESTINATION__DUCKDB__CREDENTIALS", str(tmp_path / "warehouse.duckdb"))
    schema = {"type": "object", "properties": {"id": {"type": ["null", "integer"]},
                                               "price": {"type": ["null", "number"]}}}
    for columns, record in ((None, {"id": 1, "price": 2.6}),
                            ([("id", "BIGINT"), ("price", "DECIMAL(9,2)")], {"id": 2, "price": Decimal("3.10")})):
        output = dlt_output.DltOutput("duckdb", None, {"name": "shop", "streams": []})
        output.initial_state()
        if columns is not None:
            output.write_columns("prices", columns)
        output.write_schema("prices", schema, ["id"])
        output.write_record("prices", record)
        output.close()
    assert duckdb_table(tmp_path / "warehouse.duckdb", "shop.prices") == (
        [("id", "BIGINT"), ("price", "DOUBLE")], [(1, 2.6), (2, 3.1)])


# * ----------------------------------------------------------------------------------------------
# * typical money (2.60) reads 2.6, as it did when DECIMAL values were floats: records, partitions
# * ----------------------------------------------------------------------------------------------

def money_source(api):
    """Typical money values, as DECIMAL (`money`) and as DOUBLE (`approx`)."""
    api.routes[("GET", "/money")] = lambda request: (200, {"data": [
        {"v": value} for value in ("2.60", "0.10", "12.34", "1234567.89", "0.01", "-3.75")]})
    stream = page_stream("money", {"http": {"path": "/money"}}, "SELECT (record->>'v')::DECIMAL(12, 2) AS money, "
                         "(record->>'v')::DOUBLE AS approx FROM records", records={"path": "data"})
    return {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [stream]}


@pytest.mark.parametrize("kind", ["singer", "jsonl", "csv", "tsv"])
def test_typical_money_is_written_as_its_double_was(api, tmp_path, capsys, kind):
    out = tmp_path / "out"
    arguments = ["run", write_source(tmp_path, money_source(api))]
    assert cli.main(arguments + ([] if kind == "singer" else ["--output", "%s:%s" % (kind, out)])) == 0
    if kind == "singer":
        messages = [json.loads(line, parse_float=str) for line in capsys.readouterr().out.splitlines()]
        records = [message["record"] for message in messages if message["type"] == "RECORD"]
    elif kind == "jsonl":
        (name,) = os.listdir(str(out))
        records = [json.loads(line, parse_float=str) for line in (out / name).read_text().splitlines()]
    if kind in ("singer", "jsonl"):  # (parse_float=str: the numbers' text)
        texts = [(record["money"], record["approx"]) for record in records]
    else:
        (name,) = os.listdir(str(out))
        with open(str(out / name), newline="") as stream:
            texts = [tuple(row) for row in csv.reader(stream, dialect="excel-tab" if kind == "tsv" else "excel")][1:]
    assert texts == [(text, text) for text in ("2.6", "0.1", "12.34", "1234567.89", "0.01", "-3.75")]


def test_partitions_from_decimal_columns_render_as_their_plain_text(api):
    """`from_stream` values from a DECIMAL column, and request partitions from a step's DECIMAL column."""
    api.routes[("GET", "/tiers")] = lambda request: (200, {"data": [
        {"amount": value} for value in ("2.60", "100", "12345678901234567890.123456", "2.6")]})
    for path in ("/prices/2.6", "/prices/100.0", "/prices/12345678901234567890.123456"):
        for method in ("GET", "POST"):
            api.routes[(method, path)] = lambda request: (200, {"data": [{"price": "1.10"}]})
    tiers = page_stream("tiers", {"http": {"path": "/tiers"}},
                        "SELECT DISTINCT (record->>'amount')::DECIMAL(38, 6) AS amount FROM records ORDER BY 1",
                        records={"path": "data"})
    prices = page_stream(
        "prices", {"http": {"method": "POST", "path": "/prices/{{ partition.amount }}",
                            "params": {"amount": "{{ partition.amount }}", "text": "a{{ partition.amount }}"},
                            "json": {"amount": "{{ partition.amount }}"}}},
        "SELECT partition->>'amount' AS amount, window_start AS day, (record->>'price')::DECIMAL(9, 2) AS price "
        "FROM records", records={"path": "data"},
        partitions=[{"name": "amount", "from_stream": "tiers", "field": "amount"}],
        incremental={"cursor_field": "day", "start": TODAY.isoformat(), "window": "1d"})
    report = {"name": "report", "requests": [
        {"name": "raw_tiers", "http": {"path": "/tiers"}, "records": {"path": "data"}},
        {"name": "raw_prices", "partitions": [{"name": "tier", "from": "tier_rows", "field": "amount"}],
         "http": {"path": "/prices/{{ partition.tier }}"}, "records": {"path": "data"}}],
        "transform": {"mode": "run", "steps": [
            {"name": "tier_rows", "select": "SELECT (record->>'amount')::DECIMAL(38, 6) AS amount FROM raw_tiers"},
            {"name": "price_rows", "select": "SELECT partition->>'tier' AS tier, "
                                             "(record->>'price')::DECIMAL(9, 2) AS price FROM raw_prices"}]},
        "export": {"report": {"step": "price_rows"}}}
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [tiers, prices, report]}
    output = MemoryOutput()
    SourceRunner(source, {}, {}, output=output, today=TODAY, sleep=lambda seconds: None).run()
    paths = ["/prices/2.6", "/prices/100.0", "/prices/12345678901234567890.123456"]
    # a partition value from a DECIMAL column is the double it was before decimals were exact when that double is
    # exact (2.6, 100.0: requests, steps and bookmarks keep their text), else the exact decimal, which a double
    # rounded; exactly one reference is a JSON number
    posts = [request for request in api.requests if request["method"] == "POST"]
    assert [request["path"] for request in posts] == paths
    assert [request["params"]["text"] for request in posts] == ["a2.6", "a100.0", "a12345678901234567890.123456"]
    assert [request["params"]["amount"] for request in posts[:2]] == ["2.6", "100.0"]
    assert [request["body"] for request in posts[:2]] == ['{"amount": 2.6}', '{"amount": 100.0}']
    # request partitions from a step: distinct values, in order
    assert [request["path"] for request in api.requests if request["method"] == "GET" and request["path"] in paths] \
        == paths
    # steps read a partition's values as JSON numbers, with the text they had (merge keys made of them stay)
    assert [record["amount"] for export, record in output.records if export == "prices"][:2] == ["2.6", "100.0"]
    assert [record["tier"] for export, record in output.records if export == "report"][:2] == ["2.6", "100.0"]
    assert [record["price"] for export, record in output.records if export == "prices"] == [Decimal("1.10")] * 3
    # bookmarks are keyed by the partitions as JSON: typical values have the keys they had; a value a double rounded
    # has its exact number
    assert sorted(output.states[-1]["bookmarks"]["prices"]) == sorted([
        '{"amount": 2.6}', '{"amount": 100.0}', '{"amount": 12345678901234567890.123456}'])


def test_bookmarks_saved_with_decimal_partitions_as_doubles_still_resume(api):
    """
    Before DECIMAL values were exact, a `from_stream` partition's DECIMAL values were doubles, and its bookmarks were
    keyed so ({"amount": 1.2345678901234567e+19}): a state saved then resumes, its keys moved to the exact ones.
    """
    amounts = ("2.60", "100", "12345678901234567890.123456")
    api.routes[("GET", "/tiers")] = lambda request: (200, {"data": [{"amount": value} for value in amounts]})
    for value in ("2.6", "100.0", "12345678901234567890.123456"):
        api.routes[("GET", "/prices/" + value)] = lambda request: (200, {"data": [{"price": "1.10"}]})
    tiers = page_stream("tiers", {"http": {"path": "/tiers"}},
                        "SELECT (record->>'amount')::DECIMAL(38, 6) AS amount FROM records ORDER BY 1",
                        records={"path": "data"})
    prices = page_stream(
        "prices", {"http": {"path": "/prices/{{ partition.amount }}", "params": {"day": "{{ window.start }}"}}},
        "SELECT partition->>'amount' AS amount, window_start AS day, (record->>'price')::DECIMAL(9, 2) AS price "
        "FROM records", records={"path": "data"},
        partitions=[{"name": "amount", "from_stream": "tiers", "field": "amount"}],
        incremental={"cursor_field": "day", "start": (TODAY - datetime.timedelta(days=5)).isoformat(),
                     "window": "1d"})
    source = {"kind": "source", "name": "shop", "http": {"base_url": api.url}, "streams": [tiers, prices]}
    yesterday = (TODAY - datetime.timedelta(days=1)).isoformat()
    # the keys the commit before exact decimals saved: json.dumps of the partition, its decimals as doubles
    legacy = dict((json.dumps({"amount": float(Decimal(value))}, sort_keys=True), yesterday) for value in amounts)
    assert '{"amount": 1.2345678901234567e+19}' in legacy and '{"amount": 100.0}' in legacy
    state = {"bookmarks": {"prices": dict(legacy, **{'{"amount": 7.5}': "2026-01-01"})}}
    output = MemoryOutput()
    SourceRunner(source, {}, {}, state=state, output=output, today=TODAY, sleep=lambda seconds: None).run()
    days = [(request["path"][len("/prices/"):], request["params"]["day"]) for request in api.requests
            if request["path"].startswith("/prices/")]
    # each partition resumes the day after its bookmark: only today is read
    assert days == [("2.6", TODAY.isoformat()), ("100.0", TODAY.isoformat()),
                    ("12345678901234567890.123456", TODAY.isoformat())]
    assert output.states[-1]["bookmarks"]["prices"] == {
        '{"amount": 2.6}': yesterday, '{"amount": 100.0}': yesterday,
        '{"amount": 12345678901234567890.123456}': yesterday,  # moved from the double's key
        '{"amount": 7.5}': "2026-01-01"}  # (other partitions' bookmarks stay)
    assert state["bookmarks"]["prices"]['{"amount": 1.2345678901234567e+19}'] == yesterday  # (the caller's state)
