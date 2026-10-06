"""Streams: named requests, SQL steps (page and run mode), exports, request and `from_stream` partitions."""

import collections
import datetime
import json
import logging
import re
import time

import pytest

from adapt.core import cli
from adapt.core.engine import sql
from adapt.core.outputs.output import open_output
from adapt.core.engine.runner import SourceError, SourceRunner, partition_key
from adapt.core.runtime.testing import MemoryOutput, page_stream


TODAY = datetime.date(2026, 10, 3)
DAYS = ["2026-10-01", "2026-10-02", "2026-10-03"]
CAMPAIGNS = [{"id": "c1", "name": "Brand", "account": "a1"}, {"id": "c2", "name": "Generic", "account": "a1"},
             {"id": "c3", "name": "Other", "account": "a2"}]
CLICKS = {"c1": 1, "c2": 11, "c3": 21}
CURSOR = {"type": "cursor", "token_path": "next", "param": "cursor"}
SELECT_PATH = ("transform", "steps", 0, "select")


def source_with(api, *streams, **top):
    source = {"kind": "source", "name": "demo", "auth": {"type": "bearer", "token": "{{ secrets.token }}"},
              "http": {"base_url": api.url}, "streams": list(streams)}
    source.update(top)
    return source


def run_mode(name, requests, steps, export, **stream):
    return dict({"name": name, "requests": requests, "transform": {"mode": "run", "steps": steps},
                 "export": export}, **stream)


def report_stream():
    """Campaigns (read once) and daily stats (read per window), joined and summed into two exports."""
    return run_mode("daily_reports", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns"}, "records": {"path": "data"}},
        {"name": "raw_stats", "http": {"path": "/stats", "params": {"day": "{{ window.start }}"}},
         "records": {"path": "data"}}], [
        {"name": "campaigns", "select": "SELECT record->>'id' AS campaign_id, record->>'name' AS campaign_name, "
                                        "record->>'account' AS account_id FROM raw_campaigns"},
        {"name": "stats", "select": "SELECT record->>'campaign' AS campaign_id, (record->>'day')::DATE AS day, "
                                    "(record->>'clicks')::BIGINT AS clicks FROM raw_stats"},
        {"name": "campaign_daily_rows",
         "select": "SELECT s.*, c.campaign_name, c.account_id FROM stats s JOIN campaigns c USING (campaign_id)"},
        {"name": "account_daily_rows",
         "select": "SELECT account_id, day, sum(clicks)::BIGINT AS clicks FROM campaign_daily_rows GROUP BY ALL"}], {
        "campaign_daily": {"step": "campaign_daily_rows", "primary_key": ["campaign_id", "day"]},
        "account_daily": {"step": "account_daily_rows", "primary_key": ["account_id", "day"]}},
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"})


def demo(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": CAMPAIGNS})
    api.routes[("GET", "/stats")] = lambda request: (200, {"data": [
        {"campaign": campaign["id"], "day": request["params"]["day"], "clicks": str(CLICKS[campaign["id"]])}
        for campaign in CAMPAIGNS]})
    return source_with(api, report_stream())


def run(source, streams=None, state=None, output=None, config=None):
    output = output if output is not None else MemoryOutput()
    SourceRunner(source, config or {}, {"token": "secret-token-1"}, state=state, output=output, today=TODAY,
                 sleep=lambda seconds: None).run(streams)
    return output


def records(output, name):
    return [record for stream, record in output.records if stream == name]


def steps_of(source, index=0):
    return source["streams"][index]["transform"]["steps"]


# * --------
# * run mode
# * --------

def test_run_mode_runs_requests_and_steps_and_writes_every_export(api):
    output = run(demo(api))
    assert list(output.schemas) == ["campaign_daily", "account_daily"]
    # the request that uses the window runs once per window, the other once
    assert (len(api.calls("/campaigns")), len(api.calls("/stats"))) == (1, 3)
    assert records(output, "account_daily") == [
        {"account_id": account, "day": day, "clicks": clicks}
        for account, clicks in (("a1", 12), ("a2", 21)) for day in DAYS]
    assert len(records(output, "campaign_daily")) == 9
    assert output.schemas["account_daily"] == ({"type": "object", "properties": {
        "account_id": {"type": ["null", "string"]}, "day": {"type": ["null", "string"], "format": "date"},
        "clicks": {"type": ["null", "integer"]}}}, ["account_id", "day"])
    assert output.columns["account_daily"] == [("account_id", "VARCHAR"), ("day", "DATE"), ("clicks", "BIGINT")]
    # the state is written once, after the exports
    assert output.states == [{"bookmarks": {"daily_reports": {"{}": "2026-10-02"}}}]


def test_selection_takes_stream_and_export_names_and_a_stream_writes_all_its_exports(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = demo(api)
    source["streams"].append(page_stream("items", {"http": {"path": "/items"}},
                                         "SELECT (record->>'id')::BIGINT AS id FROM records",
                                         records={"path": "data"}))
    assert list(run(source, streams=["account_daily"]).schemas) == ["campaign_daily", "account_daily"]
    assert list(run(source, streams=["daily_reports"]).schemas) == ["campaign_daily", "account_daily"]
    assert list(run(source, streams=["items"]).schemas) == ["items"]
    assert list(run(source).schemas) == ["campaign_daily", "account_daily", "items"]
    with pytest.raises(SourceError, match=r"unknown stream\(s\): nope \(streams/exports: account_daily, "
                                          r"campaign_daily, daily_reports, items\)"):
        run(source, streams=["nope"])


def test_export_keys_are_checked_and_rows_ordered(api):
    source = demo(api)
    source["streams"][0]["export"]["account_daily"]["primary_key"] = ["account_id"]
    output = MemoryOutput()
    with pytest.raises(SourceError, match=r"stream 'daily_reports': export 'account_daily': primary_key "
                                          r"\(account_id\) is not unique"):
        run(source, output=output)
    assert output.records == [] and output.states == []  # no export is written when one fails
    source = demo(api)
    steps_of(source)[3]["select"] = (
        "SELECT CASE WHEN account_id <> 'a2' THEN account_id END AS account_id, day, sum(clicks) AS clicks "
        "FROM campaign_daily_rows GROUP BY ALL")
    with pytest.raises(SourceError, match=r"primary_key column\(s\) are null: 'account_id' in 3 row\(s\)"):
        run(source)


def test_requests_partition_from_a_request_and_a_step(api):
    api.routes[("GET", "/parents")] = lambda request: (200, {"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}]})
    api.routes[("GET", "/children")] = lambda request: (200, {"data": [{"id": request["params"]["parent"] + "1"}]})
    api.routes[("GET", "/grand")] = lambda request: (200, {"data": [{"id": request["params"]["child"] + "x"}]})
    source = source_with(api, run_mode("tree", [
        {"name": "raw_parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "raw_children", "partitions": [{"name": "parent", "from": "raw_parents", "field": "id"}],
         "http": {"path": "/children", "params": {"parent": "{{ partition.parent }}"}}, "records": {"path": "data"}},
        {"name": "raw_grand", "partitions": [{"name": "child", "from": "child_rows", "field": "child_id"}],
         "http": {"path": "/grand", "params": {"child": "{{ partition.child }}"}}, "records": {"path": "data"}}], [
        {"name": "child_rows",
         "select": "SELECT partition->>'parent' AS parent_id, record->>'id' AS child_id FROM raw_children"},
        {"name": "grand_rows",
         "select": "SELECT partition->>'child' AS child_id, record->>'id' AS grand_id FROM raw_grand"}],
        {"tree": {"step": "grand_rows", "primary_key": ["child_id", "grand_id"]}}))
    assert records(run(source), "tree") == [{"child_id": "a1", "grand_id": "a1x"},
                                            {"child_id": "b1", "grand_id": "b1x"}]
    assert [request["params"]["parent"] for request in api.calls("/children")] == ["a", "b"]  # distinct values


def test_run_mode_stream_that_skips_a_partition_writes_the_others_and_their_bookmarks(api, caplog):
    def stats(request):
        if request["params"]["campaign"] == "a2-c2" and request["params"]["day"] == "2026-10-02":
            return 500, {}
        return 200, {"data": [{"clicks": "1"}]}
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [
        {"id": request["params"]["account"] + "-c1"}, {"id": request["params"]["account"] + "-c2"}]})
    api.routes[("GET", "/stats")] = stats
    source = source_with(api, run_mode("report", [
        {"name": "campaign_list", "http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}},
        # partitioned from a step: runs after it, so the step ran on a2's campaigns before a2 failed; `account`
        # names the stream partition, so each account's calls use only its own campaigns
        {"name": "daily_stats", "partitions": [{"from": "campaign_rows", "fields": ["account", "campaign_id"]}],
         "http": {"path": "/stats", "params": {"campaign": "{{ partition.campaign_id }}",
                                               "day": "{{ window.start }}"}},
         "records": {"path": "data"}}], [
        {"name": "campaign_rows",
         "select": "SELECT partition->>'account' AS account, record->>'id' AS campaign_id FROM campaign_list"},
        {"name": "stat_rows",
         "select": "SELECT partition->>'account' AS account, partition->>'campaign_id' AS campaign_id, "
                   "window_start AS day, (record->>'clicks')::BIGINT AS clicks FROM daily_stats"}], {
        "campaigns_out": {"step": "campaign_rows", "primary_key": ["campaign_id"]},
        "stats_out": {"step": "stat_rows", "primary_key": ["campaign_id", "day"]}},
        on_partition_error="skip", partitions=[{"name": "account", "values": ["a1", "a2"]}],
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"}))
    state = {"bookmarks": {"report": {partition_key({"account": "a2"}): "2026-09-30"}}}
    with caplog.at_level(logging.WARNING, logger="adapt.source"):
        output = run(source, state=state)
    assert 'stream \'report\': skipping partition {"account": "a2"}: request \'daily_stats\': GET ' in caplog.text
    # a2's rows (its campaigns and the days read before its failure) are left out; the steps ran again without them
    assert records(output, "campaigns_out") == [{"account": "a1", "campaign_id": "a1-c1"},
                                                {"account": "a1", "campaign_id": "a1-c2"}]
    assert records(output, "stats_out") == [{"account": "a1", "campaign_id": campaign, "day": day, "clicks": 1}
                                            for campaign in ("a1-c1", "a1-c2") for day in DAYS]
    assert output.partial == ["campaigns_out", "stats_out"]
    assert output.states == [{"bookmarks": {"report": {partition_key({"account": "a2"}): "2026-09-30",
                                                       partition_key({"account": "a1"}): "2026-10-02"}}}]


def test_run_mode_resolves_five_step_chain_and_multiple_exports(api):
    api.routes[("GET", "/numbers")] = lambda request: (200, {"data": [{"id": 1, "value": 2}, {"id": 2, "value": 3}]})
    source = source_with(api, run_mode(
        "chain", [{"name": "numbers", "http": {"path": "/numbers"}, "records": {"path": "data"}}], [
            {"name": "step1", "select": "SELECT (record->>'id')::BIGINT AS id, (record->>'value')::BIGINT AS value "
                                        "FROM numbers"},
            {"name": "step2", "select": "SELECT id, value * 2 AS doubled FROM step1"},
            {"name": "step3", "select": "SELECT id, value + 10 AS plus_ten FROM step1"},
            {"name": "step4", "select": "SELECT id, doubled + 1 AS from_step2 FROM step2"},
            {"name": "step5", "select": "SELECT step1.id, value, plus_ten, from_step2 FROM step1 "
                                        "JOIN step3 USING (id) JOIN step4 USING (id)"}], {
            "chain_result": {"step": "step5", "primary_key": ["id"]},
            "plus_ten": {"step": "step3", "primary_key": ["id"]},
            "plus_ten_copy": {"step": "step3", "primary_key": ["id"]}}))
    output = run(source)
    assert records(output, "chain_result") == [{"id": 1, "value": 2, "plus_ten": 12, "from_step2": 5},
                                               {"id": 2, "value": 3, "plus_ten": 13, "from_step2": 7}]
    assert records(output, "plus_ten") == [{"id": 1, "plus_ten": 12}, {"id": 2, "plus_ten": 13}]
    assert records(output, "plus_ten_copy") == records(output, "plus_ten")


def test_run_mode_mixes_windowed_and_non_window_requests_and_partition_bookmarks(api):
    api.routes[("GET", "/daily")] = lambda request: (200, {"data": [{
        "account": request["params"]["account"], "day": request["params"]["day"], "clicks": "1"}]})
    api.routes[("GET", "/profile")] = lambda request: (200, {"data": [{"account": request["params"]["account"]}]})
    source = source_with(api, run_mode("daily", [
        {"name": "daily_raw", "records": {"path": "data"},
         "http": {"path": "/daily", "params": {"account": "{{ partition.account }}", "day": "{{ window.start }}"}}},
        {"name": "profiles", "http": {"path": "/profile", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}}], [
        {"name": "daily_rows", "select": "SELECT partition->>'account' AS account, (record->>'day')::DATE AS day, "
                                         "(record->>'clicks')::BIGINT AS clicks FROM daily_raw"},
        {"name": "profile_windows",
         "select": "SELECT partition->>'account' AS account, window_start, window_end FROM profiles"}], {
        "daily": {"step": "daily_rows", "primary_key": ["account", "day"]},
        "account_profiles": {"step": "profile_windows", "primary_key": ["account"]}},
        partitions=[{"name": "account", "values": ["a1", "a2"]}],
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"}))
    output = run(source)
    assert (len(api.calls("/daily")), len(api.calls("/profile"))) == (6, 2)
    assert records(output, "account_profiles") == [{"account": "a1", "window_start": None, "window_end": None},
                                                   {"account": "a2", "window_start": None, "window_end": None}]
    assert output.states == [{"bookmarks": {"daily": {partition_key({"account": "a1"}): "2026-10-02",
                                                      partition_key({"account": "a2"}): "2026-10-02"}}}]


def test_run_mode_request_reads_every_page_and_later_streams_reuse_its_names(api):
    def page(request):
        if "cursor" not in request["params"]:
            return 200, {"data": [{"id": 1}, {"id": 2}], "next": "p2"}
        return 200, {"data": [{"id": 3}]}
    api.routes[("GET", "/items")] = page
    api.routes[("GET", "/other")] = lambda request: (200, {"data": [{"id": 9}]})
    select = "SELECT (record->>'id')::BIGINT AS id FROM records"
    source = source_with(api, run_mode(
        "items", [{"name": "records", "http": {"path": "/items"}, "records": {"path": "data"}, "paginator": CURSOR}],
        [{"name": "items", "select": select}], {"items": {"step": "items", "primary_key": ["id"]}}),
        page_stream("other", {"http": {"path": "/other"}}, select, records={"path": "data"}))
    output = run(source)
    assert records(output, "items") == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert records(output, "other") == [{"id": 9}]


def test_requests_explode_their_records(api):
    api.routes[("GET", "/accounts")] = lambda request: (200, {"data": [
        {"id": "a1", "campaigns": [{"campaign": "c1"}, {"campaign": "c2"}]}, {"id": "a2", "campaigns": []}]})
    select = "SELECT record->>'id' AS account_id, record->>'campaign' AS campaign_id FROM %s"
    records_spec = {"path": "data", "explode": "campaigns"}
    source = source_with(api, run_mode(
        "listed", [{"name": "account_list", "http": {"path": "/accounts"}, "records": records_spec}],
        [{"name": "listed_rows", "select": select % "account_list"}], {"listed": {"step": "listed_rows"}}),
        page_stream("paged", {"http": {"path": "/accounts"}}, select % "records", records=records_spec))
    output = run(source)
    expected = [{"account_id": "a1", "campaign_id": "c1"}, {"account_id": "a1", "campaign_id": "c2"}]
    assert records(output, "listed") == expected and records(output, "paged") == expected


def test_each_stream_drops_its_tables_when_it_ends(api, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = demo(api)
    source["streams"].append(page_stream("items", {"http": {"path": "/items"}},
                                         "SELECT (record->>'id')::BIGINT AS id FROM records",
                                         records={"path": "data"}))
    tables = []
    run_stream = SourceRunner.run_stream

    def run_and_list_tables(self, stream):
        run_stream(self, stream)
        tables.append((stream["name"], [row[0] for row in self.workspace().connection.execute(
            "SELECT table_name FROM duckdb_tables()").fetchall()]))
    monkeypatch.setattr(SourceRunner, "run_stream", run_and_list_tables)
    run(source)
    assert tables == [("daily_reports", []), ("items", [])]


def test_run_mode_order_ignores_the_case_of_names(api):
    api.routes[("GET", "/parents")] = lambda request: (200, {"data": [{"id": "a"}, {"id": "b"}]})
    api.routes[("GET", "/children")] = lambda request: (200, {"data": [{"id": request["params"]["parent"] + "1"}]})
    source = source_with(api, run_mode("tree", [
        {"name": "Parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "Children", "partitions": [{"name": "parent", "from": "Parent_Rows", "field": "id"}],
         "http": {"path": "/children", "params": {"parent": "{{ partition.parent }}"}}, "records": {"path": "data"}}], [
        {"name": "parent_rows", "select": "SELECT record->>'id' AS id FROM PARENTS"},
        {"name": "child_rows", "select": "SELECT record->>'id' AS child_id FROM children"}],
        {"tree_out": {"step": "Child_Rows"}}))
    assert records(run(source), "tree_out") == [{"child_id": "a1"}, {"child_id": "b1"}]


def test_run_mode_step_failures_name_the_stream_and_step_and_write_nothing(api):
    # the value that cannot be converted is a secret: the message is redacted
    api.routes[("GET", "/bad")] = lambda request: (200, {"data": [{"id": "secret-token-1",
                                                                   "day": request["params"]["day"]}]})
    source = source_with(api, run_mode(
        "bad", [{"name": "raw_bad", "http": {"path": "/bad", "params": {"day": "{{ window.start }}"}},
                 "records": {"path": "data"}}],
        [{"name": "bad_rows", "select": "SELECT CAST(record->>'id' AS INTEGER) AS id, (record->>'day')::DATE AS day "
                                        "FROM raw_bad"}],
        {"bad": {"step": "bad_rows", "primary_key": ["id"]}},
        incremental={"cursor_field": "day", "start": "2026-10-03", "window": "1d"}))
    output = MemoryOutput()
    with pytest.raises(SourceError) as error:
        run(source, output=output)
    assert str(error.value).startswith("stream 'bad': step 'bad_rows': Conversion Error: Could not convert string "
                                       "'***' to INT32")
    assert "secret-token-1" not in str(error.value)
    assert (output.records, output.schemas, output.states) == ([], {}, [])


# * ---------
# * page mode
# * ---------

def test_page_mode_runs_steps_and_exports_per_page(api):
    pages = [[{"id": 2}], [{"id": 1}]]

    def page(request):
        data = pages.pop(0)
        return 200, dict({"data": data}, **({"next": "again"} if pages else {}))
    api.routes[("GET", "/items")] = page
    source = source_with(api, page_stream("items", {"http": {"path": "/items"}, "paginator": CURSOR},
                                          "SELECT (record->>'id')::BIGINT AS id FROM records",
                                          records={"path": "data"}, primary_key=["id"]))
    assert records(run(source), "items") == [{"id": 2}, {"id": 1}]


def test_page_mode_stream_that_skips_a_partition_writes_the_others_and_their_bookmarks(api):
    api.routes[("GET", "/stats")] = lambda request: (
        (500, {}) if request["params"]["account"] == "a2" and request["params"]["day"] == "2026-10-02" else
        (200, {"data": [{"clicks": "1"}]}))
    source = source_with(api, page_stream(
        "stats", {"name": "raw_stats", "http": {"path": "/stats", "params": {
            "account": "{{ partition.account }}", "day": "{{ window.start }}"}}},
        "SELECT partition->>'account' AS account, window_start AS day, (record->>'clicks')::BIGINT AS clicks "
        "FROM raw_stats", records={"path": "data"}, primary_key=["account", "day"], on_partition_error="skip",
        partitions=[{"name": "account", "values": ["a1", "a2"]}],
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"}))
    output = run(source)
    # page mode writes each page as it comes: a2's first day is written and saved
    assert [(record["account"], record["day"]) for record in records(output, "stats")] == [
        ("a1", day) for day in DAYS] + [("a2", "2026-10-01")]
    assert output.partial == ["stats"]
    assert output.states[-1] == {"bookmarks": {"stats": {partition_key({"account": "a1"}): "2026-10-02",
                                                         partition_key({"account": "a2"}): "2026-10-01"}}}


def test_page_mode_windows_state_per_window_and_duplicate_keys_across_pages(api):
    def page(request):
        page_number = request["params"].get("cursor", "first")
        return 200, dict({"data": [{"id": 1, "day": request["params"]["day"], "page": page_number}]},
                         **({"next": "second"} if page_number == "first" else {}))
    api.routes[("GET", "/items")] = page
    source = source_with(api, page_stream(
        "items", {"name": "items_raw", "http": {"path": "/items", "params": {"day": "{{ window.start }}"}},
                  "paginator": CURSOR},
        "SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day, record->>'page' AS page FROM items_raw",
        records={"path": "data"}, primary_key=["id"],
        incremental={"cursor_field": "day", "start": "2026-10-02", "window": "1d"}))
    output = run(source)
    assert len(api.calls("/items")) == 4
    assert records(output, "items") == [  # keys are not checked across pages
        {"id": 1, "day": "2026-10-02", "page": "first"}, {"id": 1, "day": "2026-10-02", "page": "second"},
        {"id": 1, "day": "2026-10-03", "page": "first"}, {"id": 1, "day": "2026-10-03", "page": "second"}]
    assert output.states == [{"bookmarks": {"items": {"{}": "2026-10-02"}}}] * 2


class CountingOutput(MemoryOutput):
    """MemoryOutput that counts the columns and schemas written of each export."""

    def __init__(self):
        super(CountingOutput, self).__init__()
        self.written = collections.Counter()

    def write_columns(self, name, columns):
        self.written[("columns", name)] += 1
        super(CountingOutput, self).write_columns(name, columns)

    def write_schema(self, name, schema, key_properties):
        self.written[("schema", name)] += 1
        super(CountingOutput, self).write_schema(name, schema, key_properties)


def test_page_mode_writes_each_export_schema_once_so_outputs_keep_every_page(api, tmp_path):
    pages = {"": {"data": [{"id": 1}, {"id": 2}], "next": "p2"}, "p2": {"data": [{"id": 3}]}}
    api.routes[("GET", "/items")] = lambda request: (200, pages[request["params"].get("cursor", "")])
    stream = page_stream("items", {"http": {"path": "/items"}, "paginator": CURSOR},
                         "SELECT (record->>'id')::BIGINT AS id FROM records", records={"path": "data"})
    stream["transform"]["steps"].append({"name": "doubled_rows", "select": "SELECT id * 2 AS doubled FROM items"})
    stream["export"]["doubled"] = {"step": "doubled_rows"}
    source = source_with(api, stream)
    output = run(source, output=CountingOutput())
    assert output.written == {("columns", "items"): 1, ("schema", "items"): 1, ("columns", "doubled"): 1,
                              ("schema", "doubled"): 1}
    assert records(output, "doubled") == [{"doubled": 2}, {"doubled": 4}, {"doubled": 6}]

    folder = tmp_path / "out"
    output = open_output("jsonl:%s" % folder, source)
    run(source, output=output)
    output.close()

    def lines(name):
        (path,) = [path for path in folder.iterdir() if path.name.startswith(name + ".")]
        return [json.loads(line) for line in path.read_text().splitlines()]
    assert lines("items") == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert lines("doubled") == [{"doubled": 2}, {"doubled": 4}, {"doubled": 6}]
    assert [path.name for path in folder.iterdir() if path.name.startswith(".")] == []  # no files left behind

    database = tmp_path / "warehouse.duckdb"
    output = open_output("duckdb:%s:client" % database, source)
    run(source, output=output)
    output.close()
    import duckdb
    connection = duckdb.connect(str(database), read_only=True)
    try:
        assert connection.execute("SELECT id FROM client.items ORDER BY id").fetchall() == [(1,), (2,), (3,)]
        assert connection.execute("SELECT doubled FROM client.doubled ORDER BY 1").fetchall() == [(2,), (4,), (6,)]
    finally:
        connection.close()


# * ----------------------------------
# * `$name` config inputs in the steps
# * ----------------------------------

CONFIG_SPEC = {"config": {
    "client": {"type": "string"}, "limit": {"type": "integer"}, "ratio": {"type": "number"},
    "on": {"type": "boolean"}, "since": {"type": "date"}, "ids": {"type": "list", "items": "integer"},
    "names": {"type": "list"}, "missing": {"type": "string", "required": False}}}
PARAMETERS = ("SELECT $client AS client, $limit AS limit_value, $ratio AS ratio, $on AS on_value, $since AS since, "
              "$ids AS ids, $names AS names, $missing AS missing, (record->>'id')::BIGINT AS id FROM records "
              "WHERE (record->>'id')::BIGINT <= $limit")


@pytest.mark.parametrize("mode", ["page", "run"])
def test_config_parameters_are_bound_values_with_their_config_types(api, mode):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 9}]})
    stream = page_stream("items", {"http": {"path": "/items"}}, PARAMETERS, records={"path": "data"})
    stream["transform"]["mode"] = mode
    config = {"client": "O'Brien'); DROP TABLE records; --", "limit": 5, "ratio": 2, "on": True,
              "since": datetime.date(2026, 10, 1), "ids": [1, 2], "names": [], "missing": None}
    output = run(source_with(api, stream, spec=CONFIG_SPEC), config=config)
    # values are data, never SQL; a value that is not given is null; every column has its config type
    assert records(output, "items") == [{
        "client": "O'Brien'); DROP TABLE records; --", "limit_value": 5, "ratio": 2.0, "on_value": True,
        "since": "2026-10-01", "ids": [1, 2], "names": [], "missing": None, "id": 1}]
    assert output.columns["items"] == [
        ("client", "VARCHAR"), ("limit_value", "BIGINT"), ("ratio", "DOUBLE"), ("on_value", "BOOLEAN"),
        ("since", "DATE"), ("ids", "BIGINT[]"), ("names", "VARCHAR[]"), ("missing", "VARCHAR"), ("id", "BIGINT")]


@pytest.mark.parametrize("mode", ["page", "run"])
def test_steps_keep_their_column_types_when_config_values_are_null(api, mode):
    """DuckDB folds `'a' || NULL` into an INTEGER NULL: a step's columns keep their types whatever the values."""
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    stream = page_stream("items", {"http": {"path": "/items"}}, "SELECT (record->>'id')::BIGINT AS id, 'p-' || "
                                                                "$client AS label, $ids[1] AS first_id FROM records",
                         records={"path": "data"})
    stream["transform"]["mode"] = mode
    stream["transform"]["steps"] += [
        # a lambda takes no subquery: this step's parameters are plain casts, and its table gets the planned types
        {"name": "tagged", "select": "SELECT id, list_transform([id], x -> x + $limit) AS shifted, 'q-' || $client "
                                     "AS tag FROM items"},
        {"name": "tags", "select": "SELECT id, coalesce(label, 'none') AS label, coalesce(tag, 'none') AS tag "
                                   "FROM items JOIN tagged USING (id)"}]
    stream["export"].update(tagged={"step": "tagged"}, tags={"step": "tags"})
    source = source_with(api, stream, spec=CONFIG_SPEC)
    columns = {"items": [("id", "BIGINT"), ("label", "VARCHAR"), ("first_id", "BIGINT")],
               "tagged": [("id", "BIGINT"), ("shifted", "BIGINT[]"), ("tag", "VARCHAR")],
               "tags": [("id", "BIGINT"), ("label", "VARCHAR"), ("tag", "VARCHAR")]}
    output = run(source, config={"client": "acme", "limit": 5, "ids": [7]})
    assert output.columns == columns
    assert records(output, "tags") == [{"id": 1, "label": "p-acme", "tag": "q-acme"},
                                       {"id": 2, "label": "p-acme", "tag": "q-acme"}]
    assert records(output, "tagged")[0] == {"id": 1, "shifted": [6], "tag": "q-acme"}
    output = run(source, config={})
    assert output.columns == columns
    assert records(output, "items")[0] == {"id": 1, "label": None, "first_id": None}
    assert records(output, "tags") == [{"id": 1, "label": "none", "tag": "none"},
                                       {"id": 2, "label": "none", "tag": "none"}]


def test_config_parameters_have_their_types_in_the_checks(api):
    def problems(query):
        return sql.check_source(source_with(api, page_stream("items", {"http": {"path": "/x"}}, query),
                                            spec=CONFIG_SPEC))
    assert problems("SELECT $since + 1 AS next_day, $ids[1] + $limit AS n, $names AS names FROM records") == []
    # where DuckDB takes no subquery (lambdas, `= ANY(list)`, arguments that must be constants)
    assert problems("SELECT list_transform($ids, x -> x + $limit) AS shifted, strftime($since, $client) AS day "
                    "FROM records WHERE $limit = ANY($ids)") == []
    (found,) = problems("SELECT $client + 1 AS x FROM records")
    assert found[0] == ("streams", 0) + SELECT_PATH and "+(VARCHAR, INTEGER_LITERAL)" in found[1]
    assert problems("SELECT ? AS x FROM records") == [(("streams", 0) + SELECT_PATH, (
        "parameters are config inputs named in the query, `$name`: not ? or $1"))]
    assert problems("SELECT $limit AS a, $LIMIT AS b FROM records") == [
        (("streams", 0) + SELECT_PATH, "`$LIMIT` is not a config input (declare it in spec.config)"),
        (("streams", 0) + SELECT_PATH, "`$LIMIT` and `$limit` are one parameter to DuckDB (names ignore case): use "
                                       "one of them")]


def test_parameters_that_are_not_config_inputs_stop_the_run_and_secrets_are_never_parameters(api):
    stream = page_stream("items", {"http": {"path": "/items"}}, "SELECT $client AS client, $token AS token, "
                                                                "$nope AS nope FROM records")
    source = source_with(api, stream, spec={"config": {"client": {"type": "string"}},
                                            "secrets": {"token": {"type": "string"}}})
    assert sql.check_source(source) == [
        (("streams", 0) + SELECT_PATH, "`$nope` is not a config input (declare it in spec.config)"),
        (("streams", 0) + SELECT_PATH, "`$token` is not a config input (declare it in spec.config)")]
    with pytest.raises(SourceError, match=r"stream 'items': transform.steps\[0\].select: `\$nope` is not a config "
                                          r"input .*`\$token` is not a config input"):
        run(source, config={"client": "x", "token": "secret-token-1"})
    assert api.requests == []


STATUSES = [{"id": 1, "status": "ACTIVE"}, {"id": 2, "status": "PAUSED"}, {"id": 3, "status": "REMOVED"}]
LISTED = {"config": {"statuses": {"type": "list", "items": "string"}, "limit": {"type": "integer"}}}


def status_stream(api, condition, mode="page"):
    """A stream of STATUSES whose step keeps the ids of the records that meet `condition`."""
    api.routes[("GET", "/items")] = lambda request: (200, {"data": STATUSES})
    stream = page_stream("items", {"http": {"path": "/items"}},
                         "SELECT (record->>'id')::BIGINT AS id FROM records WHERE " + condition,
                         records={"path": "data"})
    stream["transform"]["mode"] = mode
    return source_with(api, stream, spec=LISTED)


@pytest.mark.parametrize("mode", ["page", "run"])
@pytest.mark.parametrize("condition,ids", [
    ("record->>'status' = ANY($statuses)", [1, 3]),
    ("list_contains($statuses, record->>'status')", [1, 3]),
    ("record->>'status' IN $statuses", [1, 3]),  # DuckDB's list membership: no parentheses
    ("record->>'status' <> ALL($statuses)", [2]),
])
def test_list_parameters_test_membership_with_any_list_contains_or_in(api, mode, condition, ids):
    source = status_stream(api, condition, mode)
    assert sql.check_source(source) == []
    output = run(source, config={"statuses": ["ACTIVE", "REMOVED"], "limit": 1})
    assert records(output, "items") == [{"id": value} for value in ids]


def test_list_parameters_compared_as_one_value_are_reported_before_any_request(api):
    problem = (("streams", 0) + SELECT_PATH, "`$statuses` is a list (VARCHAR[]), which IN and comparisons take as one "
                                             "value: use `= ANY($statuses)` or `list_contains($statuses, x)` (to "
                                             "compare whole lists, cast it: `$statuses::VARCHAR[]`)")
    # DuckDB casts each value to a list, which fails on the first record: the checks report it instead
    for condition in ("record->>'status' IN ($statuses)", "record->>'status' NOT IN ('x', $statuses)",
                      "record->>'status' = $statuses", "$statuses <> record->>'status'"):
        source = status_stream(api, condition)
        assert sql.check_source(source) == [problem], condition
        with pytest.raises(SourceError, match=r"stream 'items': transform.steps\[0\].select: `\$statuses` is a list"):
            run(source, config={"statuses": ["ACTIVE"], "limit": 1})
        assert api.requests == []
    # lists compared with lists
    assert sql.check_source(status_stream(api, "$statuses = [] OR $statuses IN (['ACTIVE'], ['PAUSED']) OR "
                                               "$statuses::VARCHAR[] = (record->'tags')::VARCHAR[]")) == []


def test_parameters_are_plain_casts_only_where_duckdb_takes_no_subquery(api):
    # a reused column alias, TRY, a lambda and a non-inner join take no subquery: the steps compile with plain casts,
    # and run
    stream = page_stream("items", {"http": {"path": "/items"}},
                         "SELECT (record->>'id')::BIGINT AS id, $limit AS lim, lim + 1 AS next, "
                         "TRY(CAST($limit AS TINYINT)) AS small, list_transform([id], x -> x * $limit) AS scaled "
                         "FROM records", records={"path": "data"})
    stream["transform"]["steps"].append({"name": "joined", "select": "SELECT a.id, b.id AS other FROM items a FULL "
                                                                     "JOIN items b ON a.id = b.id AND b.lim = $limit"})
    stream["export"]["joined"] = {"step": "joined"}
    api.routes[("GET", "/items")] = lambda request: (200, {"data": STATUSES[:1]})
    source = source_with(api, stream, spec=LISTED)
    assert sql.check_source(source) == []
    output = run(source, config={"limit": 2})
    assert records(output, "items") == [{"id": 1, "lim": 2, "next": 3, "small": 2, "scaled": [2]}]
    assert records(output, "joined") == [{"id": 1, "other": 1}]
    # other errors are the query's, though plain casts would compile: `x IN ($limit)` compares text with a number
    assert sql.check_source(status_stream(api, "record->>'status' IN ($limit)")) == [(
        ("streams", 0) + SELECT_PATH, "Binder Error: Cannot compare values of type VARCHAR and BIGINT in IN/ANY/ALL "
                                      "clause - an explicit cast is required")]


# * ------------------
# * request partitions
# * ------------------

def test_request_partitions_from_request_dotted_path_fields_distinct_and_missing(api):
    api.routes[("GET", "/parents")] = lambda request: (200, {"data": [
        {"owner": {"id": "o1"}, "account": "a1", "campaign": "c1"},
        {"owner": {"id": "o1"}, "account": "a1", "campaign": "c1"},
        {"owner": {}, "account": "a1", "campaign": None},
        {"owner": {"id": "o2"}, "account": "a2", "campaign": "c2"}]})
    api.routes[("GET", "/owner")] = lambda request: (200, {"data": [{"owner": request["params"]["owner"]}]})
    api.routes[("GET", "/metric")] = lambda request: (200, {"data": [{
        "account": request["params"]["account"], "campaign": request["params"]["campaign"]}]})
    source = source_with(api, run_mode("request_parts", [
        {"name": "parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "owners", "partitions": [{"name": "owner", "from": "parents", "field": "owner.id"}],
         "http": {"path": "/owner", "params": {"owner": "{{ partition.owner }}"}}, "records": {"path": "data"}},
        {"name": "metrics", "partitions": [{"from": "parents", "fields": ["account", "campaign"]}],
         "http": {"path": "/metric", "params": {"account": "{{ partition.account }}",
                                                "campaign": "{{ partition.campaign }}"}},
         "records": {"path": "data"}}], [
        {"name": "owner_rows", "select": "SELECT record->>'owner' AS owner FROM owners"},
        {"name": "metric_rows",
         "select": "SELECT record->>'account' AS account, record->>'campaign' AS campaign FROM metrics"}], {
        "owner_list": {"step": "owner_rows", "primary_key": ["owner"]},
        "metric_list": {"step": "metric_rows", "primary_key": ["account", "campaign"]}}))
    output = run(source)
    assert [request["params"]["owner"] for request in api.calls("/owner")] == ["o1", "o2"]
    assert [(request["params"]["account"], request["params"]["campaign"]) for request in api.calls("/metric")] == [
        ("a1", "c1"), ("a2", "c2")]
    assert records(output, "owner_list") == [{"owner": "o1"}, {"owner": "o2"}]


def ads_source(api, partition):
    """Accounts a1 and a2, each with two campaigns; an ad request partitioned by `partition`."""
    return source_with(api, run_mode("ads", [
        {"name": "campaign_list", "http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}},
        {"name": "ads_raw", "partitions": [partition], "records": {"path": "data"}, "http": {
            "path": "/ads", "params": {"account": "{{ partition.account }}",
                                       "campaign": "{{ partition.campaign }}"}}}], [
        {"name": "campaign_rows",
         "select": "SELECT partition->>'account' AS account, record->>'campaign' AS campaign FROM campaign_list"},
        {"name": "ad_rows",
         "select": "SELECT record->>'account' AS account, record->>'campaign' AS campaign FROM ads_raw"}],
        {"ads": {"step": "ad_rows", "primary_key": ["account", "campaign"]}},
        partitions=[{"name": "account", "values": ["a1", "a2"]}]))


def test_request_partitions_from_a_step_match_stream_partitions_only_by_name(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [
        {"campaign": request["params"]["account"] + "-c1"}, {"campaign": request["params"]["account"] + "-c2"}]})
    api.routes[("GET", "/ads")] = lambda request: (200, {"data": [{
        "account": request["params"]["account"], "campaign": request["params"]["campaign"]}]})

    def calls():
        found = [(request["params"]["account"], request["params"]["campaign"]) for request in api.calls("/ads")]
        api.requests.clear()
        return found
    # `fields` include `account`, the stream partition's name: each account uses only the rows of its own value
    run(ads_source(api, {"from": "campaign_rows", "fields": ["account", "campaign"]}))
    assert calls() == [("a1", "a1-c1"), ("a1", "a1-c2"), ("a2", "a2-c1"), ("a2", "a2-c2")]
    # a column named like a stream partition does not filter on its own: all the step's rows are crossed
    run(ads_source(api, {"name": "campaign", "from": "campaign_rows", "field": "campaign"}))
    assert calls() == [(account, "%s-%s" % (owner, campaign)) for account in ("a1", "a2")
                       for owner in ("a1", "a2") for campaign in ("c1", "c2")]


def test_request_partitions_from_json_columns_are_decoded_values(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [
        {"campaign": request["params"]["account"] + "-c1"}]})
    api.routes[("GET", "/ads")] = lambda request: (200, {"data": [{
        "account": request["params"]["account"], "campaign": request["params"]["campaign"]}]})
    source = ads_source(api, {"from": "campaign_rows", "fields": ["account", "campaign"]})
    # JSON columns (record->'campaign', partition->'account'): their values, not quoted JSON texts
    steps_of(source)[0]["select"] = ("SELECT partition->'account' AS account, record->'campaign' AS campaign "
                                     "FROM campaign_list")
    output = run(source)
    assert [(request["params"]["account"], request["params"]["campaign"]) for request in api.calls("/ads")] == [
        ("a1", "a1-c1"), ("a2", "a2-c1")]
    assert records(output, "ads") == [{"account": "a1", "campaign": "a1-c1"}, {"account": "a2", "campaign": "a2-c1"}]


def test_request_partitions_from_a_request_use_its_records_of_the_same_stream_partition(api):
    campaigns = {"A": [{"Id": "1"}, {"Id": "2"}, {"Id": "1"}], "B": [{"Id": "3"}]}
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"Campaign": campaigns[request["params"]["account"]]})
    api.routes[("GET", "/ad_groups")] = lambda request: (200, {"AdGroup": [{"Id": request["params"]["campaign"]}]})
    source = source_with(api, run_mode("tree", [
        {"name": "raw_campaigns", "records": {"path": "Campaign"},
         "http": {"path": "/campaigns", "params": {"account": "{{ partition.account_id }}"}}},
        {"name": "raw_ad_groups", "records": {"path": "AdGroup"},
         "partitions": [{"name": "campaign_id", "from": "raw_campaigns", "field": "Id"}],
         "http": {"path": "/ad_groups", "params": {"campaign": "{{ partition.campaign_id }}"},
                  "headers": {"CustomerAccountId": "{{ partition.account_id }}"}}}], [
        {"name": "ad_group_rows", "select": "SELECT partition->>'account_id' AS account_id, partition->>'campaign_id' "
                                            "AS campaign_id, record->>'Id' AS ad_group_id FROM raw_ad_groups"}],
        {"ad_group_tree": {"step": "ad_group_rows", "primary_key": ["account_id", "ad_group_id"]}},
        partitions=[{"name": "account_id", "values": ["A", "B"]}]))
    output = run(source)
    calls = collections.Counter((request["headers"]["CustomerAccountId"], request["params"]["campaign"])
                                for request in api.calls("/ad_groups"))
    assert calls == {("A", "1"): 1, ("A", "2"): 1, ("B", "3"): 1}
    assert records(output, "ad_group_tree") == [
        {"account_id": "A", "campaign_id": "1", "ad_group_id": "1"},
        {"account_id": "A", "campaign_id": "2", "ad_group_id": "2"},
        {"account_id": "B", "campaign_id": "3", "ad_group_id": "3"}]


def test_request_partitions_from_a_request_compare_stream_partition_values_as_json(api):
    def campaigns(request):
        account = json.loads(request["body"])["account"]
        ids = ["number"] if isinstance(account, int) else ["text-1", "text-2"]
        return 200, {"data": [{"campaign": {"id": campaign_id}} for campaign_id in ids]}
    api.routes[("POST", "/campaigns")] = campaigns
    api.routes[("POST", "/ads")] = lambda request: (200, {"data": [json.loads(request["body"])]})
    source = source_with(api, run_mode("ads", [
        {"name": "campaign_list", "records": {"path": "data"},
         "http": {"method": "POST", "path": "/campaigns", "json": {"account": "{{ partition.account }}"}}},
        {"name": "ad_list", "records": {"path": "data"},
         "partitions": [{"name": "campaign", "from": "campaign_list", "field": "campaign.id"}],
         "http": {"method": "POST", "path": "/ads", "json": {"account": "{{ partition.account }}",
                                                             "campaign": "{{ partition.campaign }}"}}}],
        [{"name": "ad_rows", "select": "SELECT record->>'campaign' AS campaign FROM ad_list"}],
        {"ad_rows_out": {"step": "ad_rows"}}, partitions=[{"name": "account", "values": [7, "7"]}]))
    run(source)
    # the stream partitions 7 and "7" are told apart, as JSON values are
    assert [json.loads(request["body"]) for request in api.calls("/ads")] == [
        {"account": 7, "campaign": "number"}, {"account": "7", "campaign": "text-1"},
        {"account": "7", "campaign": "text-2"}]


def test_request_partition_sources_without_rows_and_values_matched_as_text(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"id": "c1"}]})
    api.routes[("GET", "/ad_groups")] = lambda request: (200, {"data": [{"id": request["params"]["campaign"]}]})
    source = source_with(api, run_mode("tree", [
        {"name": "campaign_types", "partitions": [{"name": "kind", "values": []}],
         "http": {"path": "/campaigns"}, "records": {"path": "data"}},
        {"name": "typed_ad_groups", "partitions": [{"name": "campaign", "from": "campaign_types", "field": "id"}],
         "http": {"path": "/ad_groups", "params": {"campaign": "{{ partition.campaign }}"}},
         "records": {"path": "data"}},
        {"name": "campaign_list", "http": {"path": "/campaigns"}, "records": {"path": "data"}},
        {"name": "ad_list", "partitions": [{"from": "campaign_rows", "fields": ["account_id", "campaign"]}],
         "http": {"path": "/ad_groups", "params": {"campaign": "{{ partition.campaign }}"}},
         "records": {"path": "data"}}], [
        {"name": "campaign_rows", "select": "SELECT 5::BIGINT AS account_id, record->>'id' AS campaign "
                                            "FROM campaign_list"},
        {"name": "ad_group_rows", "select": "SELECT partition->>'account_id' AS account_id, record->>'id' AS id "
                                            "FROM ad_list UNION ALL SELECT 'typed', record->>'id' "
                                            "FROM typed_ad_groups"}],
        {"tree_out": {"step": "ad_group_rows"}}, partitions=[{"name": "account_id", "values": ["acct-1", "5"]}]))
    output = run(source)
    # a request that made no call gives no partitions; the BIGINT 5 matches the stream partition "5" (as text), and
    # the stream partition keeps its own value
    assert records(output, "tree_out") == [{"account_id": "5", "id": "c1"}]
    assert [request["params"]["campaign"] for request in api.calls("/ad_groups")] == ["c1"]


class FakeApiStub(object):
    url = "http://127.0.0.1:9"


def test_request_partitions_read_each_source_once_for_many_stream_partitions():
    """1000 accounts with 100 campaigns each: each `from:` source is read once, not once per account."""
    stream = run_mode("tree", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns"}},
        {"name": "raw_ad_groups", "partitions": [{"name": "campaign_id", "from": "raw_campaigns", "field": "Id"}],
         "http": {"path": "/ad_groups"}},
        {"name": "raw_ads", "partitions": [{"from": "campaign_rows", "fields": ["account_id", "campaign_id"]}],
         "http": {"path": "/ads"}}],
        [{"name": "campaign_rows", "select": "SELECT partition->>'account_id' AS account_id, record->>'Id' AS "
                                             "campaign_id FROM raw_campaigns"}],
        {"tree": {"step": "campaign_rows"}}, partitions=[{"name": "account_id", "values": [
            "a%d" % account for account in range(1000)]}])
    runner = SourceRunner(source_with(FakeApiStub(), stream), {}, {"token": "secret-token-1"}, output=MemoryOutput(),
                          today=TODAY)
    sandbox = runner.workspace()
    try:
        sandbox.create_raw("raw_campaigns")
        sandbox.connection.execute(
            "INSERT INTO raw_campaigns SELECT json_object('Id', 'c' || campaign), json_object('account_id', "
            "'a' || account), '{}', NULL, NULL, NULL FROM range(1000) a(account), range(100) c(campaign)")
        sandbox.connection.execute("CREATE TABLE campaign_rows AS SELECT 'a' || account AS account_id, "
                                   "'c' || campaign AS campaign_id FROM range(1000) a(account), range(100) c(campaign)")
        partitions = runner.partitions(stream)
        started = time.time()
        for request in stream["requests"][1:]:
            resolve = runner.request_partitions(stream, request)
            found = [resolve(partition) for partition in partitions]
            assert sum(len(partitions_) for partitions_ in found) == 100000
            assert sorted(item["campaign_id"] for item in found[7]) == sorted("c%d" % n for n in range(100))
            assert set(item["account_id"] for item in found[7]) == {"a7"}
        assert time.time() - started < 5  # it took minutes when each account read every row
        # with `batch_size`: still one read of the source, each account's own 100 campaigns in lists of up to 30
        request = {"name": "raw_batched", "http": {"path": "/batched"}, "partitions": [
            {"name": "campaign_ids", "from": "raw_campaigns", "field": "Id", "batch_size": 30}]}
        resolve = runner.request_partitions(stream, request)
        found = [resolve(partition) for partition in partitions]
        assert sum(len(partitions_) for partitions_ in found) == 4000
        assert [len(item["campaign_ids"]) for item in found[7]] == [30, 30, 30, 10]
        assert sorted(campaign for item in found[7] for campaign in item["campaign_ids"]) == sorted(
            "c%d" % n for n in range(100))
        # from a step, scoped by the stream partition field it lists: the same lists of each account's own campaigns
        request = {"name": "raw_batched_rows", "http": {"path": "/batched"}, "partitions": [
            {"from": "campaign_rows", "fields": ["account_id", "campaign_id"], "batch_size": 30}]}
        resolve = runner.request_partitions(stream, request)
        found = [resolve(partition) for partition in partitions]
        assert sum(len(partitions_) for partitions_ in found) == 4000
        assert [len(item["campaign_id"]) for item in found[7]] == [30, 30, 30, 10]
        assert set(item["account_id"] for item in found[7]) == {"a7"}
        assert sorted(campaign for item in found[7] for campaign in item["campaign_id"]) == sorted(
            "c%d" % n for n in range(100))
        assert time.time() - started < 15
    finally:
        sandbox.close()
        runner.sandbox = None


# * ----------------------------------
# * request partitions with batch_size
# * ----------------------------------

def body(request):
    return json.loads(request["body"])


def test_batch_size_from_a_step_reads_lists_of_values_and_every_row_lands(api):
    api.routes[("GET", "/ids")] = lambda request: (200, {"data": [{"id": "i%04d" % n} for n in range(1000)]})
    api.routes[("POST", "/details")] = lambda request: (200, {"data": [
        {"id": item, "detail": "d-" + item} for item in body(request)["ids"]]})
    source = source_with(api, run_mode("details", [
        {"name": "raw_ids", "http": {"path": "/ids"}, "records": {"path": "data"}},
        {"name": "raw_details", "partitions": [{"name": "ids", "from": "id_rows", "field": "id", "batch_size": 200}],
         "http": {"method": "POST", "path": "/details", "json": {"ids": "{{ partition.ids }}"}},
         "records": {"path": "data"}}], [
        {"name": "id_rows", "select": "SELECT record->>'id' AS id FROM raw_ids"},
        {"name": "detail_rows", "select": "SELECT record->>'id' AS id, record->>'detail' AS detail, "
                                          "json_array_length(partition->'ids')::BIGINT AS batch FROM raw_details"},
        {"name": "joined", "select": "SELECT i.id, d.detail, d.batch FROM id_rows i "
                                     "LEFT JOIN detail_rows d USING (id)"}],
        {"joined": {"step": "joined", "primary_key": ["id"]}}))
    output = run(source)
    batches = [body(request)["ids"] for request in api.calls("/details")]
    assert len(batches) == 5  # 1000 ids in lists of 200: 5 requests, not 1000
    assert [item for batch in batches for item in batch] == ["i%04d" % n for n in range(1000)]  # (a step: sorted)
    assert records(output, "joined") == [{"id": "i%04d" % n, "detail": "d-i%04d" % n, "batch": 200}
                                         for n in range(1000)]


def test_batch_size_on_values_gives_distinct_values_in_order_without_missing_ones(api):
    api.routes[("POST", "/items")] = lambda request: (200, {"data": [{"ids": body(request)["ids"]}]})

    def items(size, ids):
        source = source_with(api, run_mode("items", [
            {"name": "raw_items", "partitions": [{"name": "ids", "values": "{{ config.ids }}", "batch_size": size}],
             "http": {"method": "POST", "path": "/items", "json": {"ids": "{{ partition.ids }}"}},
             "records": {"path": "data"}}],
            [{"name": "item_rows", "select": "SELECT partition->>'ids' AS ids FROM raw_items"}],
            {"item_rows": {"step": "item_rows"}}))
        output = run(source, config={"ids": ids})
        found = [body(request)["ids"] for request in api.calls("/items")]
        api.requests.clear()
        assert [json.loads(record["ids"]) for record in records(output, "item_rows")] == found
        return found
    ids = ["b", "a", "b", None, "c"]
    assert items(2, ids) == [["b", "a"], ["c"]]  # the last list is smaller
    assert items(10, ids) == [["b", "a", "c"]]   # more than the values: one request
    assert items(1, ids) == [["b"], ["a"], ["c"]]  # one value per request, still a list
    assert items(2, []) == [] and items(2, [None]) == []  # no values: no request (not an empty list)


def test_batch_size_lists_each_stream_partitions_own_values_crossed_with_other_items(api):
    campaigns = {"a1": [{"id": "c1"}, {"id": "c2"}, {"id": "c1"}, {}, {"id": "c3"}], "a2": [{"id": "c4"}], "a3": []}
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": campaigns[request["params"]["account"]]})
    api.routes[("POST", "/ads")] = lambda request: (200, {"data": [
        dict(campaign=campaign, kind=body(request)["kind"]) for campaign in body(request)["campaigns"]]})
    source = source_with(api, run_mode("ads", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}},
        {"name": "raw_ads", "records": {"path": "data"}, "partitions": [
            {"name": "kind", "values": ["x", "y"]},
            {"name": "campaigns", "from": "raw_campaigns", "field": "id", "batch_size": 2}],
         "http": {"method": "POST", "path": "/ads", "json": {
             "account": "{{ partition.account }}", "kind": "{{ partition.kind }}",
             "campaigns": "{{ partition.campaigns }}"}}}],
        [{"name": "ad_rows", "select": "SELECT partition->>'account' AS account, record->>'kind' AS kind, "
                                       "record->>'campaign' AS campaign FROM raw_ads"}],
        {"ad_rows": {"step": "ad_rows", "primary_key": ["account", "kind", "campaign"]}},
        partitions=[{"name": "account", "values": ["a1", "a2", "a3"]}]))
    output = run(source)
    # each account lists only its own campaigns (distinct, in the order read, without the missing id); a3 has none
    assert [(request_["account"], request_["kind"], request_["campaigns"])
            for request_ in map(body, api.calls("/ads"))] == [
        ("a1", "x", ["c1", "c2"]), ("a1", "x", ["c3"]), ("a1", "y", ["c1", "c2"]), ("a1", "y", ["c3"]),
        ("a2", "x", ["c4"]), ("a2", "y", ["c4"])]
    assert len(records(output, "ad_rows")) == 8


def test_batch_size_lists_are_read_for_each_window_and_bookmarks_stay_per_stream_partition(api):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"id": "c%d" % n} for n in range(5)]})
    api.routes[("POST", "/stats")] = lambda request: (200, {"data": [
        {"campaign": campaign, "day": body(request)["day"]} for campaign in body(request)["campaigns"]]})
    source = source_with(api, run_mode("stats", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}},
        {"name": "raw_stats", "records": {"path": "data"},
         "partitions": [{"name": "campaigns", "from": "raw_campaigns", "field": "id", "batch_size": 2}],
         "http": {"method": "POST", "path": "/stats", "json": {
             "campaigns": "{{ partition.campaigns }}", "day": "{{ window.start }}"}}}],
        [{"name": "stats", "select": "SELECT partition->>'account' AS account, record->>'campaign' AS campaign, "
                                     "(record->>'day')::DATE AS day FROM raw_stats"}],
        {"stats": {"step": "stats", "primary_key": ["account", "campaign", "day"]}},
        partitions=[{"name": "account", "values": ["a1", "a2"]}],
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"}))
    output = run(source)
    calls = collections.Counter((tuple(request_["campaigns"]), request_["day"])
                                for request_ in map(body, api.calls("/stats")))
    # per account: 3 lists x 3 windows; the campaign request (no window) once per account
    assert calls == dict(((campaigns, day), 2) for campaigns in (("c0", "c1"), ("c2", "c3"), ("c4",))
                         for day in DAYS)
    assert len(api.calls("/campaigns")) == 2
    assert len(records(output, "stats")) == 2 * 5 * 3
    assert output.states[-1] == {"bookmarks": {"stats": {'{"account": "a1"}': "2026-10-02",
                                                         '{"account": "a2"}': "2026-10-02"}}}


def test_batch_size_on_from_fields_batches_each_stream_partitions_own_step_values(api):
    # the step has both accounts' rows; `account` keeps each account's, `campaign` is batched and holds the list
    campaigns = {"a1": ["c1", "c2", "c3", "c2", None], "a2": ["c4", "c5"]}
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [
        {"id": campaign} for campaign in campaigns[request["params"]["account"]]]})
    api.routes[("POST", "/ads")] = lambda request: (200, {"data": [
        {"campaign": campaign} for campaign in body(request)["campaigns"]]})
    source = source_with(api, run_mode("ads", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}},
         "records": {"path": "data"}},
        {"name": "raw_ads", "records": {"path": "data"},
         "partitions": [{"from": "campaign_rows", "fields": ["account", "campaign"], "batch_size": 2}],
         "http": {"method": "POST", "path": "/ads", "json": {"account": "{{ partition.account }}",
                                                            "campaigns": "{{ partition.campaign }}"}}}],
        [{"name": "campaign_rows", "select": "SELECT partition->>'account' AS account, record->>'id' AS campaign "
                                             "FROM raw_campaigns ORDER BY account, campaign"},
         {"name": "ad_rows", "select": "SELECT partition->>'account' AS account, record->>'campaign' AS campaign, "
                                       "json_array_length(partition->'campaign')::BIGINT AS batch FROM raw_ads"}],
        {"ad_rows": {"step": "ad_rows", "primary_key": ["account", "campaign"]}},
        partitions=[{"name": "account", "values": ["a1", "a2"]}]))
    output = run(source)
    # each account lists only its own campaigns (distinct, without the missing one), in lists of up to 2
    assert [(request_["account"], request_["campaigns"]) for request_ in map(body, api.calls("/ads"))] == [
        ("a1", ["c1", "c2"]), ("a1", ["c3"]), ("a2", ["c4", "c5"])]
    assert records(output, "ad_rows") == [
        {"account": "a1", "campaign": "c1", "batch": 2}, {"account": "a1", "campaign": "c2", "batch": 2},
        {"account": "a1", "campaign": "c3", "batch": 1}, {"account": "a2", "campaign": "c4", "batch": 2},
        {"account": "a2", "campaign": "c5", "batch": 2}]


@pytest.mark.parametrize("item,message", [
    ({"from": "campaign_rows", "fields": ["account"], "batch_size": 2},
     "`batch_size` on `{from, fields}` needs exactly one field that is not a stream partition (the one batched), got "
     "account"),
    ({"from": "campaign_rows", "fields": ["account", "campaign", "kind"], "batch_size": 2},
     "`batch_size` on `{from, fields}` needs exactly one field that is not a stream partition (the one batched), got "
     "account, campaign, kind"),
    # a step has every stream partition's rows: without the stream partition fields, each list would mix them
    ({"name": "campaigns", "from": "campaign_rows", "field": "campaign", "batch_size": 2},
     "`batch_size` on step 'campaign_rows' would list other stream partitions' values: use `{from: campaign_rows, "
     "fields: [account, campaign], batch_size: 2}`"),
    ({"from": "campaign_rows", "fields": ["campaign"], "batch_size": 2},
     "`batch_size` on step 'campaign_rows' would list other stream partitions' values: use `{from: campaign_rows, "
     "fields: [account, campaign], batch_size: 2}`"),
])
def test_batch_size_from_a_step_needs_the_stream_partition_fields_and_one_batched_field(api, item, message):
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"id": "c1"}]})
    source = source_with(api, run_mode("ads", [
        {"name": "raw_campaigns", "http": {"path": "/campaigns"}, "records": {"path": "data"}},
        {"name": "raw_ads", "partitions": [item], "http": {"path": "/ads"}, "records": {"path": "data"}}],
        [{"name": "campaign_rows", "select": "SELECT partition->>'account' AS account, record->>'id' AS campaign, "
                                             "'k' AS kind FROM raw_campaigns"}],
        {"campaign_rows": {"step": "campaign_rows"}}, partitions=[{"name": "account", "values": ["a1"]}]))
    with pytest.raises(SourceError, match=re.escape("stream 'ads': request 'raw_ads': " + message)):
        run(source)
    assert api.calls("/ads") == []


def test_batch_size_must_be_a_whole_number_of_at_least_one(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": "x"}]})
    for size in (0, "2", True):
        source = source_with(api, run_mode("items", [
            {"name": "raw_items", "partitions": [{"name": "ids", "values": ["a"], "batch_size": size}],
             "http": {"path": "/items"}, "records": {"path": "data"}}],
            [{"name": "item_rows", "select": "SELECT record->>'id' AS id FROM raw_items"}],
            {"item_rows": {"step": "item_rows"}}))
        with pytest.raises(SourceError, match=r"stream 'items': request 'raw_items': `batch_size` must be a whole "
                                              r"number of at least 1, got %s" % repr(size)):
            run(source)
    assert api.calls("/items") == []


def test_distinct_rows_reports_duckdb_errors_as_sql_errors():
    sandbox = sql.Sandbox()
    try:
        sandbox.create_empty("rows", [("id", "VARCHAR")])
        with pytest.raises(sql.SqlError, match="missing"):
            sandbox.distinct_rows("rows", ("missing",))
        with pytest.raises(sql.SqlError, match="nowhere"):
            sandbox.distinct_rows("nowhere", ("id",))
        with pytest.raises(sql.SqlError, match="nowhere"):
            list(sandbox.raw_rows("nowhere"))
    finally:
        sandbox.close()


# * ---------------------------------
# * `from_stream` partitions (kept)
# * ---------------------------------

def accounts_stream(**stream):
    """Accounts per region (`bad` fails), skipping partitions that fail."""
    return page_stream("accounts", {"http": {"path": "/accounts", "params": {"region": "{{ partition.region }}"}}},
                       "SELECT (record->>'id')::INT AS id, record->>'id' AS account_id FROM records",
                       records={"path": "data"}, on_partition_error="skip",
                       partitions=[{"name": "region", "values": ["good", "bad"]}], **stream)


def test_from_stream_children_take_the_values_of_their_parents_export_and_are_partial_with_it(api):
    api.routes[("GET", "/accounts")] = lambda request: (
        (500, {}) if request["params"]["region"] == "bad" else (200, {"data": [{"id": 1}, {"id": 2}, {"id": 1}]}))
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{
        "id": "c" + request["params"]["account"], "day": request["params"]["day"]}]})
    api.routes[("GET", "/stats")] = lambda request: (200, {"data": [{"clicks": "2"}]})
    source = source_with(api, accounts_stream(), page_stream(
        "campaigns", {"http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}",
                                                                "day": "{{ window.start }}"}}},
        "SELECT record->>'id' AS id, (record->>'day')::DATE AS day FROM records", records={"path": "data"},
        partitions=[{"name": "account", "from_stream": "Accounts", "field": "id"}],
        incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"}), run_mode(
        "report", [{"name": "raw_stats", "records": {"path": "data"}, "http": {"path": "/stats", "params": {
            "account": "{{ partition.account_id }}", "day": "{{ window.start }}"}}}],
        [{"name": "stat_rows", "select": "SELECT partition->>'account_id' AS account_id, window_start AS day, "
                                         "(record->>'clicks')::BIGINT AS clicks FROM raw_stats"}],
        {"report": {"step": "stat_rows", "primary_key": ["account_id", "day"]}},
        partitions=[{"name": "account_id", "from_stream": "accounts", "field": "account_id"}],
        incremental={"cursor_field": "day", "start": "2026-10-02", "window": "1d"}))
    output = MemoryOutput()
    runner = SourceRunner(source, {}, {"token": "secret-token-1"}, output=output, today=TODAY,
                          sleep=lambda seconds: None)
    runner.run(["campaigns", "report"])  # their parent runs first, and writes its export
    assert list(output.schemas) == ["accounts", "campaigns", "report"]
    # distinct values of the fields the children need, not the parent's rows
    assert dict((parent, dict((fields, rows.values) for fields, rows in found.items()))
                for parent, found in runner.collected.items()) == {
        "accounts": {("id",): [(1,), (2,)], ("account_id",): [("1",), ("2",)]}}
    # the children run the partitions they have, and are partial like their parent
    assert [request["params"]["account"] for request in api.calls("/campaigns")] == ["1", "1", "1", "2", "2", "2"]
    assert records(output, "report") == [{"account_id": account, "day": day, "clicks": 2}
                                         for account in ("1", "2") for day in DAYS[1:]]
    assert output.partial == ["accounts", "campaigns", "report"]
    # page mode writes the state after each window, run mode once
    assert output.states[-1] == {"bookmarks": {
        "campaigns": {partition_key({"account": 1}): "2026-10-02", partition_key({"account": 2}): "2026-10-02"},
        "report": {partition_key({"account_id": "1"}): "2026-10-02", partition_key({"account_id": "2"}): "2026-10-02"}}}
    assert len(output.states) == 6 + 1


def test_from_stream_parents_have_exactly_one_export_and_the_fields_are_its_columns(api):
    parent = accounts_stream()
    parent["transform"]["steps"].append({"name": "ids", "select": "SELECT id FROM accounts"})
    parent["export"]["account_ids"] = {"step": "ids"}
    child = page_stream("campaigns", {"http": {"path": "/campaigns"}}, "SELECT 1 AS x FROM records",
                        partitions=[{"name": "account", "from_stream": "accounts", "field": "id"}])
    source = source_with(api, parent, child)
    assert sql.check_source(source) == [(("streams", 1, "partitions", 0, "from_stream"), (
        "`from_stream` takes values from the export of stream 'accounts', which has 2 exports: it needs exactly one"))]
    with pytest.raises(SourceError, match="stream 'campaigns': partitions\\[0\\].from_stream: `from_stream` takes "
                                          "values from the export of stream 'accounts', which has 2 exports"):
        run(source, streams=["campaigns"])
    child["partitions"] = [{"from_stream": "accounts", "fields": ["account_id", "missing"]}]
    assert sql.check_source(source_with(api, accounts_stream(), child)) == [(
        ("streams", 1, "partitions", 0, "fields", 1),
        "'missing' is not a column of export 'accounts' of stream 'accounts' (columns: id, account_id)")]
    assert api.requests == []


# * ------
# * checks
# * ------

def test_check_source_reports_paths_into_the_source(api):
    def problems(change):
        source = demo(api)
        change(source["streams"][0])
        return sql.check_source(source)

    def select(position, text):
        return lambda stream: stream["transform"]["steps"][position].update(select=text)
    (found,) = problems(select(0, "SELECT * FROM nope"))
    assert found[0] == ("streams", 0) + SELECT_PATH and "not nope" in found[1]
    assert problems(select(0, "SELECT * FROM account_daily_rows")) == [(("streams", 0) + SELECT_PATH, (
        "step 'campaigns' reads later step(s) account_daily_rows: a step reads its stream's requests and earlier "
        "steps"))]
    # exports are not tables: a step reads steps
    (found,) = problems(select(3, "SELECT account_id, day, sum(clicks) AS clicks FROM campaign_daily GROUP BY ALL"))
    assert found[0] == ("streams", 0, "transform", "steps", 3, "select") and "not campaign_daily" in found[1]
    assert problems(lambda stream: stream["export"]["account_daily"].update(primary_key=["account_id", "x"])) == [(
        ("streams", 0, "export", "account_daily", "primary_key", 1),
        "primary_key 'x' is not a column of step 'account_daily_rows' (columns: account_id, day, clicks)")]
    assert problems(lambda stream: stream["incremental"].update(cursor_field="date")) == [(
        ("streams", 0, "incremental", "cursor_field"),
        "cursor_field 'date' is not a column of an export (columns: campaign_daily: campaign_id, day, clicks, "
        "campaign_name, account_id; account_daily: account_id, day, clicks)")]
    assert problems(lambda stream: stream["export"]["account_daily"].update(step="nope")) == [(
        ("streams", 0, "export", "account_daily", "step"),
        "'nope' is not a step of this stream (steps: account_daily_rows, campaign_daily_rows, campaigns, stats)")]


def test_streams_take_one_form_and_page_mode_reads_one_request(api):
    """What adapt-validate rejects stops a run too, before any request (e.g. sources run from Python)."""
    old = {"name": "old", "request": {"http": {"path": "/x"}}, "select": "SELECT 1 AS x FROM records"}
    assert set(sql.check_source({"streams": [old]})) == {
        (("streams", 0, "export"), "a stream needs at least one export: `export: {NAME: {step: STEP}}`"),
        (("streams", 0, "requests"), "a stream needs `requests`, a list of named requests: a stream reads only "
                                     "its own requests"),
        (("streams", 0, "transform"), "a stream needs `transform: {mode: page|run, steps: [...]}`")}
    with pytest.raises(SourceError, match="stream 'old': requests: a stream needs `requests`"):
        run(source_with(api, old))
    stream = page_stream("items", {"http": {"path": "/x"}, "partitions": [{"name": "p", "values": [1]}]},
                         "SELECT 1 AS x FROM records")
    stream["requests"].append({"name": "more", "http": {"path": "/y"}})
    assert sql.check_source({"streams": [stream]}) == [
        (("streams", 0, "requests"), "`transform.mode: page` reads one request: with 2 requests, use `mode: run`"),
        (("streams", 0, "requests", 0, "partitions"), "request partitions need `transform.mode: run`")]
    stream = run_mode("items", [{"name": "raw", "http": {"path": "/x"}},
                                {"name": "more", "http": {"path": "/y"},
                                 "partitions": [{"name": "p", "from": "nope", "field": "x"}]}],
                      [{"name": "rows", "select": "SELECT 1 AS x"}], {"items": {"step": "rows"}, "bad": "junk"})
    stream["transform"]["mode"] = "batch"
    assert sql.check_source({"streams": [stream]}) == [
        (("streams", 0, "transform", "mode"), "`transform.mode` must be page or run"),
        (("streams", 0, "export", "bad"), "export 'bad' needs `step`: the step it writes"),
        (("streams", 0, "requests", 1, "partitions", 0, "from"),
         "'nope' is not an earlier request or a step of this stream")]


def test_check_source_reports_export_names_used_twice_or_naming_another_stream(api):
    first = page_stream("first", {"http": {"path": "/x"}}, "SELECT 1 AS x FROM records")
    second = page_stream("second", {"http": {"path": "/x"}}, "SELECT 1 AS x FROM records")
    second["export"] = {"First": {"step": "second"}}
    third = page_stream("third", {"http": {"path": "/x"}}, "SELECT 1 AS x FROM records")
    third["export"] = {"second": {"step": "third"}}
    assert sql.check_source({"streams": [first, second, third]}) == [
        (("streams", 1, "export", "First"), "export 'First' is also an export of stream 'first': export names are "
                                            "unique in a source (names ignore case)"),
        (("streams", 2, "export", "second"), "export 'second' has the name of stream 'second': an export can have "
                                             "its own stream's name, not another's")]


def test_check_source_reports_within_stream_request_partition_cycle():
    source = {"kind": "source", "name": "demo", "streams": [run_mode("cycle", [
        {"name": "parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "children", "partitions": [{"name": "id", "from": "child_rows", "field": "id"}],
         "http": {"path": "/children/{{ partition.id }}"}, "records": {"path": "data"}}],
        [{"name": "child_rows", "select": "SELECT record->>'id' AS id FROM children"}],
        {"cycle": {"step": "child_rows"}})]}
    assert sql.check_source(source) == [(("streams", 0, "requests", 1, "partitions", 0, "from"), (
        "requests and steps in a cycle: children -> child_rows -> children (a request partitioned `from:` a step "
        "runs after it, and a step after the requests it reads)"))]


def test_request_partition_cycle_stops_the_run_before_any_request(api):
    source = source_with(api, run_mode("cycle", [
        {"name": "parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "children", "partitions": [{"name": "id", "from": "child_rows", "field": "id"}],
         "http": {"path": "/children/{{ partition.id }}"}, "records": {"path": "data"}}], [
        {"name": "parent_rows", "select": "SELECT record->>'id' AS id FROM parents"},
        {"name": "child_rows", "select": "SELECT c.record->>'id' AS id FROM children c, parent_rows"}],
        {"cycle": {"step": "child_rows"}}))
    with pytest.raises(SourceError, match=r"stream 'cycle': requests\[1\].partitions\[0\].from: requests and steps "
                                          r"in a cycle: children -> child_rows -> children"):
        run(source)
    assert api.requests == []


def test_check_source_reports_from_stream_cycles(api):
    first = page_stream("first", {"http": {"path": "/x"}}, "SELECT 1 AS id FROM records",
                        partitions=[{"name": "a", "from_stream": "second", "field": "id"}])
    second = page_stream("second", {"http": {"path": "/x"}}, "SELECT 1 AS id FROM records",
                         partitions=[{"name": "b", "from_stream": "first", "field": "id"}])
    source = source_with(api, first, second)
    assert sql.check_source(source) == [(("streams", 1, "partitions", 0, "from_stream"), (
        "streams in a cycle: first -> second -> first (a stream runs after its `from_stream` parents)"))]
    with pytest.raises(SourceError, match="streams in a cycle: first -> second -> first"):
        run(source, streams=["first"])
    assert api.requests == []


def test_check_source_rejects_duckdb_builtin_request_and_step_names():
    source = {"kind": "source", "name": "demo", "streams": [run_mode(
        "builtins", [{"name": "pg_settings", "http": {"path": "/x"}}],
        [{"name": "duckdb_tables", "select": "SELECT 1 AS id"},
         {"name": "rows", "select": "SELECT * FROM pg_settings"}],
        {"builtins": {"step": "rows"}})]}
    assert sql.check_source(source) == [
        (("streams", 0, "requests", 0, "name"), "'pg_settings' is the name of a DuckDB built-in table or view: name it "
                                                "otherwise"),
        (("streams", 0, "transform", "steps", 0, "name"), "'duckdb_tables' is the name of a DuckDB built-in table or "
                                                          "view: name it otherwise")]
    # names that adapt-validate reports do not stop the DuckDB checks
    source = {"kind": "source", "name": "demo", "streams": [run_mode(
        "odd", [{"name": "raw", "http": {"path": "/x"}}, {"name": "bad name", "http": {}}, "junk", {"name": None}],
        [{"name": "rows", "select": "SELECT 1 AS id"}, {"name": 5, "select": "SELECT 2"}, "junk"],
        {"bad name": {"step": "rows"}, "records": {"step": "rows"}}), "junk"]}
    assert sql.check_source(source) == []
    # (a stream without a request that has a name cannot run: it has no requests)
    source["streams"][0]["requests"] = source["streams"][0]["requests"][1:]
    assert sql.check_source(source) == [(("streams", 0, "requests"), "a stream needs `requests`, a list of named "
                                                                     "requests: a stream reads only its own requests")]


def test_check_source_reports_missing_request_partition_field_path():
    source = {"kind": "source", "name": "demo", "streams": [run_mode("missing_field", [
        {"name": "parents", "http": {"path": "/parents"}, "records": {"path": "data"}},
        {"name": "children", "partitions": [{"name": "id", "from": "parent_rows", "field": "missing"}],
         "http": {"path": "/children/{{ partition.id }}"}, "records": {"path": "data"}}], [
        {"name": "parent_rows", "select": "SELECT record->>'id' AS id FROM parents"},
        {"name": "child_rows", "select": "SELECT record->>'id' AS id FROM children"}],
        {"child_list": {"step": "child_rows"}})]}
    assert sql.check_source(source) == [(("streams", 0, "requests", 1, "partitions", 0, "field"),
                                         "'missing' is not a column of step 'parent_rows' (columns: id)")]


def test_check_source_reports_tables_duckdb_cannot_make(monkeypatch):
    create_empty = sql.Sandbox.create_empty

    def fail_for_step(sandbox, name, columns):
        if name == "rows":
            raise sql.SqlError("Catalog Error: no room")
        return create_empty(sandbox, name, columns)
    monkeypatch.setattr(sql.Sandbox, "create_empty", fail_for_step)
    source = {"kind": "source", "name": "demo", "streams": [run_mode(
        "report", [{"name": "raw", "http": {"path": "/x"}}],
        [{"name": "rows", "select": "SELECT 1 AS id"}, {"name": "more", "select": "SELECT * FROM rows"}],
        {"report": {"step": "more"}})]}
    assert sql.check_source(source) == [(("streams", 0) + SELECT_PATH,
                                         "DuckDB cannot make the table of step 'rows': Catalog Error: no room")]


def test_check_source_reports_step_columns_that_are_not_field_names():
    source = {"kind": "source", "name": "demo", "streams": [run_mode("counts", [
        {"name": "raw", "http": {"path": "/x"}}], [
        {"name": "counted", "select": "SELECT count(*), 1 AS ok FROM range(3)"},
        {"name": "internal", "select": "SELECT a, sum(n), count(*) AS A FROM (SELECT 1 AS a, 2 AS n) GROUP BY a"},
        {"name": "cased", "select": "SELECT 1 AS id, 2 AS ID"}], {"counts": {"step": "cased"}})]}
    assert sql.check_source(source) == [
        (("streams", 0, "transform", "steps", 0, "select"),
         "column 'count_star()' is not a valid field name (letters, digits and _, not starting with a digit): name "
         "it with AS"),
        (("streams", 0, "transform", "steps", 1, "select"),
         "column 'sum(n)' is not a valid field name (letters, digits and _, not starting with a digit): name it with "
         "AS"),
        (("streams", 0, "transform", "steps", 2, "select"), "columns named more than once: ID, id (names ignore case)")]


# * ---
# * CLI
# * ---

def test_cli_accepts_export_names(tmp_path, monkeypatch, api, capsys):
    source = demo(api)
    source["spec"] = {"secrets": {"token": {"type": "string"}}}
    path = tmp_path / "source.yaml"
    path.write_text(json.dumps(source))
    monkeypatch.setenv("ADAPT_SECRET_TOKEN", "secret-token-1")
    assert cli.main(["run", str(path), "--stream", "account_daily"]) == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    schemas = [message["stream"] for message in messages if message["type"] == "SCHEMA"]
    assert schemas == ["campaign_daily", "account_daily"]


def test_validate_source_folder_reports_step_sql_file_and_select_line(tmp_path, capsys):
    folder = tmp_path / "demo"
    streams = folder / "streams"
    streams.mkdir(parents=True)
    (folder / "source.yaml").write_text("kind: source\nname: demo\nhttp: {base_url: 'https://example.com'}\n")
    lines = [
        "requests:",
        "  - name: raw_items",
        "    http: {path: /items}",
        "    records: {path: data}",
        "transform:",
        "  mode: page",
        "  steps:",
        "    - name: items",
        "      select: |",
        "        SELECT missing",
        "        FROM raw_items",
        "export:",
        "  items: {step: items}",
        "",
    ]
    (streams / "items.yaml").write_text("\n".join(lines))
    assert cli.main(["validate", str(folder)]) == 1
    out = capsys.readouterr().out
    line = next(number for number, text in enumerate(lines, 1) if "select:" in text)
    column = lines[line - 1].index("select") + 1
    assert "%s:%d:%d: error [sql-check] transform.steps[0].select:" % (streams / "items.yaml", line, column) in out
    assert 'Referenced column "missing" not found' in out
