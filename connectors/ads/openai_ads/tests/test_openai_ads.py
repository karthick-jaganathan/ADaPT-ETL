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

import pytest
import responses
from streamwright.core.runtime.testing import MemoryOutput, page_stream
from streamwright.core.runtime.components import ConnectorError
from streamwright.core.engine.runner import SourceRunner
from streamwright.connectors.openai_ads.connector import OpenAIAdsConnector

def run(request, auth):
    auth = dict(auth, provider="openai_ads")
    source = {
        "kind": "source",
        "name": "test_source",
        "auth": auth,
        "streams": [page_stream("test_stream", request, "SELECT * FROM records")]
    }
    output = MemoryOutput()
    SourceRunner(source, {}, auth, output=output).run()
    return output

def test_missing_auth():
    connector = OpenAIAdsConnector()
    with pytest.raises(ConnectorError, match="auth 'advertiser_api_key' is empty"):
        connector.connect({}, None)
    with pytest.raises(ConnectorError, match="auth 'advertiser_api_key' is empty"):
        connector.connect({"advertiser_api_key": "   "}, None)

@responses.activate
def test_ad_accounts_list():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_accounts",
        json={
            "data": [{"id": "act_1", "name": "Test Account"}],
            "has_more": False
        },
        status=200
    )
    
    output = run({"service": "ad_accounts", "method": "list", "sdk": "openai_ads"}, {"advertiser_api_key": "sk-test-123"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": "act_1", "name": "Test Account"}

@responses.activate
def test_campaigns_pagination():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns",
        json={
            "data": [{"id": "cmp_1"}],
            "has_more": True,
            "last_id": "cmp_1"
        },
        match=[responses.matchers.query_param_matcher({})],
        status=200
    )
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns",
        json={
            "data": [{"id": "cmp_2"}],
            "has_more": False
        },
        match=[responses.matchers.query_param_matcher({"after": "cmp_1"})],
        status=200
    )
    
    output = run({"service": "campaigns", "method": "list", "sdk": "openai_ads"}, {"advertiser_api_key": "sk-test-123"})
    assert len(output.records) == 2
    assert output.records[0][1]["record"] == {"id": "cmp_1"}
    assert output.records[1][1]["record"] == {"id": "cmp_2"}

@responses.activate
def test_get_ad_group():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ad_groups/ag_123",
        json={"id": "ag_123", "name": "Test Ad Group"},
        status=200
    )
    
    output = run({
        "service": "ad_groups",
        "method": "get",
        "sdk": "openai_ads",
        "arguments": {"id": "ag_123"}
    }, {"advertiser_api_key": "sk-test-123"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": "ag_123", "name": "Test Ad Group"}

@responses.activate
def test_campaign_insights():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/campaigns/cmp_1/insights",
        json={
            "data": [{"campaign_id": "cmp_1", "impressions": 100, "clicks": 10, "spend": 5.5}],
            "has_more": False
        },
        status=200
    )
    
    output = run({
        "service": "campaigns",
        "method": "insights",
        "sdk": "openai_ads",
        "arguments": {"id": "cmp_1"}
    }, {"advertiser_api_key": "sk-test-123"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"campaign_id": "cmp_1", "impressions": 100, "clicks": 10, "spend": 5.5}

@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ads",
        json={"error": {"message": "Rate limit exceeded"}},
        headers={"Retry-After": "1"},
        status=429
    )
    responses.add(
        responses.GET,
        "https://api.ads.openai.com/v1/ads",
        json={"data": [{"id": "ad_1"}], "has_more": False},
        status=200
    )
    
    output = run({"service": "ads", "method": "list", "sdk": "openai_ads"}, {"advertiser_api_key": "sk-test-123"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": "ad_1"}

def test_validation():
    connector = OpenAIAdsConnector()
    assert connector.check_request({"service": "unknown", "method": "list"}) != []
    assert connector.check_request({"service": "ads", "method": "unknown"}) != []
    assert connector.check_request({"service": "ads", "method": "get"}) != []
    assert connector.check_request({"service": "ads", "method": "get", "arguments": {"id": "1"}}) == []
    assert connector.check_request({"service": "ads", "method": "insights"}) != []
    assert connector.check_request({"service": "ads", "method": "insights", "arguments": {"id": "1"}}) == []
    assert connector.check_request({"service": "ad_accounts", "method": "insights", "arguments": {"id": "1"}}) != []
    assert connector.check_request({"service": "ads", "method": "list", "arguments": "not-a-mapping"}) != []
    assert connector.check_request({"service": "ads", "method": "get", "arguments": {"id": ""}}) != []
    assert connector.check_request({"service": "ads", "method": "get", "arguments": {"id": None}}) != []
    assert connector.check_request({"service": "ads", "method": "list", "arguments": {"params": "not-a-dict"}}) != []


def test_request_validation_error():
    connector = OpenAIAdsConnector()
    with pytest.raises(ConnectorError, match="service 'unknown' is not supported"):
        list(connector.request(None, {"service": "unknown", "method": "list"}, None))

def test_error_translation():
    import requests
    connector = OpenAIAdsConnector()
    
    # Connection / timeout errors
    conn_err = connector.error(requests.ConnectionError("connection dropped"))
    assert conn_err.retryable is True
    
    timeout_err = connector.error(requests.Timeout("timed out"))
    assert timeout_err.retryable is True
    
    # 429 with Retry-After header
    resp_429 = requests.Response()
    resp_429.status_code = 429
    resp_429.headers["Retry-After"] = "45"
    resp_429._content = b'{"error": {"message": "Rate limit exceeded"}}'
    err_429 = connector.error(requests.HTTPError("Rate limited", response=resp_429))
    assert err_429.code == 429
    assert err_429.retryable is True
    assert err_429.retry_after == 45
    assert "Rate limit exceeded" in str(err_429)
    
    # 500 internal server error
    resp_500 = requests.Response()
    resp_500.status_code = 500
    resp_500._content = b'{"error": {"message": "Internal error"}}'
    err_500 = connector.error(requests.HTTPError("Server error", response=resp_500))
    assert err_500.code == 500
    assert err_500.retryable is True
    
    # 400 bad request (non-retryable)
    resp_400 = requests.Response()
    resp_400.status_code = 400
    resp_400._content = b'{"error": {"message": "Bad request"}}'
    err_400 = connector.error(requests.HTTPError("Client error", response=resp_400))
    assert err_400.code == 400
    assert err_400.retryable is False
    
    # HTTPError with no response object
    err_no_resp = connector.error(requests.HTTPError("No response"))
    assert err_no_resp.retryable is False
    
    # Non-requests exception
    assert connector.error(ValueError("unrelated error")) is None

