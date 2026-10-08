import csv
import datetime
import json
import logging
import os
import re
import subprocess
import sys
from urllib.parse import parse_qsl

import pytest
import requests

from streamwright.core import cli
from streamwright.core.net.http import RateLimiter, Redactor
from streamwright.core.config.inputs import InputError, as_date, parse_duration, read_values_file, resolve_inputs, \
    secrets_from_env
from streamwright.core.outputs.output import FileOutput, export_files
from streamwright.core.engine.runner import SourceError, SourceRunner, partition_key
from streamwright.core.runtime.templates import render
from streamwright.core.runtime.testing import MemoryOutput, page_stream

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TODAY = datetime.date(2026, 10, 3)
ID = "SELECT (record->>'id')::BIGINT AS id FROM records"


# * --------------------
# * fake API and helpers
# * --------------------

def items_stream(name="items", request=None, select=ID, paginator=None, records=None, **stream):
    """A page-mode stream (testing.page_stream): its request (default GET /items, records at `data`) is `records`."""
    request = dict(request or {"http": {"path": "/items"}})
    if paginator is not None:
        request["paginator"] = paginator
    return page_stream(name, request, select, records={"path": "data"} if records is None else records, **stream)


def make_source(api, **stream):
    return {"kind": "source", "name": "demo", "auth": {"type": "bearer", "token": "{{ secrets.token }}"},
            "http": {"base_url": api.url, "retry": {"codes": [429, 500], "max_attempts": 3}},
            "streams": [items_stream(**stream)]}


class FakeApiStub(object):
    """For runners that are built but never send a request."""
    url = "http://127.0.0.1:9"


def write_source(tmp_path, monkeypatch, source, config=None):
    """Writes a CLI-runnable copy of `source` (JSON is YAML) and sets its token secret in the environment."""
    path = tmp_path / "source.yaml"
    spec = {"secrets": {"token": {"type": "string"}}}
    if config:
        spec["config"] = config
    path.write_text(json.dumps(dict(source, spec=spec)))
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    return path


class FlakySession(requests.Session):
    """Raises the given errors on the first requests, then talks to the server."""

    def __init__(self, errors):
        super(FlakySession, self).__init__()
        self.errors = list(errors)

    def request(self, *args, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        return super(FlakySession, self).request(*args, **kwargs)


def run(source, config=None, state=None, streams=None, sleeps=None):
    output = MemoryOutput()
    runner = SourceRunner(source, config or {}, {"token": "secret-token-1"}, state=state, output=output,
                          today=TODAY, sleep=(sleeps.append if sleeps is not None else lambda _: None))
    runner.run(streams)
    return output


# * ---------------------
# * templates and inputs
# * ---------------------

def test_render_keeps_types_for_whole_references():
    scopes = {"config": {"ids": ["1", "2"], "n": None, "name": "Ab"}, "window": {"start": TODAY, "end": TODAY},
              "today": TODAY}
    assert render("{{ config.ids }}", scopes) == ["1", "2"]
    assert render("/x/{{ config.ids | join('|') }}?d={{ window.start | date('%Y%m%d') }}", scopes) == \
        "/x/1|2?d=20261003"
    assert render({"a": ["{{ config.n | default(5) }}", "{{ config.name | lower }}"]}, scopes) == {"a": [5, "ab"]}
    assert render("{{ window.end }}", scopes) == TODAY and render("{{ today }}", scopes) == TODAY
    assert render("{{ config.missing }}", scopes) is None


def test_inputs_are_typed():
    definitions = {"ids": {"type": "list", "items": "integer"}, "on": {"type": "boolean", "default": False},
                   "since": {"type": "date", "default": "-30d"}, "name": {"type": "string", "required": False}}
    values = resolve_inputs(definitions, {"ids": "1, 2,3"}, "config", today=TODAY)
    assert values == {"ids": [1, 2, 3], "on": False, "since": datetime.date(2026, 9, 3), "name": None}
    with pytest.raises(InputError) as error:
        resolve_inputs(definitions, {"ids": "1,x", "extra": 1}, "config")
    assert "whole number" in str(error.value) and "unknown config: extra" in str(error.value)
    with pytest.raises(InputError, match="missing required config 'ids'"):
        resolve_inputs(definitions, {}, "config")


def test_dates_durations_files_and_environment(tmp_path):
    assert as_date("today", TODAY) == TODAY and as_date("2025-01-02T10:00:00", TODAY) == datetime.date(2025, 1, 2)
    assert parse_duration("15s").total_seconds() == 15 and parse_duration("7d").days == 7
    with pytest.raises(InputError):
        parse_duration("1 week")
    path = tmp_path / "secrets.yaml"
    path.write_text("token: abc\n")
    os.chmod(str(path), 0o644)
    warnings = []
    assert read_values_file(str(path), warn=warnings.append) == {"token": "abc"} and "chmod 600" in warnings[0]
    environ = {"STREAMWRIGHT_SECRET_CLIENT_ID": "x", "STREAMWRIGHT_SECRET_APIKEY": "k", "STREAMWRIGHT_SECRET_OTHER": "o", "OTHER": "y"}
    assert secrets_from_env(["client_id", "apiKey"], environ) == {"client_id": "x", "apiKey": "k"}
    with pytest.raises(InputError) as error:
        resolve_inputs({"pin": {"type": "integer"}}, {"pin": "abc-secret"}, "secret", hide_values=True)
    assert str(error.value) == "secret 'pin' is not a valid integer"


# * ----
# * HTTP
# * ----

def test_retries_honour_retry_after_and_stop_on_other_errors(api):
    replies = [(429, {}, {"Retry-After": "2"}), (500, {}), (200, {"data": [{"id": 1}]})]
    api.routes[("GET", "/items")] = lambda request: replies.pop(0)
    sleeps = []
    output = run(make_source(api), sleeps=sleeps)
    assert [r for _, r in output.records] == [{"id": 1}] and sleeps == [2.0, 2]
    assert api.requests[0]["headers"]["Authorization"] == "Bearer secret-token-1"
    api.routes[("GET", "/items")] = lambda request: (400, {"error": "bad request secret-token-1"})
    with pytest.raises(SourceError) as error:
        run(make_source(api))
    assert "HTTP 400" in str(error.value) and "secret-token-1" not in str(error.value) and "***" in str(error.value)


def test_rate_limiter_waits_with_a_fake_clock():
    now, sleeps = [0.0], []
    limiter = RateLimiter(2, 10.0, clock=lambda: now[0],
                          sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(3):
        limiter.wait()
        now[0] += 1.0
    assert sleeps == [8.0]


def test_oauth_token_is_fetched_once_and_refreshed_after_401(api):
    tokens = iter(["tok-1", "tok-2"])
    api.routes[("POST", "/token")] = lambda request: (200, {"access_token": next(tokens), "expires_in": 3600})
    seen = []

    def items(request):
        seen.append(request["headers"]["Authorization"])
        return (401, {}) if len(seen) == 2 else (200, {"data": [{"id": len(seen)}]})
    api.routes[("GET", "/items")] = items
    source = make_source(api, partitions=[{"name": "p", "values": ["a", "b", "c"]}])
    source["auth"] = {"type": "oauth2_refresh_token", "token_url": api.url + "/token", "client_id": "cid",
                      "client_secret": "{{ secrets.token }}", "refresh_token": "{{ secrets.token }}"}
    output = run(source)
    assert seen == ["Bearer tok-1", "Bearer tok-1", "Bearer tok-2", "Bearer tok-2"]
    assert len(output.records) == 3
    form = dict(parse_qsl(api.calls("/token")[0]["body"]))
    assert form == {"grant_type": "refresh_token", "refresh_token": "secret-token-1", "client_id": "cid",
                    "client_secret": "secret-token-1"}


@pytest.mark.parametrize("paginator,pages", [
    ({"type": "page_number", "page_param": "page", "page_size": 2, "size_param": "per_page"},
     {"1": [1, 2], "2": [3, 4], "3": [5]}),
    ({"type": "cursor", "token_path": "next", "param": "after"}, {None: [1, 2], "t1": [3], "t2": []}),
    ({"type": "cursor", "next_url_path": "links.next"}, {None: [1], "2": [2, 3]}),
])
def test_paginators(api, paginator, pages):
    def items(request):
        params = request["params"]
        key = params.get("page") or params.get("after") or params.get("cursor")
        data = [{"id": i} for i in pages[key]]
        if paginator["type"] == "page_number":
            assert params["per_page"] == "2"
            return 200, {"data": data}
        if "token_path" in paginator:
            following = {None: "t1", "t1": "t2"}.get(key)
            return 200, dict({"data": data}, **({"next": following} if following else {}))
        return 200, {"data": data, "links": {"next": api.url + "/items?cursor=2" if key is None else None}}
    api.routes[("GET", "/items")] = items
    output = run(make_source(api, paginator=paginator))
    assert [r["id"] for _, r in output.records] == [i for page in pages.values() for i in page]


def test_a_paginator_that_does_not_advance_is_stopped(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    with pytest.raises(SourceError, match="not advancing"):
        run(make_source(api, paginator={"type": "offset", "offset_param": "o", "limit_param": "l", "page_size": 2}))


def test_an_earlier_page_may_repeat(api):
    pages = {"0": [1, 2], "2": [3, 4], "4": [1, 2], "6": []}
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": i} for i in pages[request["params"]["o"]]]})
    paginator = {"type": "offset", "offset_param": "o", "limit_param": "l", "page_size": 2}
    output = run(make_source(api, paginator=paginator))
    assert [r["id"] for _, r in output.records] == [1, 2, 3, 4, 1, 2]


def test_next_links_are_resolved_against_the_previous_request(api):
    def items(request):
        cursor = request["params"].get("cursor")
        following = {None: "/v2/items?cursor=2", "2": "?cursor=3"}.get(cursor)  # absolute path, then query only
        return 200, {"data": [{"id": int(cursor or 1)}], "next": following}
    api.routes[("GET", "/v2/items")] = items
    source = make_source(api, paginator={"type": "cursor", "next_url_path": "next"})
    source["http"]["base_url"] = api.url + "/v2"
    assert [r["id"] for _, r in run(source).records] == [1, 2, 3]
    assert [r["path"] for r in api.requests] == ["/v2/items"] * 3


def test_redactor_covers_the_forms_secrets_take():
    redact = Redactor(["a b/c+d=e", 12345678, "token-9\n", None, True, "abc"])
    text = "q=a+b%2Fc%2Bd%3De p=a%20b%2Fc%2Bd%3De raw=a b/c+d=e n=12345678 t=token-9 esc=token-9\\n short=abc"
    assert redact(text) == "q=*** p=*** raw=*** n=*** t=*** esc=*** short=abc"
    runner = SourceRunner(make_source(FakeApiStub()), {}, {"token": "secret-token-1", "pin": 12345678}, today=TODAY)
    assert runner.redact("pin=12345678 token=secret-token-1") == "pin=*** token=***"


def test_header_values_may_be_numbers(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = make_source(api)
    source["http"]["headers"] = {"X-Version": 202509}
    source["auth"] = {"type": "api_key", "name": "X-Key", "value": 12345678}
    assert len(run(source).records) == 1
    assert (api.requests[0]["headers"]["X-Version"], api.requests[0]["headers"]["X-Key"]) == ("202509", "12345678")


def test_broken_responses_are_retried_and_other_request_errors_follow_the_partition_policy(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    output, sleeps = MemoryOutput(), []
    session = FlakySession([requests.exceptions.ChunkedEncodingError("connection broken")])
    SourceRunner(make_source(api), {}, {"token": "secret-token-1"}, output=output, today=TODAY, session=session,
                 sleep=sleeps.append).run()
    assert len(output.records) == 1 and sleeps == [1]

    stream = {"partitions": [{"name": "p", "values": ["a", "b"]}], "on_partition_error": "skip"}
    output = MemoryOutput()
    session = FlakySession([requests.exceptions.TooManyRedirects("redirect loop")])
    SourceRunner(make_source(api, **stream), {}, {"token": "secret-token-1"}, output=output, today=TODAY,
                 session=session).run()
    assert len(output.records) == 1  # partition a failed (not retried) and was skipped
    session = FlakySession([requests.exceptions.InvalidURL("bad URL with secret-token-1")])
    with pytest.raises(SourceError) as error:
        SourceRunner(make_source(api), {}, {"token": "secret-token-1"}, output=MemoryOutput(), today=TODAY,
                     session=session).run()
    assert "bad URL" in str(error.value) and "secret-token-1" not in str(error.value)


def test_the_source_rate_limit_is_shared_by_streams(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = make_source(api)
    source["http"]["rate_limit"] = {"requests": 2, "per": "10s"}
    source["streams"] += [items_stream("more"), items_stream("again"),
                          items_stream("own", rate_limit={"requests": 5, "per": "10s"})]
    now, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    SourceRunner(source, {}, {"token": "secret-token-1"}, output=MemoryOutput(), today=TODAY, clock=lambda: now[0],
                 sleep=sleep).run()
    assert len(api.requests) == 4 and sleeps == [10.0]  # the third request waits; `own` has its own limit


# * --------------------------------------
# * partitions, incremental state, errors
# * --------------------------------------

def test_incremental_windows_and_state(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": request["params"]["since"]}]})
    stream = {"incremental": {"cursor_field": "id", "start": "2026-09-25", "window": "4d", "lookback": "2d"},
              "request": {"http": {"path": "/items", "params": {"since": "{{ window.start }}",
                                                               "until": "{{ window.end }}"}}},
              "select": "SELECT record->>'id' AS id FROM records",
              "partitions": [{"name": "account", "values": "{{ config.accounts }}"}]}
    output = run(make_source(api, **stream), config={"accounts": ["a"]})
    windows = [(r["params"]["since"], r["params"]["until"]) for r in api.requests]
    assert windows == [("2026-09-25", "2026-09-28"), ("2026-09-29", "2026-10-02"), ("2026-10-03", "2026-10-03")]
    key = partition_key({"account": "a"})
    # today is read but not complete, so the bookmark stops at yesterday
    assert [s["bookmarks"]["items"][key] for s in output.states] == ["2026-09-28", "2026-10-02", "2026-10-02"]

    api.requests.clear()
    run(make_source(api, **stream), config={"accounts": ["a"]}, state=output.states[-1])
    assert [r["params"]["since"] for r in api.requests] == ["2026-10-01"]  # cursor + 1 day - 2 days of lookback


def test_old_bookmarks_win_over_start_and_bookmarks_never_move_back(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": request["params"]["since"]}]})
    stream = {"incremental": {"cursor_field": "id", "start": "-3d", "window": "1d", "lookback": "3d"},
              "request": {"http": {"path": "/items", "params": {"since": "{{ window.start }}"}}},
              "select": "SELECT record->>'id' AS id FROM records"}
    # the bookmark is older than `start` (-3d = 2026-09-30): resume from it so no days are skipped
    output = run(make_source(api, **stream), state={"bookmarks": {"items": {"{}": "2026-09-20"}}})
    since = [r["params"]["since"] for r in api.requests]
    assert (since[0], since[-1], len(since)) == ("2026-09-21", "2026-10-03", 13)
    assert output.states[-1]["bookmarks"]["items"]["{}"] == "2026-10-02"

    # lookback re-reads days before the bookmark (not before `start`) without moving the bookmark back
    api.requests.clear()
    output = run(make_source(api, **stream), state={"bookmarks": {"items": {"{}": "2026-10-01"}}})
    assert [r["params"]["since"] for r in api.requests] == ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]
    assert [s["bookmarks"]["items"]["{}"] for s in output.states] == ["2026-10-01", "2026-10-01", "2026-10-02",
                                                                      "2026-10-02"]


@pytest.mark.parametrize("window", ["0d", "12h"])
def test_windows_must_be_whole_days(api, window):
    with pytest.raises(SourceError, match="whole days"):
        run(make_source(api, incremental={"cursor_field": "id", "start": "today", "window": window}))
    assert api.requests == []


def test_a_from_stream_parent_runs_with_its_own_state_and_writes_its_export(api):
    api.routes[("GET", "/accounts")] = lambda request: (200, {"data": [{"id": 1}]})
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [{"id": 5}]})
    source = make_source(api, request={"http": {"path": "/accounts", "params": {"since": "{{ window.start }}"}}},
                         incremental={"cursor_field": "id", "start": "2026-10-01"})
    source["streams"].append(items_stream(
        "campaigns", partitions=[{"name": "account", "from_stream": "items", "field": "id"}],
        incremental={"cursor_field": "id", "start": "2026-10-03"},
        request={"http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}}}))
    output = run(source, state={"bookmarks": {"items": {"{}": "2026-10-02"}}}, streams=["campaigns"])
    # The selected child runs with its parent, which writes its export and keeps its own bookmark; both write their
    # state after each window.
    assert [r["params"]["since"] for r in api.calls("/accounts")] == ["2026-10-03"]
    assert [name for name, _ in output.records] == ["items", "campaigns"]
    assert output.states == [{"bookmarks": {"items": {"{}": "2026-10-02"}}},
                             {"bookmarks": {"items": {"{}": "2026-10-02"},
                                            "campaigns": {partition_key({"account": 1}): "2026-10-02"}}}]


def test_partitions_from_a_parent_stream_and_explode(api):
    api.routes[("GET", "/accounts")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}, {"id": 1}]})
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": [
        {"account": request["params"]["account"], "ads": [{"ad": "x"}, {"ad": "y"}]}]})
    source = make_source(api, request={"http": {"path": "/accounts"}})
    source["streams"].append(items_stream(
        "ads", partitions=[{"name": "account", "from_stream": "items", "field": "id"}],
        request={"http": {"path": "/campaigns", "params": {"account": "{{ partition.account }}"}}},
        records={"path": "data", "explode": "ads"},
        select="SELECT (record->>'account')::BIGINT AS account, record->>'ad' AS ad FROM records"))
    output = run(source, streams=["ads"])
    assert [r for name, r in output.records if name == "items"] == [{"id": 1}, {"id": 2}, {"id": 1}]
    assert [r for name, r in output.records if name == "ads"] == [
        {"account": 1, "ad": "x"}, {"account": 1, "ad": "y"},
        {"account": 2, "ad": "x"}, {"account": 2, "ad": "y"}]
    assert list(output.schemas) == ["items", "ads"]


def test_partitions_from_several_fields_of_a_parent(api):
    """accounts -> campaigns -> ad groups: each level keeps the parent fields it needs, crossed with other items."""
    api.routes[("GET", "/accounts")] = lambda request: (200, {"data": [{"id": "a1"}, {"id": "a2"}]})
    campaigns = {"a1": [{"id": "c1"}, {"id": "c2"}, {"id": "c1"}], "a2": [{"id": "c3"}, {"id": None}]}
    api.routes[("GET", "/campaigns")] = lambda request: (200, {"data": campaigns[request["params"]["account"]]})
    api.routes[("GET", "/ad_groups")] = lambda request: (200, {"data": [{"id": request["params"]["campaign"] + "g"}]})
    source = make_source(api, name="accounts", request={"http": {"path": "/accounts"}},
                         select="SELECT record->>'id' AS account_id FROM records")
    source["streams"] += [items_stream(
        "campaigns",
        partitions=[{"name": "account_id", "from_stream": "accounts", "field": "account_id"}],
        request={"http": {"path": "/campaigns", "params": {"account": "{{ partition.account_id }}"}}},
        select="SELECT partition->>'account_id' AS account_id, record->>'id' AS campaign_id FROM records",
    ), items_stream(
        "ad_groups",
        partitions=[{"from_stream": "campaigns", "fields": ["account_id", "campaign_id"]},
                    {"name": "device", "values": ["mobile", "desktop"]}],
        incremental={"cursor_field": "ad_group_id", "start": "today"},
        request={"http": {"path": "/ad_groups", "params": {
            "account": "{{ partition.account_id }}", "campaign": "{{ partition.campaign_id }}",
            "device": "{{ partition.device }}"}}},
        select="""SELECT partition->>'account_id' AS account_id, partition->>'campaign_id' AS campaign_id,
                         partition->>'device' AS device, record->>'id' AS ad_group_id FROM records""",
    )]
    output = run(source, streams=["ad_groups"])
    expected = [("a1", "c1", "mobile"), ("a1", "c1", "desktop"), ("a1", "c2", "mobile"), ("a1", "c2", "desktop"),
                ("a2", "c3", "mobile"), ("a2", "c3", "desktop")]
    # one partition per distinct (account, campaign): a repeated campaign and one without an ID give no more
    ad_groups = [r for name, r in output.records if name == "ad_groups"]
    assert [(r["account_id"], r["campaign_id"], r["device"]) for r in ad_groups] == expected
    assert [r["ad_group_id"] for r in ad_groups][:2] == ["c1g", "c1g"]
    assert len(api.calls("/ad_groups")) == 6 and list(output.schemas) == ["accounts", "campaigns", "ad_groups"]
    assert sorted(output.states[-1]["bookmarks"]["ad_groups"]) == sorted(
        partition_key({"account_id": a, "campaign_id": c, "device": d}) for a, c, d in expected)


def test_error_policies(api):
    api.routes[("GET", "/items")] = lambda request: (
        (500, {}) if request["params"].get("p") == "bad" else (200, {"data": [{"id": "1"}, {"id": "x"}]}))
    stream = {"partitions": [{"name": "p", "values": ["good", "bad"]}],
              "request": {"http": {"path": "/items", "params": {"p": "{{ partition.p }}"}}}}
    # a value the step cannot convert fails its page, like a failed request; the error names the step
    with pytest.raises(SourceError, match=r"""partition \{"p": "good"\}: step 'items': .*Could not convert """
                                          r"""string 'x'"""):
        run(make_source(api, **stream))
    assert run(make_source(api, on_partition_error="skip", **stream)).records == []
    # TRY_CAST turns values that do not convert into null instead
    output = run(make_source(api, on_partition_error="skip", select="SELECT TRY_CAST(record->>'id' AS BIGINT) AS id "
                                                                    "FROM records", **stream))
    assert [r for _, r in output.records] == [{"id": 1}, {"id": None}]


def test_records_as_returned_and_connector_requests(api):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1, "x": {"y": 2}}]})
    source = make_source(api, select="SELECT record FROM records")  # records as the API returned them
    assert [r for _, r in run(source).records] == [{"record": {"id": 1, "x": {"y": 2}}}]
    source["streams"][0]["requests"][0] = {"name": "records", "sdk": "google_ads", "method": "search"}
    with pytest.raises(SourceError, match=r"stream 'items': requests\[0\]: sdk requests need a connector "
                                          r"`auth.provider`"):
        run(source)
    # a request's failure names it
    source = make_source(api, request={"http": {"path": "/missing"}})
    with pytest.raises(SourceError, match=r"stream 'items', partition \{\}: request 'records': GET \S+/missing "
                                          r"failed: HTTP 404"):
        run(source)


# * ---------------
# * CLI and output
# * ---------------

def test_file_output_and_state(api, tmp_path):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    source_path = tmp_path / "source.yaml"
    source_path.write_text(json.dumps(dict(make_source(api, incremental={"cursor_field": "id", "start": "today"},
                                                       request={"http": {"path": "/items", "params": {
                                                           "since": "{{ window.start }}"}}}),
                                           spec={"secrets": {"token": {"type": "string"}}})))
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("token: secret-token-1\n")
    os.chmod(str(secrets), 0o600)
    out = tmp_path / "out"
    assert cli.main(["run", str(source_path), "--secrets", str(secrets), "--output", "csv:%s" % out]) == 0
    files = sorted(os.listdir(str(out)))
    assert files[-1] == "state.json" and files[0].startswith("items.") and files[0].endswith(".csv")
    with open(str(out / files[0])) as stream:
        assert stream.read().splitlines() == ["id", "1", "2"]
    with open(str(out / "state.json")) as stream:
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        assert json.load(stream) == {"bookmarks": {"items": {"{}": yesterday.isoformat()}}}


def test_timezone_sets_the_run_date_and_windows(api, tmp_path):
    import zoneinfo
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source_path = tmp_path / "source.yaml"
    source_path.write_text(json.dumps(dict(make_source(api, incremental={"cursor_field": "id", "start": "today"},
                                                       request={"http": {"path": "/items", "params": {
                                                           "since": "{{ window.start }}"}}}),
                                           spec={"secrets": {"token": {"type": "string"}}})))
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("token: secret-token-1\n")
    os.chmod(str(secrets), 0o600)

    def bookmark(tz):
        out = tmp_path / ("out_" + tz.replace("/", "_"))
        assert cli.main(["run", str(source_path), "--secrets", str(secrets), "--timezone", tz,
                         "--output", "csv:%s" % out]) == 0
        with open(str(out / "state.json")) as stream:
            return json.load(stream)["bookmarks"]["items"]["{}"]

    # the run's `today` (and the window) is the client-local date: the last complete day is that date minus one
    for tz in ("Pacific/Kiritimati", "Pacific/Niue"):  # UTC+14 and UTC-11
        expected = (datetime.datetime.now(zoneinfo.ZoneInfo(tz)).date() - datetime.timedelta(days=1)).isoformat()
        assert bookmark(tz) == expected
    # 25 hours apart, so their local dates are always different -- proving the flag takes effect
    assert bookmark("Pacific/Kiritimati") != bookmark("Pacific/Niue")


def test_cli_validate_takes_options_before_paths(tmp_path, capsys):
    example = os.path.join(REPO_ROOT, "examples", "sources", "readers", "files_demo")
    assert cli.main(["validate", "--strict", example]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: source\nname: x\nstreams: []\n")
    assert cli.main(["validate", "--format", "github", str(bad)]) != 0
    assert "::error" in capsys.readouterr().out


def folder_source(api, tmp_path, monkeypatch):
    """A source folder with the streams `items` and `others`, and its token secret in the environment."""
    folder = tmp_path / "demo"
    (folder / "streams").mkdir(parents=True)
    source = make_source(api)
    del source["streams"]
    source["spec"] = {"config": {"limit": {"type": "integer", "default": 10}},
                      "secrets": {"token": {"type": "string"}}}
    (folder / "source.yaml").write_text(json.dumps(source))
    for name in ("items", "others"):
        body = items_stream(name, request={"http": {"path": "/" + name, "params": {"limit": "{{ config.limit }}"}}})
        del body["name"]  # a stream file is named after the file
        (folder / "streams" / ("%s.yaml" % name)).write_text(json.dumps(body))
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    api.routes[("GET", "/others")] = lambda request: (200, {"data": [{"id": 2}]})
    monkeypatch.setenv("STREAMWRIGHT_SECRET_TOKEN", "secret-token-1")
    return folder


def records_by_stream(out):
    messages = [json.loads(line) for line in out.splitlines()]
    return [(m["stream"], m["record"]) for m in messages if m["type"] == "RECORD"]


def test_a_source_folder_runs_as_one_source(api, tmp_path, capsys, monkeypatch):
    folder = folder_source(api, tmp_path, monkeypatch)
    assert cli.main(["run", str(folder)]) == 0
    assert records_by_stream(capsys.readouterr().out) == [("items", {"id": 1}), ("others", {"id": 2})]
    assert cli.main(["run", str(folder / "source.yaml"), "--stream", "others"]) == 0
    assert records_by_stream(capsys.readouterr().out) == [("others", {"id": 2})]


def test_a_config_file_sets_values_and_streams(api, tmp_path, capsys, monkeypatch):
    folder = folder_source(api, tmp_path, monkeypatch)
    client = tmp_path / "acme.yaml"
    client.write_text("config:\n  limit: 5\nstreams: [others]\n")
    assert cli.main(["run", str(folder), "--config", str(client)]) == 0
    assert records_by_stream(capsys.readouterr().out) == [("others", {"id": 2})]
    assert [call["params"]["limit"] for call in api.calls("/others")] == ["5"]
    # --stream replaces the file's streams, and --set overrides its values
    assert cli.main(["run", str(folder), "--config", str(client), "--stream", "items", "--set", "limit=7"]) == 0
    assert records_by_stream(capsys.readouterr().out) == [("items", {"id": 1})]
    assert [call["params"]["limit"] for call in api.calls("/items")] == ["7"]


@pytest.mark.parametrize("text,problem", [
    ("limit: 5\n", "unknown section(s) limit (sections: config, streams); config values go under `config:`"),
    ("config: {limit: 5}\nsecrets: {token: x}\n", "secrets go in --secrets FILE or STREAMWRIGHT_SECRET_<NAME> variables"),
    ("config: [5]\n", "`config` must be a mapping of names to values"),
    ("streams: []\n", "`streams` must be a list of stream names"),
    ("streams: [itmes]\n", "unknown stream(s): itmes (streams/exports: items, others)"),
    ("config: {limt: 5}\n", "unknown config: limt (declared: limit)"),
])
def test_config_file_problems_stop_the_run(api, tmp_path, caplog, monkeypatch, text, problem):
    folder = folder_source(api, tmp_path, monkeypatch)
    client = tmp_path / "client.yaml"
    client.write_text(text)
    assert cli.main(["run", str(folder), "--config", str(client)]) == 2
    assert problem in caplog.text and api.requests == []


def test_a_streams_folder_and_a_stream_named_source(api, tmp_path, capsys, caplog, monkeypatch):
    folder = folder_source(api, tmp_path, monkeypatch)
    os.rename(str(folder / "streams" / "others.yaml"), str(folder / "streams" / "source.yaml"))
    assert cli.main(["run", str(folder / "streams")]) == 0  # the streams folder stands for its source
    # the stream `source` keeps its export `others`
    assert records_by_stream(capsys.readouterr().out) == [("items", {"id": 1}), ("others", {"id": 2})]
    assert cli.main(["run", str(folder / "streams" / "source.yaml")]) == 2
    assert "is a stream of the source folder %s; run the folder: streamwright run %s --stream source" % (
        folder, folder) in caplog.text


def test_source_folder_problems_stop_the_run(api, tmp_path, caplog, monkeypatch):
    folder = folder_source(api, tmp_path, monkeypatch)
    stream_file = folder / "streams" / "others.yaml"
    assert cli.main(["run", str(stream_file)]) == 2
    assert "%s is a stream of the source folder %s; run the folder: streamwright run %s --stream others" % (
        stream_file, folder, folder) in caplog.text
    assert cli.main(["run", str(folder), "--stream", "itmes"]) == 2
    assert "unknown stream(s): itmes (streams/exports: items, others)" in caplog.text
    body = json.loads(stream_file.read_text())
    body["incremntal"] = {"cursor_field": "id", "start": "today"}  # a misspelt key
    stream_file.write_text(json.dumps(body))
    assert cli.main(["run", str(folder)]) == 2
    assert "%s:1:" % stream_file in caplog.text and "unknown key 'incremntal'" in caplog.text
    assert cli.main(["run", str(tmp_path / "nowhere")]) == 2
    assert "nowhere: no such file or folder" in caplog.text
    assert cli.main(["run", str(folder) + ".yaml"]) == 2  # the path of a source before it became a folder
    assert "did you mean the source folder %s?" % folder in caplog.text
    assert api.requests == []


def test_csv_output_writes_mappings_as_json_and_lists_comma_joined(api, tmp_path, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1, "tags": ["a", "b"], "meta": {"k": 1}}]})
    source = make_source(api, select="SELECT (record->>'id')::BIGINT AS id, record, (record->'tags')::VARCHAR[] AS "
                                     "tags FROM records")
    path = write_source(tmp_path, monkeypatch, source)
    out = tmp_path / "out"
    assert cli.main(["run", str(path), "--output", "csv:%s" % out]) == 0
    (written,) = [name for name in os.listdir(str(out)) if name.endswith(".csv")]
    with open(str(out / written), newline="") as stream:
        assert list(csv.reader(stream)) == [
            ["id", "record", "tags"], ["1", '{"id": 1, "meta": {"k": 1}, "tags": ["a", "b"]}', "a,b"]]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_runs_leave_no_files(api, tmp_path, monkeypatch, interruption):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    path = write_source(tmp_path, monkeypatch, make_source(api))
    original = SourceRunner.run_stream

    def run_stream(self, stream):
        original(self, stream)  # the stream's file is open and holds a record
        raise interruption()
    monkeypatch.setattr(SourceRunner, "run_stream", run_stream)
    out = tmp_path / "out"
    arguments = ["run", str(path), "--output", "jsonl:%s" % out]
    if interruption is KeyboardInterrupt:
        assert cli.main(arguments) == 130
    else:
        with pytest.raises(SystemExit):
            cli.main(arguments)
    assert os.listdir(str(out)) == []


def test_the_log_filter_redacts_messages_and_tracebacks():
    try:
        try:
            raise ValueError("GET /items?api_key=secret-token-1")
        except ValueError:
            raise RuntimeError("request failed")
    except RuntimeError:
        record = logging.LogRecord("urllib3", logging.DEBUG, __file__, 1, "sent %s", ("secret-token-1",),
                                   sys.exc_info())
    assert cli._RedactFilter(Redactor(["secret-token-1"])).filter(record)
    text = logging.Formatter().format(record)
    assert "secret-token-1" not in text and "sent ***" in text and "api_key=***" in text


def test_debug_logging_keeps_secrets_out(api, tmp_path, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = make_source(api)
    source["auth"] = {"type": "api_key", "in": "query", "name": "api_key", "value": "{{ secrets.token }}"}
    path = write_source(tmp_path, monkeypatch, source)
    # a separate process, so the command configures logging itself (as it does for users)
    result = subprocess.run([sys.executable, "-m", "streamwright.core.cli", "run", str(path), "--log-level", "DEBUG"],
                            cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert api.requests[0]["params"]["api_key"] == "secret-token-1"
    assert "1 record(s)" in result.stderr and "secret-token-1" not in result.stderr + result.stdout


# * -----------
# * --file-name
# * -----------

def two_exports(api):
    """make_source, whose stream `items` has a second step and export: `doubled`."""
    source = make_source(api)
    stream = source["streams"][0]
    stream["transform"]["steps"].append({"name": "doubled", "select": "SELECT id * 2 AS doubled FROM items"})
    stream["export"]["doubled"] = {"step": "doubled"}
    return source


def files_in(folder):
    """The paths of the files in a folder and its subfolders, relative to it."""
    return sorted(os.path.relpath(os.path.join(root, name), str(folder))
                  for root, _, names in os.walk(str(folder)) for name in names)


def test_file_name_templates_name_each_export_file(api, tmp_path, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}, {"id": 2}]})
    path = write_source(tmp_path, monkeypatch, two_exports(api), config={"client": {"type": "string"}})
    out = tmp_path / "out"
    template = "{{ config.client }}/{{ source }}-{{ export }}_{{ today }}_{{ timestamp }}.jsonl"
    assert cli.main(["run", str(path), "--set", "client=acme", "--output", "jsonl:%s" % out,
                     "--file-name", template]) == 0
    files = files_in(out)
    today = datetime.date.today().isoformat()
    assert [re.sub(r"_\d{8}T\d{6}Z\.", "_TIMESTAMP.", name) for name in files] == [
        "acme/demo-doubled_%s_TIMESTAMP.jsonl" % today, "acme/demo-items_%s_TIMESTAMP.jsonl" % today]
    with open(str(out / files[0])) as stream:
        assert [json.loads(line) for line in stream] == [{"doubled": 2}, {"doubled": 4}]
    # the same names on the next run replace the files, and csv works the same way
    for _ in range(2):
        assert cli.main(["run", str(path), "--set", "client=acme", "--output", "csv:%s" % (tmp_path / "csv"),
                         "--file-name", "{{ export | upper }}.csv"]) == 0
    assert files_in(tmp_path / "csv") == ["DOUBLED.csv", "ITEMS.csv"]


@pytest.mark.parametrize("arguments,problem", [
    (["--file-name", "{{ source }}.jsonl"], "exports 'items' and 'doubled' get the same file 'demo.jsonl'"),
    (["--file-name", "../{{ export }}.jsonl"], "'../items.jsonl' (export 'items') goes outside the output directory"),
    (["--file-name", "/abs/{{ export }}.jsonl"], "'/abs/items.jsonl' (export 'items') is not a relative path"),
    (["--file-name", "{{ secrets.token }}.jsonl"], "{{ secrets.token }}: a file name can use {{ export }}"),
    (["--file-name", "{{ config.nope }}/{{ export }}.jsonl"], "'nope' is not a config input (declared: client)"),
    (["--file-name", "{{ config.client }}/{{ export }}.jsonl"], "config 'client' has no value"),
    (["--file-name", "state.json", "--stream", "items"], "'state.json' (export 'items') is the state file's name"),
    (["--file-name", "./State.JSON/{{ export }}.jsonl"], "'./State.JSON/items.jsonl' (export 'items') needs the "
                                                         "folder 'State.JSON', the state file's name"),
])
def test_file_name_problems_stop_the_run_before_any_request(api, tmp_path, monkeypatch, caplog, arguments, problem):
    config = {"client": {"type": "string", "required": False}}
    path = write_source(tmp_path, monkeypatch, two_exports(api), config=config)
    out = tmp_path / "out"
    assert cli.main(["run", str(path), "--output", "jsonl:%s" % out] + arguments) == 2
    assert problem in caplog.text and api.requests == [] and not out.exists()


@pytest.mark.parametrize("output", ["singer", "duckdb:warehouse.duckdb", "dlt:duckdb"])
def test_file_name_needs_file_output(api, tmp_path, monkeypatch, caplog, output):
    path = write_source(tmp_path, monkeypatch, make_source(api))
    assert cli.main(["run", str(path), "--output", output, "--file-name", "{{ export }}.jsonl"]) == 2
    assert "--file-name works only with --output jsonl:DIR, csv:DIR, tsv:DIR or parquet:DIR" in caplog.text
    assert api.requests == []


@pytest.mark.parametrize("arguments", [[], ["--file-name", "{{ export }}.jsonl"]])
def test_a_folder_named_state_json_stops_the_run_before_any_request(api, tmp_path, monkeypatch, caplog, arguments):
    path = write_source(tmp_path, monkeypatch, make_source(api))
    (tmp_path / "out" / "state.json").mkdir(parents=True)
    assert cli.main(["run", str(path), "--output", "jsonl:%s" % (tmp_path / "out")] + arguments) == 2
    assert "%s is a folder: the run writes its state to that path" % (tmp_path / "out" / "state.json") in caplog.text
    assert api.requests == [] and os.listdir(str(tmp_path / "out")) == ["state.json"]


def test_file_names_cannot_leave_the_directory_through_symbolic_links(api, tmp_path, monkeypatch, caplog):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    path = write_source(tmp_path, monkeypatch, make_source(api), config={"client": {"type": "string"}})
    outside, out = tmp_path / "outside", tmp_path / "out"
    outside.mkdir()
    out.mkdir()
    os.symlink(str(outside), str(out / "link"))
    arguments = ["run", str(path), "--output", "jsonl:%s" % out, "--file-name",
                 "{{ config.client }}/{{ export }}.jsonl"]
    assert cli.main(arguments + ["--set", "client=link"]) == 2
    assert "'link/items.jsonl' (export 'items') leads outside %s through a symbolic link" % out in caplog.text
    assert api.requests == [] and os.listdir(str(outside)) == []
    # a link made during the run: closing fails, and writes no file
    original = SourceRunner.run_stream

    def run_stream(self, stream):
        original(self, stream)
        os.symlink(str(outside), str(out / "acme"))
    monkeypatch.setattr(SourceRunner, "run_stream", run_stream)
    assert cli.main(arguments + ["--set", "client=acme"]) == 1
    assert "--file-name: 'acme/items.jsonl' leads outside %s through a symbolic link: no file was written" % out \
        in caplog.text
    assert len(api.requests) == 1 and os.listdir(str(outside)) == []
    assert sorted(os.listdir(str(out))) == ["acme", "link"]  # the links: no file, no temporary file


def test_export_files_are_paths_inside_the_output_directory(tmp_path):
    scopes = {"source": "demo", "today": TODAY, "timestamp": "20261003T101500Z",
              "config": {"client": "acme", "region": None, "ids": [1, 2]}}
    assert export_files("{{ config.client }}/{{ export }}_{{ today | date('%Y%m') }}_{{ config.ids }}.csv",
                        ["a", "b"], scopes) == {"a": "acme/a_202610_1,2.csv", "b": "acme/b_202610_1,2.csv"}
    assert export_files("./x//{{ export }}.jsonl", ["a"], scopes) == {"a": "x/a.jsonl"}
    (tmp_path / "acme").write_text("a file")
    (tmp_path / "b.jsonl").mkdir()
    for template, exports, problem in (
            ("{{ export }}/", ["a"], "'a/' (export 'a') names a folder, not a file"),
            ("{{ config.client }}.jsonl", ["a", "b"], "exports 'a' and 'b' get the same file 'acme.jsonl'"),
            ("x/{{ export | upper }}.jsonl", ["a", "x"], None),
            ("{{ export.name }}.jsonl", ["a"], "{{ export.name }}: a file name can use"),
            ("{{ config.region }}/{{ export }}.jsonl", ["a"], "config 'region' has no value"),
            ("{{ today | nope }}.jsonl", ["a"], "invalid filter 'nope'"),
            ("{{ config.client }}/{{ export }}.jsonl", ["a"], "'acme/a.jsonl' (export 'a') needs the folder 'acme', "
                                                              "a file in %s" % tmp_path),
            ("{{ export }}.jsonl", ["b"], "'b.jsonl' (export 'b') is a folder in %s" % tmp_path)):
        if problem is None:
            assert export_files(template, exports, scopes, str(tmp_path)) == {"a": "x/A.jsonl", "x": "x/X.jsonl"}
            continue
        with pytest.raises(ValueError) as error:
            export_files(template, exports, scopes, str(tmp_path))
        assert problem in str(error.value)
    # a path is never one export's file and another's folder
    with pytest.raises(ValueError, match="'x' is the file of export 'x' and a folder of export 'x/a''s file 'x/a'"):
        export_files("{{ export }}", ["x", "x/a"], scopes)


def test_file_names_are_written_atomically_into_their_folders(tmp_path):
    folder = tmp_path / "out"
    output = FileOutput(str(folder), "jsonl", {"items": os.path.join("acme", "items.jsonl")})
    output.write_schema("items", {"type": "object", "properties": {"id": {}}}, ["id"])
    output.write_record("items", {"id": 1})
    output.write_state({"bookmarks": {}})
    (hidden,) = os.listdir(str(folder))  # nothing to read yet, and no folder
    assert hidden.startswith(".items.jsonl.") and hidden.endswith(".part")
    output.close()
    assert files_in(folder) == [os.path.join("acme", "items.jsonl"), "state.json"]
    assert (folder / "acme" / "items.jsonl").read_text() == '{"id": 1}\n'
    output = FileOutput(str(tmp_path / "failed"), "csv", {"items": os.path.join("acme", "items.csv")})
    output.write_schema("items", {"type": "object", "properties": {"id": {}}}, [])
    output.write_record("items", {"id": 1})
    output.close(failed=True)
    assert os.listdir(str(tmp_path / "failed")) == []


# * --------------------------------------------
# * the state written per window, without copies
# * --------------------------------------------

class StateHistory(MemoryOutput):
    """A MemoryOutput keeping each state written as JSON (not a copy); writing a state with `fail_on` fails."""

    def __init__(self, fail_on=None):
        super(StateHistory, self).__init__()
        self.fail_on = fail_on

    def write_state(self, state):
        if self.fail_on in state["bookmarks"]:
            raise OSError("cannot write the state")
        self.states.append(json.loads(json.dumps(state)))


def counted_copies(monkeypatch):
    """The runner's copy.deepcopy calls (one entry per call)."""
    import copy
    import types
    from streamwright.core.engine import runner as runner_module
    calls = []

    def deepcopy(value, *args):
        calls.append(1)
        return copy.deepcopy(value, *args)
    monkeypatch.setattr(runner_module, "copy", types.SimpleNamespace(deepcopy=deepcopy))
    return calls


def windowed_items(name, mode, accounts):
    """An incremental stream over `accounts` partitions, one window each (yesterday to today)."""
    request = {"name": "raw_%s" % name, "http": {"path": "/items", "params": {"account": "{{ partition.account }}",
                                                                               "day": "{{ window.start }}"}},
               "records": {"path": "data"}}
    return {"name": name, "requests": [request],
            "partitions": [{"name": "account", "values": ["a%d" % number for number in range(accounts)]}],
            "incremental": {"cursor_field": "day", "start": (TODAY - datetime.timedelta(days=1)).isoformat()},
            "transform": {"mode": mode, "steps": [{"name": name, "select": "SELECT (record->>'id')::BIGINT AS id, "
                                                                           "window_start AS day FROM raw_%s" % name}]},
            "export": {name: {"step": name}}}


def test_the_state_is_written_after_every_window_without_a_copy_per_window(api, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = {"kind": "source", "name": "demo", "http": {"base_url": api.url},
              "streams": [windowed_items("pages", "page", 40), windowed_items("steps", "run", 40)]}
    copies = counted_copies(monkeypatch)
    output = StateHistory()
    runner = SourceRunner(source, {}, {}, output=output, today=TODAY)
    runner.run()
    yesterday = (TODAY - datetime.timedelta(days=1)).isoformat()
    # page mode: a state per window, each with one more bookmark; run mode: one state after the exports
    assert len(output.states) == 40 + 1
    assert [len(state["bookmarks"]["pages"]) for state in output.states[:40]] == list(range(1, 41))
    assert output.states[-1]["bookmarks"] == {
        "pages": dict(('{"account": "a%d"}' % number, yesterday) for number in range(40)),
        "steps": dict(('{"account": "a%d"}' % number, yesterday) for number in range(40))}
    # one copy: the state written last, kept for the run's metrics while run mode advances bookmarks unwritten
    assert len(copies) == 1
    assert runner.metrics.state == output.states[-1]


def test_the_run_summary_keeps_the_state_written_last(api, monkeypatch):
    api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
    source = {"kind": "source", "name": "demo", "http": {"base_url": api.url},
              "streams": [windowed_items("pages", "page", 3), windowed_items("steps", "run", 3)]}
    output = StateHistory(fail_on="steps")
    runner = SourceRunner(source, {}, {}, output=output, today=TODAY)
    with pytest.raises(OSError):
        runner.run()
    # the run-mode stream's bookmarks were never written: the metrics keep the page-mode stream's last state
    assert runner.metrics.state == output.states[-1] and list(output.states[-1]["bookmarks"]) == ["pages"]
    assert list(runner.state["bookmarks"]) == ["pages", "steps"]
