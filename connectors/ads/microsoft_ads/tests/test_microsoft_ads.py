import csv
import datetime
import http.client
import io
import json
import logging
import os
import re
import urllib.error
import zipfile

import pytest

pytest.importorskip("bingads")
pytest.importorskip("streamwright.connectors.microsoft_ads.connector")

import bingads.service_client as service_client  # noqa: E402
from bingads.authorization import OAuthTokens, _UriOAuthService  # noqa: E402
from bingads.exceptions import OAuthTokenRequestException  # noqa: E402
from suds.transport import Reply, TransportError  # noqa: E402
from suds.transport.http import HttpTransport  # noqa: E402

from streamwright.connectors.microsoft_ads.connector import MicrosoftAdsConnector  # noqa: E402
from streamwright.core import cli
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.config.loader import load_source  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "ads", "microsoft_ads")  # a source folder
TODAY = datetime.date(2026, 10, 3)
SECRETS = {"developer_token": "dev-token-1", "client_id": "client-1", "refresh_token": "refresh-1"}
REPORTING = "https://bingads.microsoft.com/Reporting/v13"
CAMPAIGNS = "https://bingads.microsoft.com/CampaignManagement/v13"
PERFORMANCE = ["campaign_performance"]
XSI = 'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'


# * ----------------------------------------
# * a fake SOAP endpoint behind the real SDK
# * ----------------------------------------

def unprefixed(xml):
    """The XML without namespace prefixes: suds picks them in no fixed order."""
    return re.sub(r'type="[\w-]+:', 'type="', re.sub(r"<(/?)[\w-]+:", r"<\1", xml))


def envelope(body):
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Header/><s:Body>%s</s:Body>'
            '</s:Envelope>' % body)


def fault(code, name, message):
    return envelope(
        '<s:Fault><faultcode>s:Server</faultcode><faultstring>Invalid client data.</faultstring><detail>'
        '<AdApiFaultDetail xmlns="https://adapi.microsoft.com" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
        '<TrackingId>tracking-9</TrackingId><Errors><AdApiError><Code>%d</Code><Detail i:nil="true"/>'
        '<ErrorCode>%s</ErrorCode><Message>%s</Message></AdApiError></Errors></AdApiFaultDetail></detail>'
        '</s:Fault>' % (code, name, message))


class FakeSoap(HttpTransport):
    """
    Answers operations with handlers[operation](xml) -> a response body (a `fault(...)` body is sent as HTTP 500), an
    (HTTP status, body) error without a SOAP fault, or an exception to raise (e.g. a connection failure).
    """

    def __init__(self, handlers=None, sent=None):
        HttpTransport.__init__(self)
        self.handlers = {} if handlers is None else handlers
        self.sent = [] if sent is None else sent

    def copy(self):
        """suds links a transport to one client, so every client gets its own (sharing handlers and calls)."""
        return FakeSoap(self.handlers, self.sent)

    def send(self, request):
        xml = request.message.decode("utf-8")
        operation = re.search(r":(\w+)Request>", xml).group(1)
        self.sent.append((operation, xml))
        body = self.handlers[operation](xml)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, tuple):
            status, text = body
            raise TransportError(http.client.responses[status], status, io.BytesIO(text.encode("utf-8")))
        if "<s:Fault>" in body:
            raise TransportError("Internal Server Error", 500, io.BytesIO(body.encode("utf-8")))
        return Reply(200, {"Content-Type": "text/xml; charset=utf-8"}, body.encode("utf-8"))


@pytest.fixture
def soap(monkeypatch):
    fake = FakeSoap()
    fake.token_requests = []
    original = service_client.Client
    monkeypatch.setattr(service_client, "Client",
                        lambda url, **options: original(url, transport=fake.copy(), **options))

    def get_access_token(**request):
        fake.token_requests.append(request)
        return OAuthTokens(access_token="access-1", access_token_expires_in_seconds=3600, refresh_token="refresh-2")
    monkeypatch.setattr(_UriOAuthService, "get_access_token", staticmethod(get_access_token))
    return fake


def submitted(job):
    return lambda xml: envelope('<SubmitGenerateReportResponse xmlns="%s"><ReportRequestId>%s</ReportRequestId>'
                                '</SubmitGenerateReportResponse>' % (REPORTING, job(xml)))


def status(value, url=None):
    link = "<ReportDownloadUrl>%s</ReportDownloadUrl>" % url.replace("&", "&amp;") if url else \
        '<ReportDownloadUrl i:nil="true" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"/>'
    return envelope('<PollGenerateReportResponse xmlns="%s"><ReportRequestStatus>%s<Status>%s</Status>'
                    '</ReportRequestStatus></PollGenerateReportResponse>' % (REPORTING, link, value))


def report(rows):
    buffer = io.BytesIO()
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(["TimePeriod", "AccountId", "CampaignId", "CampaignName", "Impressions", "Clicks", "Spend"])
    writer.writerows(rows)
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.csv", text.getvalue().encode("utf-8-sig"))
    return buffer.getvalue()


def run(source, config=None, sleeps=None, streams=PERFORMANCE):
    output = MemoryOutput()
    config = dict({"account_ids": ["123456"], "customer_id": "555", "start_date": TODAY}, **(config or {}))
    SourceRunner(source, config, dict(SECRETS), output=output, today=TODAY,
                 sleep=sleeps.append if sleeps is not None else (lambda seconds: None)).run(streams)
    return output


def stream_named(source, name):
    return next(stream for stream in source["streams"] if stream["name"] == name)


def sent(soap, operation, element):
    """{the value of `element` in each `operation` request: its CustomerAccountId header}."""
    found = {}
    for name, xml in soap.sent:
        if name == operation:
            xml = unprefixed(xml)
            found[re.search(r"<%s>([^<]*)<" % element, xml).group(1)] = re.search(
                r"<CustomerAccountId>(\d+)<", xml).group(1)
    return found


def response(operation, body):
    return envelope('<%sResponse xmlns="%s" %s>%s</%sResponse>' % (operation, CAMPAIGNS, XSI, body, operation))


def account_entities(soap):
    """Two accounts: 123456 with campaigns 11 (ad groups 111, 112) and 12 (none), and 789 with 21 (ad group 211)."""
    campaigns = {"123456": [("11", "Brand", "Search"), ("12", "Shopping", "Shopping")],
                 "789": [("21", "Brand", "Search")]}
    ad_groups = {"11": ["111", "112"], "21": ["211"]}

    def get_campaigns(xml):
        account = re.search(r"<AccountId>(\d+)<", unprefixed(xml)).group(1)
        return response("GetCampaignsByAccountId", "<Campaigns>%s</Campaigns>" % "".join(
            "<Campaign><BudgetType>DailyBudgetStandard</BudgetType><DailyBudget>25</DailyBudget><Id>%s</Id>"
            "<Name>%s</Name><Status>Active</Status><TimeZone>PacificTimeUSCanadaTijuana</TimeZone>"
            "<CampaignType>%s</CampaignType></Campaign>" % row for row in campaigns[account]))

    def get_ad_groups(xml):
        campaign = re.search(r"<CampaignId>(\d+)<", unprefixed(xml)).group(1)
        return response("GetAdGroupsByCampaignId", "<AdGroups>%s</AdGroups>" % "".join(
            "<AdGroup><CpcBid><Amount>0.5</Amount></CpcBid><Id>%s</Id><Language i:nil=\"true\"/><Name>Group %s</Name>"
            "<Network>OwnedAndOperatedAndSyndicatedSearch</Network><Status>Active</Status>"
            "<AdGroupType>SearchStandard</AdGroupType></AdGroup>" % (group, group)
            for group in ad_groups.get(campaign, [])))

    def get_keywords(xml):
        group = re.search(r"<AdGroupId>(\d+)<", unprefixed(xml)).group(1)
        return response("GetKeywordsByAdGroupId", "<Keywords>%s</Keywords>" % (
            "" if group == "112" else "<Keyword><Bid><Amount>0.75</Amount></Bid><EditorialStatus>Active"
            "</EditorialStatus><Id>%s1</Id><MatchType>Exact</MatchType><Status>Active</Status><Text>shoes %s</Text>"
            "</Keyword>" % (group, group)))

    def get_campaign_criteria(xml):
        campaign = re.search(r"<CampaignId>(\d+)<", unprefixed(xml)).group(1)
        kind = "Negative" if campaign == "21" else "Biddable"
        bid = "" if kind == "Negative" else \
            '<CriterionBid i:type="BidMultiplier"><Type>BidMultiplier</Type><Multiplier>10</Multiplier></CriterionBid>'
        return response("GetCampaignCriterionsByIds", (
            '<CampaignCriterions><CampaignCriterion i:type="%sCampaignCriterion"><CampaignId>%s</CampaignId>'
            '<Criterion i:type="LocationCriterion"><Type>LocationCriterion</Type><DisplayName>Canada</DisplayName>'
            '<LocationId>32</LocationId><LocationType>Country</LocationType></Criterion><Id>9%s</Id>'
            '<Status>Active</Status><Type>%sCampaignCriterion</Type>%s</CampaignCriterion></CampaignCriterions>'
            '<PartialErrors i:nil="true"/>' % (kind, campaign, campaign, kind, bid)) if campaign != "12" else
            "<CampaignCriterions/><PartialErrors/>")

    def get_ad_group_criteria(xml):
        group = re.search(r"<AdGroupId>(\d+)<", unprefixed(xml)).group(1)
        return response("GetAdGroupCriterionsByIds", (
            '<AdGroupCriterions><AdGroupCriterion i:type="BiddableAdGroupCriterion"><AdGroupId>%s</AdGroupId>'
            '<Criterion i:type="AudienceCriterion"><Type>AudienceCriterion</Type><AudienceId>777</AudienceId>'
            '<AudienceType>RemarketingList</AudienceType></Criterion><Id>8%s</Id><Status>Active</Status>'
            '<Type>BiddableAdGroupCriterion</Type></AdGroupCriterion></AdGroupCriterions>' % (group, group))
            if group == "111" else "<AdGroupCriterions/>")

    soap.handlers.update(GetCampaignsByAccountId=get_campaigns, GetAdGroupsByCampaignId=get_ad_groups,
                         GetKeywordsByAdGroupId=get_keywords, GetCampaignCriterionsByIds=get_campaign_criteria,
                         GetAdGroupCriterionsByIds=get_ad_group_criteria)


# * -----
# * tests
# * -----

def test_the_microsoft_ads_example_runs_end_to_end(soap, api, capsys, caplog, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv("STREAMWRIGHT_SECRET_" + name.upper(), value)
    today = datetime.date.today().isoformat()
    soap.handlers["SubmitGenerateReport"] = submitted(lambda xml: "job-" + re.search(r"<\w+:long>(\d+)<", xml).group(1))
    soap.handlers["PollGenerateReport"] = lambda xml: status("Success", "%s/reports/%s.zip?sv=1&sig=SAS-SECRET" % (
        api.url, re.search(r"ReportRequestId>([\w-]+)<", xml).group(1)))
    api.routes[("GET", "/reports/job-123456.zip")] = lambda request: (200, report([
        [today, "123456", "11", "Brand", "1000", "50", "12.5"], [today, "123456", "12", "Generic, US", "", "", ""]]))
    api.routes[("GET", "/reports/job-789.zip")] = lambda request: (200, report([]))
    account_entities(soap)

    assert cli.main(["run", EXAMPLE, "--set", "account_ids=123456,789", "--set", "customer_id=555",
                     "--set", "start_date=today", "--log-level", "DEBUG"]) == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    records = {}
    for message in messages:
        if message["type"] == "RECORD":
            records.setdefault(message["stream"], []).append(message["record"])
    assert records["campaign_performance"] == [
        {"account_id": "123456", "campaign_id": "11", "campaign_name": "Brand", "date": today, "impressions": 1000,
         "clicks": 50, "spend": 12.5},
        {"account_id": "123456", "campaign_id": "12", "campaign_name": "Generic, US", "date": today,
         "impressions": 0, "clicks": 0, "spend": 0}]
    assert [(r["account_id"], r["campaign_id"], r["campaign_type"], r["daily_budget"]) for r in records["campaigns"]] \
        == [("123456", "11", "Search", 25.0), ("123456", "12", "Shopping", 25.0), ("789", "21", "Search", 25.0)]
    assert records["ad_groups"][0] == {
        "account_id": "123456", "campaign_id": "11", "ad_group_id": "111", "ad_group_name": "Group 111",
        "status": "Active", "ad_group_type": "SearchStandard", "cpc_bid": 0.5, "language": None,
        "network": "OwnedAndOperatedAndSyndicatedSearch"}
    assert [(r["account_id"], r["campaign_id"], r["ad_group_id"]) for r in records["ad_groups"]] == [
        ("123456", "11", "111"), ("123456", "11", "112"), ("789", "21", "211")]
    assert records["keywords"] == [
        {"account_id": "123456", "campaign_id": "11", "ad_group_id": "111", "keyword_id": "1111",
         "keyword": "shoes 111", "match_type": "Exact", "status": "Active", "editorial_status": "Active", "bid": 0.75},
        {"account_id": "789", "campaign_id": "21", "ad_group_id": "211", "keyword_id": "2111",
         "keyword": "shoes 211", "match_type": "Exact", "status": "Active", "editorial_status": "Active", "bid": 0.75}]
    assert records["location_targets"] == [
        {"account_id": "123456", "campaign_id": "11", "criterion_id": "911", "location_id": "32",
         "location_name": "Canada", "location_type": "Country", "criterion_type": "BiddableCampaignCriterion",
         "negative": False, "bid_multiplier": 10.0, "status": "Active"},
        {"account_id": "789", "campaign_id": "21", "criterion_id": "921", "location_id": "32",
         "location_name": "Canada", "location_type": "Country", "criterion_type": "NegativeCampaignCriterion",
         "negative": True, "bid_multiplier": None, "status": "Active"}]
    assert records["audience_targets"] == [
        {"account_id": "123456", "campaign_id": "11", "ad_group_id": "111", "criterion_id": "8111",
         "audience_id": "777", "audience_type": "RemarketingList", "criterion_type": "BiddableAdGroupCriterion",
         "negative": False, "bid_multiplier": None, "status": "Active"}]
    assert [(r["account_id"], r["campaign_id"], r["ad_group_id"], r["campaign_name"]) for r in
            records["ad_group_tree"]] == [("123456", "11", "111", "Brand"), ("123456", "11", "112", "Brand"),
                                          ("789", "21", "211", "Brand")]
    # requests about a campaign or an ad group carry its account in the CustomerAccountId header
    assert sent(soap, "GetAdGroupsByCampaignId", "CampaignId") == {"11": "123456", "12": "123456", "21": "789"}
    assert sent(soap, "GetKeywordsByAdGroupId", "AdGroupId") == {"111": "123456", "112": "123456", "211": "789"}
    assert sent(soap, "GetCampaignCriterionsByIds", "CampaignId") == {"11": "123456", "12": "123456", "21": "789"}
    assert sent(soap, "GetAdGroupCriterionsByIds", "AdGroupId") == {"111": "123456", "112": "123456", "211": "789"}
    operations = [operation for operation, _ in soap.sent]
    assert operations.count("GetCampaignsByAccountId") == 4 and operations.count("GetAdGroupsByCampaignId") == 6
    xml = unprefixed(dict(soap.sent)["GetAdGroupCriterionsByIds"])
    assert "<CriterionType>RemarketingList InMarketAudience CustomAudience CustomerList CombinedList " \
           "SimilarRemarketingList ProductAudience ImpressionBasedRemarketingList CustomSegment</CriterionType>" in xml
    assert "<ReturnAdditionalFields>AdGroupType</ReturnAdditionalFields>" in \
        unprefixed(dict(soap.sent)["GetAdGroupsByCampaignId"])

    operation, xml = next((name, xml) for name, xml in soap.sent if name == "SubmitGenerateReport")
    for part in ('<ReportRequest xsi:type="CampaignPerformanceReportRequest">', "<Aggregation>Daily</Aggregation>",
                 "<Columns><CampaignPerformanceReportColumn>TimePeriod</CampaignPerformanceReportColumn>",
                 "<AccountIds><long>123456</long></AccountIds>", "<CustomerAccountId>123456</CustomerAccountId>",
                 "<CustomerId>555</CustomerId>", "<DeveloperToken>dev-token-1</DeveloperToken>",
                 "<CustomDateRangeStart><Day>%d</Day>" % datetime.date.today().day):
        assert part in unprefixed(xml)
    assert [name for name in operations if "Report" in name] == ["SubmitGenerateReport", "PollGenerateReport"] * 2
    assert soap.token_requests[0]["client_id"] == "client-1" and soap.token_requests[0]["tenant"] == "common"
    assert len(soap.token_requests) == 1  # one sign-in for every stream
    assert "SAS-SECRET" not in caplog.text and "access-1" not in caplog.text


def test_reports_are_polled_until_done(soap, api, caplog):
    caplog.set_level(logging.INFO, logger="streamwright.source")
    statuses = [status("Pending"), status("Pending"), status("Success", api.url + "/r.zip")]
    soap.handlers["SubmitGenerateReport"] = submitted(lambda xml: "job-1")
    soap.handlers["PollGenerateReport"] = lambda xml: statuses.pop(0)
    api.routes[("GET", "/r.zip")] = lambda request: (200, report([["2026-10-03", "123456", "1", "A", "1", "1", "1"]]))
    sleeps = []
    assert len(run(load_source(EXAMPLE), sleeps=sleeps).records) == 1 and sleeps == [15.0, 15.0]

    soap.handlers["PollGenerateReport"] = lambda xml: status("Success")  # no data: no file
    assert run(load_source(EXAMPLE)).records == []
    assert "which usually means no data from 2026-10-03 to 2026-10-03" in caplog.text

    soap.handlers["PollGenerateReport"] = lambda xml: status("Error")
    with pytest.raises(SourceError, match=r'the job failed: \{"ReportDownloadUrl": null, "Status": "Error"\}'):
        run(load_source(EXAMPLE))


def test_faults_are_mapped_and_rate_limits_retried(soap, api):
    faults = [fault(117, "CallRateExceeded", "You have exceeded the number of calls.")]
    soap.handlers["SubmitGenerateReport"] = lambda xml: faults.pop(0) if faults else submitted(lambda x: "job")(xml)
    soap.handlers["PollGenerateReport"] = lambda xml: status("Success")
    sleeps = []
    run(load_source(EXAMPLE), sleeps=sleeps)
    assert sleeps == [60] and [operation for operation, _ in soap.sent] == ["SubmitGenerateReport"] * 2 + [
        "PollGenerateReport"]

    soap.sent.clear()
    soap.handlers["SubmitGenerateReport"] = lambda xml: fault(105, "InvalidCredentials",
                                                              "Authentication failed for access-1.")
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE))
    text = str(error.value)
    assert "105 InvalidCredentials: Authentication failed for ***." in text and "tracking id tracking-9" in text
    assert len(soap.sent) == 1


def test_http_errors_and_connection_failures_are_retried(soap):
    replies = [(500, "<html><body>upstream error</body></html>"),
               urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))]
    soap.handlers["SubmitGenerateReport"] = lambda xml: replies.pop(0) if replies else submitted(lambda x: "job")(xml)
    soap.handlers["PollGenerateReport"] = lambda xml: status("Success")
    sleeps = []
    run(load_source(EXAMPLE), sleeps=sleeps)
    assert sleeps == [1, 2] and [operation for operation, _ in soap.sent].count("SubmitGenerateReport") == 3

    soap.handlers["SubmitGenerateReport"] = lambda xml: (400, "<html><body>bad request</body></html>")
    with pytest.raises(SourceError, match="microsoft_ads: HTTP 400: Bad Request"):
        run(load_source(EXAMPLE))


def test_operations_are_built_from_the_wsdl(soap):
    account_entities(soap)
    source = load_source(EXAMPLE)
    output = run(source, streams=["campaigns"])
    assert [r["campaign_id"] for _, r in output.records] == ["11", "12"]
    xml = unprefixed(soap.sent[0][1])
    assert "<CampaignType>Search Shopping DynamicSearchAds Audience PerformanceMax</CampaignType>" in xml and \
        "<CustomerAccountId>123456</CustomerAccountId>" in xml

    stream_named(source, "campaigns")["requests"][0]["arguments"]["Campaigntype"] = "Search"
    with pytest.raises(SourceError, match=r"GetCampaignsByAccountId has no field 'Campaigntype' \(fields: "
                                          r"AccountId, CampaignType, ReturnAdditionalFields\)"):
        run(source, streams=["campaigns"])
    source = load_source(EXAMPLE)
    stream_named(source, "campaign_performance")["requests"][0]["async_job"]["submit"]["arguments"]["ReportRequest"][
        "@type"] = "NoSuchReport"
    with pytest.raises(SourceError, match="unknown type 'NoSuchReport'"):
        run(source)


def test_headers_name_the_account_and_customer_of_a_request(soap):
    account_entities(soap)
    source = load_source(EXAMPLE)
    stream_named(source, "ad_groups")["requests"][0]["headers"]["CustomerId"] = "999"
    run(source, streams=["ad_groups"])
    get_campaigns, get_ad_groups = (unprefixed(xml) for _, xml in soap.sent[:2])
    assert "<CustomerId>555</CustomerId>" in get_campaigns and "<CustomerAccountId>123456<" in get_campaigns
    assert "<CustomerId>999</CustomerId>" in get_ad_groups and "<CustomerAccountId>123456<" in get_ad_groups
    stream_named(source, "ad_groups")["requests"][0]["headers"] = {"AccountId": "1"}
    assert components.check_source(source) == [
        "stream 'ad_groups': requests[0]: microsoft_ads: CampaignManagementService.GetAdGroupsByCampaignId does not "
        "take the header(s) AccountId (headers: CustomerAccountId, CustomerId)"]


def test_only_read_only_operations_are_allowed():
    assert components.check_source(load_source(EXAMPLE)) == []
    connector = MicrosoftAdsConnector()
    assert connector.check_request({"service": "CampaignManagementService", "method": "DeleteCampaigns"}) == [
        "microsoft_ads: CampaignManagementService.DeleteCampaigns is not a read-only operation (supported: Get*, "
        "Search*, Find*, Poll*, SubmitGenerateReport)"]
    assert connector.check_request({"service": "BulkService", "method": "GetBulkUploadUrl"}) == [
        "microsoft_ads: service 'BulkService' is not supported (supported: CampaignManagementService, "
        "ReportingService, CustomerManagementService, AdInsightService)"]


def test_web_apps_send_their_client_secret(soap):
    soap.handlers["SubmitGenerateReport"] = submitted(lambda xml: "job")
    soap.handlers["PollGenerateReport"] = lambda xml: status("Success")
    output = MemoryOutput()
    SourceRunner(load_source(EXAMPLE), {"account_ids": ["123456"], "customer_id": "555", "start_date": TODAY},
                 dict(SECRETS, client_secret="app-secret-9"), output=output, today=TODAY,
                 sleep=lambda s: None).run(PERFORMANCE)
    assert soap.token_requests[0]["client_secret"] == "app-secret-9"
    assert soap.token_requests[0]["client_id"] == "client-1"


def test_oauth_failures_stop_the_run(soap, monkeypatch):
    def rejected(**request):
        raise OAuthTokenRequestException("invalid_grant", "AADSTS70000: the grant refresh-1 is expired")
    monkeypatch.setattr(_UriOAuthService, "get_access_token", staticmethod(rejected))
    with pytest.raises(SourceError) as error:
        run(load_source(EXAMPLE))
    assert "cannot connect: microsoft_ads: the OAuth token refresh failed" in str(error.value)
    assert "invalid_grant" in str(error.value) and "refresh-1" not in str(error.value) and soap.sent == []


@pytest.mark.parametrize("sdk_logs", [False, True])
def test_network_logs_show_the_soap_messages_redacted(soap, api, capsys, monkeypatch, sdk_logs):
    for name, value in SECRETS.items():
        monkeypatch.setenv("STREAMWRIGHT_SECRET_" + name.upper(), value)
    today = datetime.date.today().isoformat()
    soap.handlers["SubmitGenerateReport"] = submitted(lambda xml: "job-1")
    soap.handlers["PollGenerateReport"] = lambda xml: status("Success", api.url + "/r.zip?sv=1&sig=SAS-SECRET")
    api.routes[("GET", "/r.zip")] = lambda request: (200, report([[today, "123456", "1", "A", "1", "1", "1"]]))
    # the SDK's loggers as `streamwright connectors` lists them: suds.client, suds.transport
    logging_options = ["--log", "streamwright.network=INFO"] if not sdk_logs else [
        "--log", "streamwright.network=DEBUG", "--log", "suds.client=DEBUG", "--log", "suds.transport=DEBUG"]
    assert cli.main(["run", EXAMPLE, "--stream", "campaign_performance", "--set", "account_ids=123456", "--set",
                     "customer_id=555", "--set", "start_date=today"] + logging_options) == 0
    err = capsys.readouterr().err
    where = "stream 'campaign_performance', request 'raw_campaign_performance', partition {\"account_id\": " \
            "\"123456\"}, window %s..%s: " % (today, today)
    assert where + "microsoft_ads ReportingService.SubmitGenerateReport: " in err
    assert where + "microsoft_ads ReportingService.PollGenerateReport: " in err
    assert "INFO streamwright.network: " + where + "GET %s/r.zip?sv=1&sig=***: 200, " % api.url in err
    if sdk_logs:
        assert "DEBUG suds.client: sending to (https://reporting.api.bingads.microsoft.com/" in err
        assert re.search(r"<\w+:DeveloperToken>\*\*\*</\w+:DeveloperToken>", err)
        assert re.search(r"<\w+:AuthenticationToken>\*\*\*</\w+:AuthenticationToken>", err)
        assert "sig=***" in err.split("PollGenerateReportResponse", 1)[1]  # the reply, before streamwright reads the URL
    else:
        assert "suds.client" not in err
    for value in list(SECRETS.values()) + ["access-1", "refresh-2", "SAS-SECRET"]:
        assert value not in err
