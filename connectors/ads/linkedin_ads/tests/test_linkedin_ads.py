# /*************************************************************************
# * Copyright 2026 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

import datetime
from urllib.parse import parse_qs, urlparse
import pytest
import responses

from streamwright.core.engine.runner import SourceRunner, SourceError
from streamwright.core.config.loader import load_source
from streamwright.core.runtime.testing import MemoryOutput
from streamwright.core.config.inputs import resolve_inputs


def _load_linkedin_source(stream_name=None):
    source = load_source("examples/sources/ads/linkedin_ads")
    if stream_name:
        source["streams"] = [s for s in source["streams"] if s["name"] == stream_name]
    return source


def _records(output, table_name):
    rows = []
    for item in output.records:
        if isinstance(item, tuple) and len(item) == 2:
            tbl, row = item
            if tbl == table_name:
                rows.append(row)
        elif isinstance(item, dict):
            rows.append(item)
    return rows


@responses.activate
def test_ad_accounts_list():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAccounts",
        json={
            "elements": [{
                "id": 123456,
                "name": "Test Ad Account",
                "status": "ACTIVE",
                "type": "BUSINESS",
                "currency": "USD",
                "reference": "urn:li:organization:789",
                "changeAuditStamps": {
                    "created": {"time": 1700000000000},
                    "lastModified": {"time": 1700000000000},
                },
            }],
            "paging": {"start": 0, "count": 100, "total": 1},
        },
        match=[responses.matchers.query_param_matcher({"q": "search", "start": "0", "count": "100"})],
        status=200,
    )

    source = _load_linkedin_source("ad_accounts")
    secrets = {"linkedin_access_token": "tok_test_123"}
    config = {"account_ids": ["123456"]}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call.request.headers["Authorization"] == "Bearer tok_test_123"
    assert call.request.headers["LinkedIn-Version"] == "202401"
    assert call.request.headers["X-Restli-Protocol-Version"] == "2.0.0"

    table = _records(output, "ad_accounts")
    assert len(table) == 1
    assert table[0]["account_id"] == 123456
    assert table[0]["account_name"] == "Test Ad Account"
    assert table[0]["currency_code"] == "USD"


@responses.activate
def test_campaigns_offset_pagination():
    # Page 1
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCampaigns",
        json={
            "elements": [{
                "id": 101,
                "name": "Campaign 1",
                "status": "ACTIVE",
                "type": "SPONSORED_UPDATES",
                "costType": "CPC",
                "campaignGroup": "urn:li:sponsoredCampaignGroup:999",
                "dailyBudget": {"amount": 100.0, "currencyCode": "USD"},
                "unitCost": {"amount": 2.5, "currencyCode": "USD"},
                "runSchedule": {"start": 1700000000000, "end": 1700086400000},
                "changeAuditStamps": {
                    "created": {"time": 1700000000000},
                    "lastModified": {"time": 1700000000000},
                },
            }],
            "paging": {"start": 0, "count": 100, "total": 101},
        },
        match=[responses.matchers.query_param_matcher({
            "q": "search",
            "search.account.values[0]": "urn:li:sponsoredAccount:123456",
            "start": "0",
            "count": "100",
        })],
        status=200,
    )
    # Page 2
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCampaigns",
        json={
            "elements": [{
                "id": 102,
                "name": "Campaign 2",
                "status": "ACTIVE",
                "type": "SPONSORED_UPDATES",
                "costType": "CPC",
                "campaignGroup": "urn:li:sponsoredCampaignGroup:999",
                "dailyBudget": {"amount": 200.0, "currencyCode": "USD"},
                "unitCost": {"amount": 3.0, "currencyCode": "USD"},
                "runSchedule": {"start": 1700000000000, "end": 1700086400000},
                "changeAuditStamps": {
                    "created": {"time": 1700000000000},
                    "lastModified": {"time": 1700000000000},
                },
            }],
            "paging": {"start": 1, "count": 100, "total": 2},
        },
        match=[responses.matchers.query_param_matcher({
            "q": "search",
            "search.account.values[0]": "urn:li:sponsoredAccount:123456",
            "start": "1",
            "count": "100",
        })],
        status=200,
    )

    source = _load_linkedin_source("campaigns")
    secrets = {"linkedin_access_token": "tok_test_123"}
    config = {"account_ids": ["123456"]}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    table = _records(output, "campaigns")
    assert len(table) == 2
    assert table[0]["campaign_id"] == 101
    assert table[1]["campaign_id"] == 102


@responses.activate
def test_custom_api_version_header():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAccounts",
        json={
            "elements": [{"id": 1, "name": "A"}],
            "paging": {"start": 0, "count": 100, "total": 1},
        },
        status=200,
    )

    source = _load_linkedin_source("ad_accounts")
    secrets = {"linkedin_access_token": "tok_test_123"}
    config = {"account_ids": ["1"], "api_version": "202501"}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["LinkedIn-Version"] == "202501"


@responses.activate
def test_campaign_performance_dotted_params():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAnalytics",
        json={
            "elements": [{
                "pivotValue": "urn:li:sponsoredCampaign:101",
                "dateRange": {"start": {"year": 2026, "month": 1, "day": 1}},
                "impressions": 1500,
                "clicks": 45,
                "costInUsd": 22.50,
                "externalWebsiteConversions": 3,
                "oneClickLeads": 1,
            }],
            "paging": {"start": 0, "count": 100, "total": 1},
        },
        match=[responses.matchers.query_param_matcher({
            "q": "analytics",
            "pivot": "CAMPAIGN",
            "timeGranularity": "DAILY",
            "accounts[0]": "urn:li:sponsoredAccount:123456",
            "dateRange.start.year": "2026",
            "dateRange.start.month": "1",
            "dateRange.start.day": "1",
            "dateRange.end.year": "2026",
            "dateRange.end.month": "1",
            "dateRange.end.day": "1",
            "start": "0",
            "count": "100",
        })],
        status=200,
    )

    source = _load_linkedin_source("campaign_performance")
    secrets = {"linkedin_access_token": "tok_test_123"}
    config = {"account_ids": ["123456"], "start_date": "2026-01-01"}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output, today=datetime.date(2026, 1, 1)).run()

    assert len(responses.calls) == 1
    table = _records(output, "campaign_performance")
    assert len(table) == 1
    row = table[0]
    assert row["campaign_id"] == 101
    assert row["impressions"] == 1500
    assert row["clicks"] == 45
    assert row["spend"] == 22.50
    assert row["cpc"] == 0.50
    assert row["conversions"] == 3
    assert row["leads"] == 1


@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAccounts",
        json={"message": "Too Many Requests", "serviceErrorCode": 101},
        headers={"Retry-After": "0"},
        status=429,
    )
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAccounts",
        json={
            "elements": [{"id": 1, "name": "Retried Account"}],
            "paging": {"start": 0, "count": 100, "total": 1},
        },
        status=200,
    )

    source = _load_linkedin_source("ad_accounts")
    secrets = {"linkedin_access_token": "tok_test_123"}
    config = {"account_ids": ["1"]}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    table = _records(output, "ad_accounts")
    assert len(table) == 1
    assert table[0]["account_name"] == "Retried Account"


def test_missing_auth():
    source = _load_linkedin_source("ad_accounts")
    with pytest.raises(SourceError, match="token"):
        _runner(source, {"account_ids": ["1"]}, {}, output=MemoryOutput()).run()


def _runner(source, config, secrets, **kwargs):
    """A SourceRunner with the config defaults applied, as `streamwright run` resolves its inputs."""
    spec = source.get("spec") or {}
    config = resolve_inputs(spec.get("config"), config, "config")
    return SourceRunner(source, config, secrets, **kwargs)
