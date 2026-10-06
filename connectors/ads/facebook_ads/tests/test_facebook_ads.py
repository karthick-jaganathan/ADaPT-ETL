import datetime
import json
import os

import pytest
import requests

pytest.importorskip("facebook_business")
pytest.importorskip("adapt.connectors.facebook_ads.connector")

from facebook_business.session import FacebookSession  # noqa: E402

from adapt.connectors.facebook_ads.connector import FacebookAdsConnector  # noqa: E402
from adapt.core import cli
from adapt.core.runtime import components  # noqa: E402
from adapt.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from adapt.core.config.loader import load_source  # noqa: E402
from adapt.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "ads", "facebook_ads")  # a source folder
TODAY = datetime.date(2026, 10, 3)
SECRETS = {"access_token": "token-123", "app_secret": "app-secret-1"}
INSIGHTS = "/v26.0/act_123/insights"
INSIGHTS_STREAM = ["campaign_insights"]


@pytest.fixture
def graph(api, monkeypatch):
    """The real SDK, talking to the local fake API instead of graph.facebook.com."""
    monkeypatch.setattr(FacebookSession, "GRAPH", api.url)
    return api


def graph_error(code, message, status=400, transient=False, headers=None):
    return status, {"error": {"message": message, "type": "OAuthException", "code": code, "is_transient": transient,
                              "fbtrace_id": "trace-1"}}, headers or {}


def two_pages(api):
    def insights(request):
        day = json.loads(request["params"]["time_range"])["since"]
        if request["params"].get("after") == "c2":
            return 200, {"data": [{"account_id": "123", "campaign_id": "2", "campaign_name": "Generic",
                                   "date_start": day, "impressions": "0", "spend": "0"}],
                         "paging": {"cursors": {"before": "c2", "after": "c3"}}}
        return 200, {"data": [{"account_id": "123", "campaign_id": "1", "campaign_name": "Brand",
                               "date_start": day, "impressions": "1000", "clicks": "40", "spend": "12.50"}],
                     "paging": {"cursors": {"before": "c0", "after": "c2"}, "next": api.url + INSIGHTS + "?after=c2"}}
    return insights


def run(source, sleeps=None, streams=INSIGHTS_STREAM):
    output = MemoryOutput()
    SourceRunner(source, {"account_ids": ["123"], "start_date": TODAY}, dict(SECRETS), output=output, today=TODAY,
                 sleep=sleeps.append if sleeps is not None else (lambda seconds: None)).run(streams)
    return output


def metadata_routes(graph):
    """The account's campaigns and ad sets (with targeting), for the example's metadata streams."""
    graph.routes[("GET", "/v26.0/act_123/campaigns")] = lambda request: (200, {"data": [
        {"id": "1", "account_id": "123", "name": "Brand", "status": "ACTIVE", "effective_status": "ACTIVE",
         "objective": "OUTCOME_SALES", "buying_type": "AUCTION", "daily_budget": "5000",
         "start_time": "2026-09-18T09:07:20+0530", "created_time": "2026-09-18T09:07:17+0530"}]})
    graph.routes[("GET", "/v26.0/act_123/adsets")] = lambda request: (200, {"data": [
        {"id": "11", "account_id": "123", "campaign_id": "1", "name": "Brand - US", "status": "PAUSED",
         "daily_budget": "100", "lifetime_budget": "0",
         "targeting": {"age_min": 18, "age_max": 65, "geo_locations": {"countries": ["US", "CA"]},
                       "custom_audiences": [{"id": "6001", "name": "Buyers"}]}}]})


def test_the_facebook_ads_example_runs_end_to_end(graph, capsys, caplog, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv("ADAPT_SECRET_" + name.upper(), value)
    today = datetime.date.today().isoformat()
    graph.routes[("GET", INSIGHTS)] = two_pages(graph)
    metadata_routes(graph)
    assert cli.main(["run", EXAMPLE, "--set", "account_ids=123", "--set", "start_date=today",
                     "--log-level", "DEBUG"]) == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    records = dict((name, [m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == name])
                   for name in ("ad_sets", "campaign_insights", "campaigns"))
    schemas = dict((m["stream"], m["schema"]["properties"]) for m in messages if m["type"] == "SCHEMA")
    # typed columns keep nested data as one object or list (dlt loads them as JSON instead of child tables)
    assert (schemas["ad_sets"]["targeting"], schemas["ad_sets"]["countries"]) == (
        {"type": ["null", "object"]}, {"type": ["null", "array"]})
    assert records["campaign_insights"] == [
        {"account_id": "123", "campaign_id": "1", "campaign_name": "Brand", "date": today, "impressions": 1000,
         "clicks": 40, "spend": 12.5, "cpc": 0.3125},
        {"account_id": "123", "campaign_id": "2", "campaign_name": "Generic", "date": today, "impressions": 0,
         "clicks": 0, "spend": 0.0, "cpc": None}]
    assert records["campaigns"] == [
        {"account_id": "123", "campaign_id": "1", "campaign_name": "Brand", "status": "ACTIVE",
         "effective_status": "ACTIVE", "objective": "OUTCOME_SALES", "buying_type": "AUCTION", "bid_strategy": None,
         "daily_budget": 5000, "lifetime_budget": None, "start_time": "2026-09-18T03:37:20+00:00", "stop_time": None,
         "created_time": "2026-09-18T03:37:17+00:00", "updated_time": None}]  # the API's +0530 times, in UTC
    ad_set = records["ad_sets"][0]
    assert (ad_set["ad_set_id"], ad_set["campaign_id"], ad_set["daily_budget"], ad_set["countries"], ad_set["age_min"],
            ad_set["custom_audiences"], ad_set["excluded_custom_audiences"]) == (
        "11", "1", 100, ["US", "CA"], 18, [{"id": "6001", "name": "Buyers"}], None)
    assert ad_set["targeting"]["geo_locations"] == {"countries": ["US", "CA"]} and len(records["ad_sets"]) == 1
    # each stream writes its export; the incremental stream's state follows each window's records
    assert [(m["type"], m.get("stream")) for m in messages] == [
        ("SCHEMA", "ad_sets"), ("RECORD", "ad_sets"), ("SCHEMA", "campaign_insights"),
        ("RECORD", "campaign_insights"), ("RECORD", "campaign_insights"), ("STATE", None),
        ("SCHEMA", "campaigns"), ("RECORD", "campaigns")]
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    assert next(m["value"] for m in messages if m["type"] == "STATE") == {
        "bookmarks": {"campaign_insights": {'{"account_id": "123"}': yesterday}}}  # the last complete day
    campaigns = graph.calls("/v26.0/act_123/campaigns")[0]["params"]
    assert campaigns["fields"].startswith("id,account_id,name,status") and campaigns["limit"] == "500"
    assert graph.calls("/v26.0/act_123/adsets")[0]["params"]["limit"] == "100"
    first = graph.calls(INSIGHTS)[0]["params"]
    assert (first["fields"], first["level"], first["time_increment"], first["limit"]) == (
        "account_id,campaign_id,campaign_name,date_start,impressions,clicks,spend", "campaign", "1", "500")
    assert json.loads(first["time_range"]) == {"since": today, "until": today}
    assert first["access_token"] == "token-123" and first["appsecret_proof"]
    assert graph.calls(INSIGHTS)[1]["params"]["after"] == "c2" and len(graph.requests) == 4
    assert "token-123" not in caplog.text and first["appsecret_proof"] not in caplog.text


def test_throttling_waits_as_asked_and_pages_are_retried_alone(graph):
    pages, calls = two_pages(graph), {None: 0, "c2": 0}

    def insights(request):
        page = request["params"].get("after")
        calls[page] += 1
        if page is None and calls[page] == 1:  # throttled: back in 2 minutes
            return graph_error(17, "(#17) User request limit reached", headers={
                "x-business-use-case-usage": json.dumps({"123": [{"type": "ads_insights", "call_count": 100,
                                                                  "estimated_time_to_regain_access": 2}]})})
        if page == "c2" and calls[page] == 1:
            return graph_error(2, "Service temporarily unavailable", status=500, transient=True)
        return pages(request)
    graph.routes[("GET", INSIGHTS)] = insights
    sleeps = []
    output = run(load_source(EXAMPLE), sleeps=sleeps)
    assert [r["campaign_id"] for _, r in output.records] == ["1", "2"]  # page 2 was retried alone: no duplicates
    assert sleeps == [120, 1] and [r["params"].get("after") for r in graph.requests] == [None, None, "c2", "c2"]


def test_connections_dropped_mid_page_are_retried(graph, monkeypatch):
    graph.routes[("GET", INSIGHTS)] = two_pages(graph)
    original, drops = requests.Session.request, [requests.exceptions.ChunkedEncodingError("Connection broken")]

    def request(session, *args, **kwargs):
        if drops and "after" in (kwargs.get("params") or {}):  # the second page breaks once
            raise drops.pop(0)
        return original(session, *args, **kwargs)
    monkeypatch.setattr(requests.Session, "request", request)
    sleeps = []
    output = run(load_source(EXAMPLE), sleeps=sleeps)
    assert [r["campaign_id"] for _, r in output.records] == ["1", "2"] and sleeps == [1]


def test_other_errors_fail_with_the_api_message(graph):
    graph.routes[("GET", INSIGHTS)] = lambda request: graph_error(100, "(#100) token-123 is not a valid field")
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE))
    text = str(error.value)
    assert "(#100) *** is not a valid field (code 100, HTTP 400, fbtrace_id trace-1)" in text
    assert len(graph.requests) == 1


def test_objects_are_read_with_api_get(graph):
    graph.routes[("GET", "/v26.0/c1/")] = lambda request: (200, {"id": "c1", "name": "Brand", "status": "ACTIVE"})
    source = load_source(EXAMPLE)
    source["streams"] = [page_stream("campaign", {
        "name": "raw_campaign", "sdk": "facebook_ads", "service": "Campaign", "method": "api_get",
        "arguments": {"id": "c1", "fields": ["id", "name", "status"]}},
        "SELECT record->>'id' AS id, record->>'status' AS status FROM raw_campaign")]
    assert [r for _, r in run(source, streams=None).records] == [{"id": "c1", "status": "ACTIVE"}]
    assert graph.requests[0]["params"]["fields"] == "id,name,status"


def test_only_reads_are_allowed_and_versions_are_checked():
    assert components.check_source(load_source(EXAMPLE)) == []
    connector = FacebookAdsConnector()
    assert connector.check_request({"service": "AdAccount", "method": "create_campaign",
                                    "arguments": {"id": "act_1"}}) \
        == ["facebook_ads: AdAccount.create_campaign is not a read (supported: api_get and get_* edges)"]
    assert connector.check_request({"service": "AdAccount", "method": "get_insights_async",
                                    "arguments": {"id": "act_1"}})[0].endswith("is not a read (supported: api_get and "
                                                                             "get_* edges)")
    assert connector.check_request({"service": "Page", "method": "api_get", "arguments": {"id": "1"}})[0].startswith(
        "facebook_ads: service 'Page' is not supported")
    assert connector.check_request({"service": "AdAccount", "method": "get_nothing", "arguments": {"limit": 1}}) == [
        "facebook_ads: AdAccount.get_nothing needs `id` (e.g. act_<account id> for an ad account)",
        "facebook_ads: AdAccount.get_nothing does not take `limit` (arguments: id, fields, params)",
        "facebook_ads: AdAccount has no method 'get_nothing'"]
    source = load_source(EXAMPLE)
    source["auth"]["api_version"] = "26"
    with pytest.raises(SourceError, match="api_version '26' is not a Graph API version such as v26.0"):
        run(source)


@pytest.mark.parametrize("sdk_logs", [False, True])
def test_network_logs_show_requests_redacted(graph, capsys, monkeypatch, sdk_logs):
    for name, value in SECRETS.items():
        monkeypatch.setenv("ADAPT_SECRET_" + name.upper(), value)
    graph.routes[("GET", INSIGHTS)] = two_pages(graph)
    # the SDK's logger as `adapt connectors` lists it: urllib3.connectionpool
    logging_options = ["--log", "adapt.network=INFO"] if not sdk_logs else [
        "--log", "adapt.network=DEBUG", "--log", "urllib3.connectionpool=DEBUG"]
    assert cli.main(["run", EXAMPLE, "--stream", "campaign_insights", "--set", "account_ids=123", "--set",
                     "start_date=today"] + logging_options) == 0
    err = capsys.readouterr().err
    today = datetime.date.today().isoformat()
    # two API pages, one page of records: the SDK pages the edge, adapt counts the calls
    assert "INFO adapt.network: stream 'campaign_insights', request 'raw_campaign_insights', partition " \
           "{\"account_id\": \"123\"}, window %s..%s: facebook_ads AdAccount.get_insights page 1: 2 record(s), " % (
               today, today) in err
    assert "2 request(s) (raw_campaign_insights: 2)" in err
    proof = graph.calls(INSIGHTS)[0]["params"]["appsecret_proof"]
    if sdk_logs:
        assert 'DEBUG urllib3.connectionpool: %s:%d "GET %s?access_token=***&appsecret_proof=***' % (
            "http://127.0.0.1", int(graph.url.rsplit(":", 1)[1]), INSIGHTS) in err
        assert "DEBUG adapt.network: GET %s%s?access_token=***&appsecret_proof=***" % (graph.url, INSIGHTS) in err
        assert 'response body: {"data": [{"account_id": "123", "campaign_id": "1"' in err
    else:
        assert "urllib3" not in err and "response body" not in err
    assert "token-123" not in err and "app-secret-1" not in err and proof not in err
