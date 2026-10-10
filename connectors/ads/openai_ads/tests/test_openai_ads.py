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

from streamwright.core.engine.runner import SourceRunner
from streamwright.core.config.loader import load_source
from streamwright.core.runtime.testing import MemoryOutput


def _load_openai_source(stream_name=None):
    source = load_source("examples/sources/ads/openai_ads")
    if stream_name:
        source["streams"] = [s for s in source["streams"] if s["name"] == stream_name]
    return source


def _records(output, export_name):
    return [rec for exp, rec in output.records if exp == export_name]


@responses.activate
def test_ad_accounts_list():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_accounts",
        json={
            "data": [{"id": "act_1", "name": "Test Account", "status": "ACTIVE",
                      "currency_code": "USD", "timezone": "UTC", "url": "https://example.com"}],
            "has_more": False,
        },
        status=200,
    )

    source = _load_openai_source("ad_accounts")
    secrets = {"openai_ads_key": "sk-test-123"}
    config = {"account_ids": ["act_1"]}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 1
    req = responses.calls[0].request
    assert req.method == "GET"
    parsed = urlparse(req.url)
    assert parsed.path == "/v1/ad_accounts"
    assert parse_qs(parsed.query) == {"limit": ["500"]}
    assert req.headers["Authorization"] == "Bearer sk-test-123"

    table = _records(output, "ad_accounts")
    assert len(table) == 1
    assert table[0]["account_id"] == "act_1"
    assert table[0]["account_name"] == "Test Account"


@responses.activate
def test_campaigns_pagination():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns",
        json={
            "data": [{
                "id": "cmp_1", "name": "Campaign 1", "status": "ACTIVE",
                "objective": "AWARENESS", "bidding_type": "CPM", "billing_event_type": "IMPRESSIONS",
                "budget": {"daily_spend_limit_micros": 1000000, "lifetime_spend_limit_micros": 5000000},
                "start_time": "1700000000", "end_time": "1700086400",
                "created_at": 1700000000, "updated_at": 1700000000,
            }],
            "has_more": True,
            "last_id": "cmp_1",
        },
        match=[responses.matchers.query_param_matcher({"ad_account_id": "act_1", "limit": "500"})],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns",
        json={
            "data": [{
                "id": "cmp_2", "name": "Campaign 2", "status": "ACTIVE",
                "objective": "CONVERSIONS", "bidding_type": "oCPM", "billing_event_type": "IMPRESSIONS",
                "budget": {"daily_spend_limit_micros": 2000000, "lifetime_spend_limit_micros": 10000000},
                "start_time": "1700000000", "end_time": "1700086400",
                "created_at": 1700000000, "updated_at": 1700000000,
            }],
            "has_more": False,
        },
        match=[responses.matchers.query_param_matcher({"ad_account_id": "act_1", "limit": "500", "after": "cmp_1"})],
        status=200,
    )

    source = _load_openai_source("campaigns")
    secrets = {"openai_ads_key": "sk-test-123"}
    config = {"account_ids": ["act_1"]}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    for call in responses.calls:
        assert call.request.headers["Authorization"] == "Bearer sk-test-123"

    table = _records(output, "campaigns")
    assert len(table) == 2
    assert table[0]["campaign_id"] == "cmp_1"
    assert table[1]["campaign_id"] == "cmp_2"


@responses.activate
def test_campaign_insights():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns",
        json={
            "data": [{"id": "cmp_1", "name": "Brand Campaign"}],
            "has_more": False,
        },
        match=[responses.matchers.query_param_matcher({"ad_account_id": "act_1", "limit": "500"})],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns/cmp_1/insights",
        json={
            "data": [{"date": "2026-01-01", "impressions": 100, "clicks": 10, "spend": 5.5}],
            "has_more": False,
        },
        match=[responses.matchers.query_param_matcher({"start_date": "2026-01-01", "end_date": "2026-01-01", "limit": "500"})],
        status=200,
    )

    source = _load_openai_source("campaign_performance")
    secrets = {"openai_ads_key": "sk-test-123"}
    config = {"account_ids": ["act_1"], "start_date": "2026-01-01"}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output, today=datetime.date(2026, 1, 1)).run()

    assert len(responses.calls) == 2
    assert responses.calls[1].request.path_url == "/v1/campaigns/cmp_1/insights?start_date=2026-01-01&end_date=2026-01-01&limit=500"

    table = _records(output, "campaign_performance")
    assert len(table) == 1
    row = table[0]
    assert row["campaign_id"] == "cmp_1"
    assert row["campaign_name"] == "Brand Campaign"
    assert row["impressions"] == 100
    assert row["clicks"] == 10
    assert row["spend"] == 5.5


@responses.activate
def test_single_entity_get():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_groups/ag_123",
        json={"id": "ag_123", "name": "Single Ad Group", "status": "ACTIVE"},
        status=200,
    )

    source = {
        "kind": "source",
        "name": "single_entity_test",
        "auth": {
            "provider": "openai_ads",
            "type": "bearer",
            "token": "{{ secrets.openai_ads_key }}",
        },
        "http": {
            "base_url": "https://api.ads.openai.com",
            "paginator": {"type": "cursor", "token_path": "last_id", "param": "after", "has_more_path": "has_more"},
            "records": {"path": "data"},
        },
        "streams": [{
            "name": "ad_group_single",
            "requests": [{
                "name": "raw_ad_group",
                "paginator": {"type": "none"},
                "records": {},
                "http": {
                    "path": "/v1/ad_groups/ag_123",
                    "method": "GET",
                },
            }],
            "transform": {
                "mode": "page",
                "steps": [{
                    "name": "ad_group",
                    "select": "SELECT record->>'id' AS id, record->>'name' AS name FROM raw_ad_group",
                }],
            },
            "export": {"ad_group": {"step": "ad_group", "primary_key": ["id"]}},
        }],
    }

    output = MemoryOutput()
    SourceRunner(source, {}, {"openai_ads_key": "sk-test-123"}, output=output).run()

    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == "https://api.ads.openai.com/v1/ad_groups/ag_123"
    table = _records(output, "ad_group")
    assert len(table) == 1
    assert table[0]["id"] == "ag_123"
    assert table[0]["name"] == "Single Ad Group"


@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_accounts",
        json={"error": {"message": "Rate limit exceeded"}},
        headers={"Retry-After": "0"},
        status=429,
    )
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_accounts",
        json={
            "data": [{"id": "act_1", "name": "Retried Account", "status": "ACTIVE"}],
            "has_more": False,
        },
        status=200,
    )

    source = _load_openai_source("ad_accounts")
    secrets = {"openai_ads_key": "sk-test-123"}
    config = {"account_ids": ["act_1"]}
    output = MemoryOutput()

    SourceRunner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    table = _records(output, "ad_accounts")
    assert len(table) == 1
    assert table[0]["account_name"] == "Retried Account"


def test_missing_auth():
    from streamwright.core.engine.runner import SourceError
    source = _load_openai_source("ad_accounts")
    with pytest.raises(SourceError, match="token"):
        SourceRunner(source, {"account_ids": ["act_1"]}, {}, output=MemoryOutput()).run()
