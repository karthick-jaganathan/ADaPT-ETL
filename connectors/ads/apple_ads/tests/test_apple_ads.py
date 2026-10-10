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

import json
import pytest
import responses

from streamwright.core.engine.runner import SourceRunner, SourceError
from streamwright.core.config.loader import load_source
from streamwright.core.runtime.testing import MemoryOutput


def _load_apple_source(stream_name=None):
    source = load_source("examples/sources/ads/apple_ads")
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
def test_campaigns_query():
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/campaigns/query",
        json={
            "result": [
                {
                    "id": 1001,
                    "adAccountId": 123456,
                    "name": "Apple Ads Campaign 1",
                    "status": "ENABLED",
                    "servingStatus": "RUNNING",
                    "dailyBudgetAmount": {"amount": "100.0", "currency": "USD"},
                    "startTime": "2026-01-01T00:00:00Z",
                    "endTime": "2026-12-31T23:59:59Z",
                },
                {
                    "id": 1002,
                    "adAccountId": 123456,
                    "name": "Apple Ads Campaign 2",
                    "status": "PAUSED",
                    "servingStatus": "NOT_RUNNING",
                    "dailyBudgetAmount": {"amount": "50.0", "currency": "USD"},
                    "startTime": "2026-01-01T00:00:00Z",
                    "endTime": "2026-12-31T23:59:59Z",
                },
            ],
            "pagination": {"totalCount": 2, "offset": 0, "pageSize": 100},
        },
        status=200,
    )

    source = _load_apple_source("campaigns")
    secrets = {"apple_ads_token": "token_apple_123"}
    config = {"ad_account_id": "123456"}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call.request.headers["Authorization"] == "Bearer token_apple_123"
    assert call.request.headers["X-AP-Context"] == "adAccountId=123456"

    body = json.loads(call.request.body)
    assert body["pagination"]["offset"] == 0
    assert body["pagination"]["limit"] == 100
    assert body["pagination"]["pageSize"] == 100
    assert body["pagination"]["fetchTotalCount"] is True

    table = _records(output, "campaigns")
    assert len(table) == 2
    assert table[0]["campaign_id"] == 1001
    assert table[0]["campaign_name"] == "Apple Ads Campaign 1"
    assert table[1]["campaign_id"] == 1002


@responses.activate
def test_pagination_in_body():
    # Page 1
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/campaigns/query",
        json={
            "result": [{
                "id": 1001,
                "adAccountId": 123456,
                "name": "Campaign 1",
                "status": "ENABLED",
                "servingStatus": "RUNNING",
            }],
            "pagination": {"totalCount": 2, "offset": 0, "pageSize": 100},
        },
        status=200,
    )
    # Page 2
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/campaigns/query",
        json={
            "result": [{
                "id": 1002,
                "adAccountId": 123456,
                "name": "Campaign 2",
                "status": "PAUSED",
                "servingStatus": "NOT_RUNNING",
            }],
            "pagination": {"totalCount": 2, "offset": 1, "pageSize": 100},
        },
        status=200,
    )

    source = _load_apple_source("campaigns")
    secrets = {"apple_ads_token": "token_apple_123"}
    config = {"ad_account_id": "123456"}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    body1 = json.loads(responses.calls[0].request.body)
    assert body1["pagination"]["offset"] == 0
    body2 = json.loads(responses.calls[1].request.body)
    assert body2["pagination"]["offset"] == 1

    table = _records(output, "campaigns")
    assert len(table) == 2


@responses.activate
def test_keywords_query_with_filters():
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/keywords/query",
        json={
            "result": [{
                "id": 3001,
                "adAccountId": 123456,
                "campaignId": 1001,
                "adGroupId": 2001,
                "text": "streaming data",
                "matchType": "EXACT",
                "status": "ACTIVE",
                "bidAmount": {"amount": "1.50", "currency": "USD"},
            }],
            "pagination": {"totalCount": 1, "offset": 0, "pageSize": 100},
        },
        status=200,
    )

    source = _load_apple_source("keywords")
    secrets = {"apple_ads_token": "token_apple_123"}
    config = {"ad_account_id": "123456", "campaign_ids": ["1001"]}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    body = json.loads(responses.calls[0].request.body)
    assert body["filters"] == [{"field": "campaignId", "operator": "EQUALS", "value": "1001"}]

    table = _records(output, "keywords")
    assert len(table) == 1
    assert table[0]["keyword_id"] == 3001
    assert table[0]["keyword_text"] == "streaming data"


@responses.activate
def test_campaign_reports_records_override():
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/reports/apps/campaigns/query",
        json={
            "result": {
                "rows": [
                    {
                        "metadata": {"id": 1001, "name": "Campaign 1"},
                        "totalMetrics": {
                            "impressions": 500,
                            "taps": 50,
                            "totalInstalls": 12,
                            "localSpend": {"amount": "25.00", "currency": "USD"},
                        },
                    }
                ]
            },
            "pagination": {"totalCount": 1, "offset": 0, "pageSize": 100},
        },
        status=200,
    )

    source = _load_apple_source("campaign_reports")
    secrets = {"apple_ads_token": "token_apple_123"}
    config = {"ad_account_id": "123456"}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    table = _records(output, "campaign_reports")
    assert len(table) == 1
    assert table[0]["campaign_id"] == 1001
    assert table[0]["impressions"] == 500
    assert table[0]["taps"] == 50
    assert table[0]["installs"] == 12
    assert table[0]["spend"] == 25.00


@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/campaigns/query",
        json={"error": {"errors": [{"message": "Rate limit exceeded"}]}},
        headers={"Retry-After": "0"},
        status=429,
    )
    responses.add(
        responses.POST,
        "https://api.ads.apple.com/v1/campaigns/query",
        json={
            "result": [{"id": 1001, "name": "Retried Campaign", "status": "ENABLED"}],
            "pagination": {"totalCount": 1, "offset": 0, "pageSize": 100},
        },
        status=200,
    )

    source = _load_apple_source("campaigns")
    secrets = {"apple_ads_token": "token_apple_123"}
    config = {"ad_account_id": "123456"}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    table = _records(output, "campaigns")
    assert len(table) == 1
    assert table[0]["campaign_name"] == "Retried Campaign"


def test_missing_auth():
    source = _load_apple_source("campaigns")
    with pytest.raises(SourceError, match="token"):
        SourceRunner(source, {"ad_account_id": "123456"}, {}, output=MemoryOutput()).run()
