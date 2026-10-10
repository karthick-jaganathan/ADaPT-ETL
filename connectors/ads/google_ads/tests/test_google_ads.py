import contextlib
import datetime
import importlib
import json
import logging
import os
from concurrent import futures
from decimal import Decimal

import pytest

pytest.importorskip("google.ads.googleads")
pytest.importorskip("streamwright.connectors.google_ads.connector")

import grpc  # noqa: E402
import google.ads.googleads.oauth2 as oauth2  # noqa: E402
from google.ads.googleads.client import GoogleAdsClient  # noqa: E402
from google.ads.googleads.errors import GoogleAdsException  # noqa: E402
from google.auth.credentials import AnonymousCredentials  # noqa: E402
from google.auth.exceptions import RefreshError  # noqa: E402

from streamwright.connectors.google_ads.connector import GoogleAdsConnector, _remember_tokens, _rows  # noqa: E402
from streamwright.core import cli
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.config.loader import load_source  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

VERSION = "v25"  # the version in examples/sources/ads/google_ads/source.yaml
ads_types = importlib.import_module("google.ads.googleads.%s.services.types.google_ads_service" % VERSION)
error_types = importlib.import_module("google.ads.googleads.%s.errors.types.errors" % VERSION)
ads_transport = importlib.import_module(
    "google.ads.googleads.%s.services.services.google_ads_service.transports.grpc" % VERSION)
FAILURE_KEY = "google.ads.googleads.%s.errors.googleadsfailure-bin" % VERSION
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "ads", "google_ads")  # a source folder
TODAY = datetime.date(2026, 10, 3)
SECRETS = {"developer_token": "dev-token-1", "client_id": "client-1", "client_secret": "secret-1",
           "refresh_token": "refresh-1"}


# * -------------------------------
# * offline client and fake services
# * -------------------------------

class FakeCall(grpc.RpcError, grpc.Call):

    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status

    def details(self):
        return "details"

    def initial_metadata(self):
        return ()

    def trailing_metadata(self):
        return ()

    def is_active(self):
        return False

    def time_remaining(self):
        return None

    def cancel(self):
        return False

    def add_callback(self, callback):
        return False


def ads_exception(field, name, status="INVALID_ARGUMENT", message="the request failed", retry_seconds=0):
    error = error_types.GoogleAdsError(message=message)
    setattr(error.error_code, field, type(getattr(error.error_code, field))[name])
    if retry_seconds:
        error.details.quota_error_details.retry_delay = datetime.timedelta(seconds=retry_seconds)
    failure = error_types.GoogleAdsFailure(errors=[error])
    call = FakeCall(getattr(grpc.StatusCode, status))
    return GoogleAdsException(call, call, failure, "req-42")


def row(customer=1234567890, campaign=10, name="Brand", status="ENABLED", date="2026-10-03", impressions=None,
        clicks=None, cost_micros=None):
    result = ads_types.GoogleAdsRow()
    result.customer.id = customer
    result.campaign.id = campaign
    result.campaign.name = name
    result.campaign.status = type(result.campaign.status)[status]
    result.segments.date = date
    for name, value in (("impressions", impressions), ("clicks", clicks), ("cost_micros", cost_micros)):
        if value is not None:  # metrics the API does not return stay unset
            setattr(result.metrics, name, value)
    return result


class FakeAdsService(object):
    """GoogleAdsService: `respond(customer_id, query)` -> batches (lists of rows, or an exception to raise)."""

    def __init__(self):
        self.calls, self.failures, self.pages = [], [], {}
        self.respond = lambda customer_id, query: []

    def search_stream(self, customer_id=None, query=None):
        self.calls.append({"customer_id": customer_id, "query": query})
        failure = self.failures.pop(0) if self.failures else None
        batches = self.respond(customer_id, query)

        def stream():  # like the SDK, errors surface while iterating
            if failure is not None:
                raise failure
            for rows in batches:
                if isinstance(rows, Exception):
                    raise rows
                yield ads_types.SearchGoogleAdsStreamResponse(results=rows)
        return stream()

    def search(self, request):
        self.calls.append(dict(request))
        rows, token = self.pages[request.get("page_token", "")]
        return ads_types.SearchGoogleAdsResponse(results=rows, next_page_token=token)


class FakeCustomerService(object):

    def __init__(self):
        self.resource_names = []

    def list_accessible_customers(self):
        return type("Response", (), {"resource_names": list(self.resource_names)})()


class FakeGoogle(object):

    def __init__(self):
        self.services = {"GoogleAdsService": FakeAdsService(), "CustomerService": FakeCustomerService()}
        self.clients = []


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(oauth2, "get_credentials", lambda config: AnonymousCredentials())

    def get_service(client, name, version=VERSION, **options):
        fake.clients.append((client, name, version))
        return fake.services[name]
    monkeypatch.setattr(GoogleAdsClient, "get_service", get_service)
    return fake


class AdsServer(object):
    """
    A local gRPC GoogleAdsService behind the real client (credentials, interceptors and error conversion included):
    each SearchStream call takes the next reply, a list of row batches or (status, GoogleAdsFailure or None), and a
    batch may be (status, failure) to fail the stream after the batches before it. A Search call takes the next
    reply's rows: one page.
    """

    def __init__(self):
        self.replies, self.calls = [], []
        handler = grpc.method_handlers_generic_handler("google.ads.googleads.%s.services.GoogleAdsService" % VERSION, {
            "SearchStream": grpc.unary_stream_rpc_method_handler(
                self.search_stream, request_deserializer=ads_types.SearchGoogleAdsStreamRequest.deserialize,
                response_serializer=ads_types.SearchGoogleAdsStreamResponse.serialize),
            "Search": grpc.unary_unary_rpc_method_handler(
                self.search, request_deserializer=ads_types.SearchGoogleAdsRequest.deserialize,
                response_serializer=ads_types.SearchGoogleAdsResponse.serialize)})
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        self.server.add_generic_rpc_handlers((handler,))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()

    def search(self, request, context):
        self.calls.append((request.customer_id, request.query, dict(context.invocation_metadata())))
        return ads_types.SearchGoogleAdsResponse(results=self.replies.pop(0))

    def search_stream(self, request, context):
        self.calls.append((request.customer_id, request.query, dict(context.invocation_metadata())))
        reply = self.replies.pop(0)
        for batch in (reply if isinstance(reply, list) else [reply]):
            if isinstance(batch, tuple):
                status, failure = batch
                metadata = [("request-id", "req-7")]
                if failure is not None:
                    metadata.append((FAILURE_KEY, error_types.GoogleAdsFailure.serialize(failure)))
                context.set_trailing_metadata(metadata)
                context.abort(status, "the call failed")
            yield ads_types.SearchGoogleAdsStreamResponse(results=batch)


@pytest.fixture
def ads_server(monkeypatch):
    server = AdsServer()
    monkeypatch.setattr(oauth2, "get_credentials", lambda config: AnonymousCredentials())
    monkeypatch.setattr(ads_transport.GoogleAdsServiceGrpcTransport, "create_channel", classmethod(
        lambda cls, *args, **options: grpc.insecure_channel("127.0.0.1:%d" % server.port)))
    yield server
    server.server.stop(None)


def failure(field, name, message="the request failed", retry_seconds=0):
    error = error_types.GoogleAdsError(message=message)
    setattr(error.error_code, field, type(getattr(error.error_code, field))[name])
    if retry_seconds:
        error.details.quota_error_details.retry_delay = datetime.timedelta(seconds=retry_seconds)
    return error_types.GoogleAdsFailure(errors=[error])


def campaign_row(customer=1112223333, campaign=10):
    """A row of the campaigns stream's query, with the settings the legacy serializer mapped."""
    result = ads_types.GoogleAdsRow()
    customer_ = result.customer
    customer_.id, customer_.descriptive_name, customer_.currency_code = customer, "Acme", "USD"
    customer_.time_zone, customer_.auto_tagging_enabled = "America/New_York", True
    customer_.conversion_tracking_setting.conversion_tracking_id = 9
    item = result.campaign
    item.id, item.name, item.start_date_time = campaign, "Brand", "2025-01-01 00:00:00"
    item.status = type(item.status)["ENABLED"]
    item.advertising_channel_type = type(item.advertising_channel_type)["SEARCH"]
    item.bidding_strategy_type = type(item.bidding_strategy_type)["TARGET_CPA"]
    item.target_cpa.target_cpa_micros = 5000000
    result.campaign_budget.id, result.campaign_budget.amount_micros = 7, 1000000
    metrics = result.metrics
    metrics.impressions, metrics.clicks, metrics.cost_micros = 100, 5, 2000000
    metrics.conversions, metrics.conversions_value = 2.0, 10.0
    return result


def entity_row(customer, **criterion):
    """A row of the metadata streams: an ad group of campaign 10, with a keyword, location or audience criterion."""
    result = ads_types.GoogleAdsRow()
    result.customer.id, result.campaign.id = customer, 10
    group = result.ad_group
    group.id, group.name, group.cpc_bid_micros = 100, "Exact", 1500000
    group.status = type(group.status)["ENABLED"]
    group.type_ = type(group.type_)["SEARCH_STANDARD"]  # `type` in GAQL: the library renames it
    kind = criterion.get("kind")
    item = result.campaign_criterion if kind == "location" else result.ad_group_criterion
    if kind:
        item.criterion_id = criterion["id"]
        item.status = type(item.status)[criterion.get("status", "ENABLED")]
    if kind == "keyword":
        item.keyword.text, item.negative = "running shoes", True
        item.keyword.match_type = type(item.keyword.match_type)["EXACT"]
    elif kind == "location":
        item.location.geo_target_constant, item.bid_modifier = "geoTargetConstants/2840", 1.5
    elif kind == "audience":
        item.type_ = type(item.type_)["USER_LIST"]
        item.user_list.user_list = "customers/%d/userLists/9" % customer
    return result


def respond_by_resource(today, rows_for_performance):
    """Answers each stream's query: the FROM resource (and criterion type) picks the rows."""
    def respond(customer_id, query):
        customer = int(customer_id)
        if query.startswith("SELECT customer.id, campaign.id, campaign.name, campaign.status FROM campaign WHERE"):
            return [[campaign_row(customer=customer), campaign_row(customer=customer, campaign=11)]]  # (hierarchy)
        if " FROM campaign WHERE campaign.status" in query and "segments.date" not in query:
            return [[campaign_row(customer=customer)]]
        if "segments.date" in query:
            return rows_for_performance(customer, query)
        if " FROM ad_group WHERE" in query:
            return [[entity_row(customer)]]
        if " FROM campaign_criterion WHERE" in query:
            return [[entity_row(customer, kind="location", id=2840)]]
        if "ad_group_criterion.type = KEYWORD" in query:
            return [[entity_row(customer, kind="keyword", id=1000, status="PAUSED")]]
        if "ad_group_criterion.type IN (USER_LIST" in query:
            return [[entity_row(customer, kind="audience", id=3000)]]
        raise AssertionError("unexpected query: %s" % query)
    return respond


PERFORMANCE = ["campaign_performance"]


def stream_named(source, name):
    return next(stream for stream in source["streams"] if stream["name"] == name)


def run(source, config=None, streams=None, sleeps=None):
    output = MemoryOutput()
    config = dict({"customer_ids": ["111-222-3333"], "login_customer_id": None, "start_date": TODAY}, **(config or {}))
    SourceRunner(source, config, dict(SECRETS), output=output, today=TODAY,
                 sleep=sleeps.append if sleeps is not None else (lambda seconds: None)).run(streams)
    return output


# * -----
# * tests
# * -----

def test_the_google_ads_example_runs_end_to_end(google, capsys, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv("STREAMWRIGHT_SECRET_" + name.upper(), value)
    today = datetime.date.today()
    ads = google.services["GoogleAdsService"]

    def performance(customer, query):
        if "'%s'" % today.isoformat() not in query:
            return []
        micros = 12345678 if customer == 1112223333 else 1005000
        return [[row(customer=customer, date=today.isoformat(), impressions=1000, clicks=50, cost_micros=micros)],
                [row(customer=customer, campaign=11, name="Generic", status="PAUSED", date=today.isoformat())]]
    ads.respond = respond_by_resource(today, performance)
    assert cli.main(["run", EXAMPLE, "--set", "customer_ids=111-222-3333,4445556666",
                     "--set", "login_customer_id=987-654-3210"]) == 0
    lines = capsys.readouterr().out.splitlines()
    messages = [json.loads(line) for line in lines]

    day = datetime.timedelta(days=1)
    first = (today - 30 * day, today - 24 * day)
    campaign_calls = [call for call in ads.calls if call["query"].endswith(" FROM campaign WHERE campaign.status IN "
                                                                           "(ENABLED, PAUSED)")
                      and "customer.descriptive_name" in call["query"]]
    performance_calls = [call for call in ads.calls if "segments.date" in call["query"]]
    hierarchy_calls = [call for call in ads.calls if " FROM ad_group WHERE campaign.id IN " in call["query"]]
    assert len(campaign_calls) == 2 and len(performance_calls) == 10  # 2 customers; x 5 windows of 7 days
    # each metadata stream: one query per customer; ad_group_hierarchy: two (its campaigns, then their ad groups)
    assert len(ads.calls) == 2 * 5 + 10 + 2 * 2
    assert [(call["customer_id"], call["query"]) for call in hierarchy_calls] == [
        (customer, "SELECT customer.id, campaign.id, ad_group.id, ad_group.name, ad_group.status FROM ad_group "
                   "WHERE campaign.id IN (10, 11)") for customer in ("1112223333", "4445556666")]  # one batch each
    assert campaign_calls[0]["query"].endswith(" FROM campaign WHERE campaign.status IN (ENABLED, PAUSED)")
    assert performance_calls[0] == {"customer_id": "1112223333", "query": (
        "SELECT customer.id, campaign.id, campaign.name, campaign.status, segments.date, metrics.impressions, "
        "metrics.clicks, metrics.cost_micros FROM campaign WHERE segments.date BETWEEN '%s' AND '%s' AND "
        "campaign.status IN (ENABLED, PAUSED)" % (first[0], first[1]))}
    client, service, version = google.clients[0]
    assert (service, version, client.login_customer_id, client.developer_token) == (
        "GoogleAdsService", "v25", "9876543210", "dev-token-1")

    campaigns = [m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == "campaigns"]
    assert campaigns[0] == {  # what the legacy campaign serializer produced
        "customer_id": "1112223333", "customer_name": "Acme", "customer_currency": "USD",
        "customer_timezone": "America/New_York", "auto_tagging_enabled": True, "tracking_url_template": None,
        "conversion_tracking_id": "9", "google_global_site_tag": None, "campaign_id": "10", "campaign_name": "Brand",
        "status": "active", "advertising_channel_type": "SEARCH", "bidding_strategy_type": "TARGET_CPA",
        "start_date": "2025-01-01", "end_date": None, "budget_id": "7", "budget_amount": 1.0, "target_cpa": 5.0,
        "target_roas": None, "impressions": 100, "clicks": 5, "cost": 2.0, "conversions": 2.0,
        "conversion_value": 10.0, "ctr": 0.05, "cpc": 0.4, "cost_per_conversion": 1.0, "roas": 5.0}
    assert len(campaigns) == 2
    records = [m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == "campaign_performance"]
    assert records[:2] == [
        {"customer_id": "1112223333", "campaign_id": "10", "campaign_name": "Brand", "status": "active",
         "date": today.isoformat(), "impressions": 1000, "clicks": 50, "cost": 12.35, "ctr": 0.05},  # rounded to cents
        {"customer_id": "1112223333", "campaign_id": "11", "campaign_name": "Generic", "status": "paused",
         "date": today.isoformat(), "impressions": 0, "clicks": 0, "cost": 0, "ctr": None}]
    assert len(records) == 4
    assert records[2]["cost"] == 1.01  # money is exact decimal arithmetic: binary floating point rounds 1.005 to 1.0
    # money columns are DECIMAL: exact JSON numbers, without trailing zeros (a budget of 1.00 is written 1)
    exact = [json.loads(line, parse_float=Decimal) for line in lines]
    campaign = next(m["record"] for m in exact if m["type"] == "RECORD" and m["stream"] == "campaigns")
    assert [(campaign[name], type(campaign[name])) for name in ("budget_amount", "target_cpa", "cost")] == [
        (1, int), (5, int), (2, int)]
    assert [m["record"]["cost"] for m in exact if m["type"] == "RECORD" and m["stream"] == "campaign_performance"] \
        == [Decimal("12.35"), 0, Decimal("1.01"), 0]
    # the state follows each window (2 customers x 5 windows): each customer's bookmark is the last complete day read
    bookmarks = [m["value"]["bookmarks"]["campaign_performance"] for m in messages if m["type"] == "STATE"]
    ends = [(today - days * day).isoformat() for days in (24, 17, 10, 3, 1)]
    assert [b.get('{"customer_id": "111-222-3333"}') for b in bookmarks] == ends + ends[-1:] * 5
    assert [b.get('{"customer_id": "4445556666"}') for b in bookmarks] == [None] * 5 + ends

    def first_record(stream):
        return next(m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == stream)
    assert first_record("ad_groups") == {
        "customer_id": "1112223333", "campaign_id": "10", "ad_group_id": "100", "ad_group_name": "Exact",
        "status": "active", "ad_group_type": "SEARCH_STANDARD", "cpc_bid": 1.5}
    assert first_record("keywords") == {
        "customer_id": "1112223333", "campaign_id": "10", "ad_group_id": "100", "criterion_id": "1000",
        "keyword": "running shoes", "match_type": "EXACT", "negative": True, "status": "paused", "cpc_bid": None}
    assert first_record("location_targets") == {
        "customer_id": "1112223333", "campaign_id": "10", "criterion_id": "2840",
        "geo_target_constant": "geoTargetConstants/2840", "negative": False, "bid_modifier": 1.5, "status": "active"}
    assert first_record("audience_targets") == {
        "customer_id": "1112223333", "campaign_id": "10", "ad_group_id": "100", "criterion_id": "3000",
        "audience_type": "USER_LIST", "user_list": "customers/1112223333/userLists/9", "user_interest": None,
        "custom_audience": None, "combined_audience": None, "audience": None, "negative": False,
        "bid_modifier": None, "status": "active"}
    assert [m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == "ad_group_hierarchy"] == [
        {"customer_id": customer, "campaign_id": "10", "campaign_name": "Brand", "campaign_status": "active",
         "ad_group_id": "100", "ad_group_name": "Exact", "ad_group_status": "active"}
        for customer in ("1112223333", "4445556666")]
    keyword_query = next(call["query"] for call in ads.calls if "KEYWORD" in call["query"])
    assert keyword_query.endswith(" FROM ad_group_criterion WHERE ad_group_criterion.type = KEYWORD AND "
                                  "ad_group_criterion.status IN (ENABLED, PAUSED) AND ad_group.status IN "
                                  "(ENABLED, PAUSED)")


def test_rows_keep_the_gaql_names_of_fields_the_library_renames():
    """The Python library renames fields named like Python words (`type` -> `type_`); rows use the GAQL names."""
    policy = importlib.import_module("google.ads.googleads.%s.common.types.policy" % VERSION)
    result = ads_types.GoogleAdsRow()
    result.ad_group.id, result.ad_group.type_ = 5, type(result.ad_group.type_)["SEARCH_STANDARD"]
    entry = policy.PolicyTopicEntry(topic="TRADEMARKS")
    entry.type_ = type(entry.type_)["LIMITED"]
    result.ad_group_ad.policy_summary.policy_topic_entries.append(entry)  # inside a repeated message
    rows = _rows(ads_types.SearchGoogleAdsStreamResponse(results=[result]))
    assert rows == [{"ad_group": {"id": "5", "type": "SEARCH_STANDARD"}, "ad_group_ad": {
        "policy_summary": {"policy_topic_entries": [{"topic": "TRADEMARKS", "type": "LIMITED"}]}}}]


def test_quota_errors_are_retried_and_others_are_not(google):
    ads = google.services["GoogleAdsService"]
    ads.respond = lambda customer_id, query: [[row(clicks=3, impressions=4)]]
    ads.failures = [ads_exception("quota_error", "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED", retry_seconds=30)]
    sleeps = []
    output = run(load_source(EXAMPLE), streams=PERFORMANCE, sleeps=sleeps)
    assert sleeps == [30.0] and [r["clicks"] for _, r in output.records] == [3]

    ads.calls.clear()
    ads.failures = [ads_exception("authorization_error", "USER_PERMISSION_DENIED", "PERMISSION_DENIED",
                                  "User doesn't have permission to access customer refresh-1")]
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE), streams=PERFORMANCE)
    text = str(error.value)
    assert "USER_PERMISSION_DENIED" in text and "req-42" in text and "refresh-1" not in text and len(ads.calls) == 1

    ads.calls.clear()
    ads.respond = lambda customer_id, query: [[row()], ads_exception("internal_error", "INTERNAL_ERROR", "INTERNAL")]
    with pytest.raises(SourceError, match="INTERNAL_ERROR"):
        run(load_source(EXAMPLE), streams=PERFORMANCE)  # the stream broke after rows were read: not retried
    assert len(ads.calls) == 1


def test_errors_through_the_real_client_stack(ads_server):
    ads_server.replies = [
        (grpc.StatusCode.RESOURCE_EXHAUSTED, failure("quota_error", "RESOURCE_EXHAUSTED", retry_seconds=30)),
        (grpc.StatusCode.UNAVAILABLE, None),
        [[row(clicks=3, impressions=4)]]]
    sleeps = []
    output = run(load_source(EXAMPLE), streams=PERFORMANCE, sleeps=sleeps)
    assert [r["clicks"] for _, r in output.records] == [3]
    assert sleeps == [30.0, 2]  # the delay Google asked for, then the backoff
    customer_id, query, metadata = ads_server.calls[0]
    assert customer_id == "1112223333" and query.startswith("SELECT customer.id") and \
        metadata["developer-token"] == "dev-token-1"

    ads_server.calls.clear()
    ads_server.replies = [(grpc.StatusCode.INVALID_ARGUMENT, failure("query_error", "PROHIBITED_FIELD_IN_SELECT_CLAUSE",
                                                                     "refresh-1 is not allowed"))]
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE), streams=PERFORMANCE)
    text = str(error.value)
    assert "PROHIBITED_FIELD_IN_SELECT_CLAUSE: *** is not allowed (request id req-7)" in text
    assert len(ads_server.calls) == 1

    ads_server.calls.clear()  # a stream that breaks after a batch was read is not retried
    ads_server.replies = [[[row(clicks=1)], (grpc.StatusCode.INTERNAL, None)]]
    with pytest.raises(SourceError, match="google_ads: INTERNAL: the call failed"):
        run(load_source(EXAMPLE), streams=PERFORMANCE)
    assert len(ads_server.calls) == 1


def test_values_with_braces_are_escaped_not_rejected(google):
    ads = google.services["GoogleAdsService"]
    source = load_source(EXAMPLE)
    stream_named(source, "campaigns")["requests"][0]["arguments"]["query"]["gaql"]["where"].append(
        {"field": "campaign.name", "op": "=", "type": "string", "value": "{{ config.name }}"})
    run(source, config={"name": "Promo {{BF}} 'x'"}, streams=["campaigns"])
    assert ads.calls[0]["query"].endswith("AND campaign.name = 'Promo {{BF}} \\'x\\''")


def test_campaigns_can_be_filtered(google):
    ads = google.services["GoogleAdsService"]
    run(load_source(EXAMPLE), config={"campaign_ids": [1, 2], "channel_types": ["SEARCH", "PERFORMANCE_MAX"]},
        streams=["campaigns"])
    assert ads.calls[0]["query"].endswith(
        " FROM campaign WHERE campaign.status IN (ENABLED, PAUSED) AND campaign.id IN (1, 2) AND "
        "campaign.advertising_channel_type IN (SEARCH, PERFORMANCE_MAX)")


def test_search_pages_and_accessible_customers(google):
    google.services["CustomerService"].resource_names = ["customers/111", "customers/222"]
    google.services["GoogleAdsService"].pages = {"": ([row(campaign=1)], "page-2"), "page-2": ([row(campaign=2)], "")}
    source = load_source(EXAMPLE)
    source["streams"] = [
        page_stream("customers", {"name": "raw_customers", "sdk": "google_ads", "service": "CustomerService",
                                  "method": "list_accessible_customers"},
                    "SELECT record->>'customer_id' AS customer_id FROM raw_customers"),
        page_stream("campaigns", {"name": "raw_campaigns", "sdk": "google_ads", "service": "GoogleAdsService",
                                  "method": "search", "arguments": {
                                      "customer_id": "{{ partition.customer_id }}",
                                      "query": {"gaql": {"select": ["campaign.id"], "from": "campaign", "limit": 10}}}},
                    "SELECT (record->>'$.campaign.id')::BIGINT AS campaign_id, partition->>'customer_id' AS "
                    "customer_id FROM raw_campaigns",
                    partitions=[{"name": "customer_id", "from_stream": "customers", "field": "customer_id"}])]
    output = run(source, streams=["campaigns"])
    assert [(r["customer_id"], r["campaign_id"]) for stream, r in output.records if stream == "campaigns"] == [
        ("111", 1), ("111", 2), ("222", 1), ("222", 2)]
    assert google.services["GoogleAdsService"].calls[:2] == [
        {"customer_id": "111", "query": "SELECT campaign.id FROM campaign LIMIT 10"},
        {"customer_id": "111", "query": "SELECT campaign.id FROM campaign LIMIT 10", "page_token": "page-2"}]


def test_connect_problems(google, monkeypatch):
    source = load_source(EXAMPLE)
    source["auth"]["api_version"] = "v99"
    with pytest.raises(SourceError, match=r"api_version 'v99' is not supported .*\(supported: v25"):
        run(source)

    def refresh_fails(config):
        raise RefreshError("invalid_grant: Bad Request (refresh-1)")
    monkeypatch.setattr(oauth2, "get_credentials", refresh_fails)
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE))
    assert "cannot connect: google_ads: the OAuth token refresh failed" in str(error.value)
    assert "refresh-1" not in str(error.value)


def test_only_read_only_calls_with_built_queries_are_allowed():
    source = load_source(EXAMPLE)
    assert components.check_source(source) == []
    request = stream_named(source, "campaign_performance")["requests"][0]
    request["arguments"]["query"] = "SELECT campaign.id FROM campaign WHERE campaign.name = '{{ config.name }}'"
    request["arguments"]["page_size"] = 10
    assert components.check_source(source) == [
        "stream 'campaign_performance': requests[0]: google_ads: GoogleAdsService.search_stream does not take "
        "`page_size` (arguments: customer_id, query)",
        "stream 'campaign_performance': requests[0]: google_ads: write the query as `query: {gaql: {...}}` so values "
        "are escaped; references inside query text are not allowed"]
    connector = GoogleAdsConnector()
    assert connector.check_request({"service": "CampaignService", "method": "mutate_campaigns"}) == [
        "google_ads: service 'CampaignService' is not supported (supported: GoogleAdsService, CustomerService)"]
    assert connector.check_request({"service": "GoogleAdsService", "method": "mutate"}) == [
        "google_ads: GoogleAdsService.mutate is not supported (supported: search_stream, search)"]


def test_rows_are_plain_dicts_keyed_like_gaql_fields():
    response = ads_types.SearchGoogleAdsStreamResponse(results=[row(clicks=5, impressions=0, status="PAUSED")])
    assert _rows(response) == [{
        "customer": {"id": "1234567890"},
        "campaign": {"id": "10", "name": "Brand", "status": "PAUSED"},
        "segments": {"date": "2026-10-03"},
        "metrics": {"clicks": "5", "impressions": "0"}}]  # int64 as text; cost_micros was not returned


def performance_run(capsys, monkeypatch, *arguments, source=EXAMPLE):
    """streamwright run of the campaign_performance stream (today, one customer): (exit status, stderr)."""
    for name, value in SECRETS.items():
        monkeypatch.setenv("STREAMWRIGHT_SECRET_" + name.upper(), value)
    code = cli.main(["run", str(source), "--stream", "campaign_performance", "--set", "customer_ids=111-222-3333",
                     "--set", "start_date=today"] + list(arguments))
    return code, capsys.readouterr().err


@contextlib.contextmanager
def fresh_google_logger():
    """
    The `google` logger as a new process has it (google-api-core makes it stop propagating with the first client),
    without the handlers pytest adds to loggers that do not propagate; put back as it was afterwards.
    """
    from google.api_core import client_logging
    google_logger = logging.getLogger("google")
    saved = client_logging._LOGGING_INITIALIZED, google_logger.propagate, list(google_logger.handlers)
    client_logging._LOGGING_INITIALIZED, google_logger.propagate, google_logger.handlers = False, True, []
    try:
        yield google_logger
    finally:
        client_logging._LOGGING_INITIALIZED, google_logger.propagate, google_logger.handlers = saved


def test_network_logs_show_the_client_logs_redacted(ads_server, tmp_path, capsys, monkeypatch):
    """`--log google.ads.googleads.client=DEBUG` is the old basicConfig + logger + stderr handler snippet, redacted."""
    source = load_source(EXAMPLE)
    source["streams"] = [stream_named(source, "campaign_performance")]
    source["streams"][0]["requests"][0]["method"] = "search"  # (the client logs unary calls as they return)
    path = tmp_path / "source.yaml"
    path.write_text(json.dumps(source, default=str))
    ads_server.replies = [[row(clicks=3, impressions=4), row(campaign=11)]]
    with fresh_google_logger() as google_logger:
        code, err = performance_run(capsys, monkeypatch, "--log", "streamwright.network=DEBUG", "--log",
                                    "google.ads.googleads.client=DEBUG", source=path)
        assert code == 0 and not google_logger.propagate  # its lines reached streamwright's handler all the same
    method = "/google.ads.googleads.%s.services.GoogleAdsService/Search" % VERSION
    assert "DEBUG google.ads.googleads.client: Request\n-------\nMethod: %s\n" % method in err
    assert "INFO google.ads.googleads.client: Request made: ClientCustomerId: 1112223333" in err
    assert '"developer-token": "REDACTED"' in err or '"developer-token": "***"' in err
    (call,) = [line.split("streamwright.network: ", 1)[1] for line in err.splitlines() if "search page" in line]
    assert call.startswith("stream 'campaign_performance', request 'raw_campaign_performance', partition "
                           '{"customer_id": "111-222-3333"}, window ')
    assert ": google_ads GoogleAdsService.search page 1: 2 record(s), " in call
    assert all(secret not in err for secret in SECRETS.values())

    ads_server.replies = [[[row(clicks=3, impressions=4)], [row(campaign=11)]]]
    code, err = performance_run(capsys, monkeypatch, "--log", "streamwright.network=INFO")  # the SDK's logger: not named
    assert code == 0 and "google.ads.googleads.client" not in err
    calls = [line.split(": google_ads ")[1].rsplit(",", 2)[0] for line in err.splitlines() if "search_stream" in line]
    assert calls == ["GoogleAdsService.search_stream page 1: 1 record(s)",
                     "GoogleAdsService.search_stream page 2: 1 record(s)"]  # a page per streamed batch


def test_refreshed_access_tokens_are_masked():
    class Credentials(object):
        token = None

        def refresh(self, request):
            self.token = "ya29.access-%d" % (request + 1)
    credentials, context = Credentials(), ConnectorContext(GoogleAdsConnector(), Redactor())
    _remember_tokens(credentials, context)
    credentials.refresh(0)
    credentials.refresh(1)
    assert context.redact("ya29.access-1 ya29.access-2") == "*** ***"
    assert GoogleAdsConnector.spec.loggers == ("google.ads.googleads.client",)
