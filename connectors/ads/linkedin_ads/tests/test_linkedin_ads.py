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
from streamwright.connectors.linkedin_ads.connector import LinkedInAdsConnector

def run(request, auth):
    auth = dict(auth, provider="linkedin_ads")
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
    connector = LinkedInAdsConnector()
    with pytest.raises(ConnectorError, match="auth 'access_token' is empty"):
        connector.connect({}, None)
    with pytest.raises(ConnectorError, match="auth 'access_token' is empty"):
        connector.connect({"access_token": "   "}, None)

def test_headers_and_version():
    class DummyContext:
        def secret(self, val):
            pass
        def redact(self, text):
            return text

    connector = LinkedInAdsConnector()
    # Default version
    session = connector.connect({"access_token": "tok_123"}, DummyContext())
    assert session.headers["Authorization"] == "Bearer tok_123"
    assert session.headers["LinkedIn-Version"] == "202401"
    assert session.headers["X-Restli-Protocol-Version"] == "2.0.0"

    # Custom version
    session_custom = connector.connect({"access_token": "tok_123", "api_version": "202501"}, DummyContext())
    assert session_custom.headers["LinkedIn-Version"] == "202501"

@responses.activate
def test_ad_accounts_list():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAccounts",
        json={
            "elements": [{"id": 123456, "name": "Test Ad Account", "currency": "USD"}],
            "paging": {"start": 0, "count": 100, "total": 1}
        },
        status=200
    )
    
    output = run({"service": "ad_accounts", "method": "list", "sdk": "linkedin_ads"}, {"access_token": "tok_test"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": 123456, "name": "Test Ad Account", "currency": "USD"}

@responses.activate
def test_campaigns_offset_pagination():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCampaigns",
        json={
            "elements": [{"id": 101, "name": "Campaign 1"}],
            "paging": {"start": 0, "count": 1, "total": 2}
        },
        match=[responses.matchers.query_param_matcher({"start": "0", "count": "1"})],
        status=200
    )
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCampaigns",
        json={
            "elements": [{"id": 102, "name": "Campaign 2"}],
            "paging": {"start": 1, "count": 1, "total": 2}
        },
        match=[responses.matchers.query_param_matcher({"start": "1", "count": "1"})],
        status=200
    )
    
    output = run({
        "service": "campaigns",
        "method": "list",
        "sdk": "linkedin_ads",
        "arguments": {"params": {"count": 1}}
    }, {"access_token": "tok_test"})
    
    assert len(output.records) == 2
    assert output.records[0][1]["record"] == {"id": 101, "name": "Campaign 1"}
    assert output.records[1][1]["record"] == {"id": 102, "name": "Campaign 2"}

@responses.activate
def test_get_campaign():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCampaigns/101",
        json={"id": 101, "name": "Single Campaign", "status": "ACTIVE"},
        status=200
    )
    
    output = run({
        "service": "campaigns",
        "method": "get",
        "sdk": "linkedin_ads",
        "arguments": {"id": "101"}
    }, {"access_token": "tok_test"})
    
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": 101, "name": "Single Campaign", "status": "ACTIVE"}

@responses.activate
def test_ad_analytics():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adAnalytics",
        json={
            "elements": [{"pivotValue": "urn:li:sponsoredCampaign:101", "impressions": 1500, "clicks": 45, "costInUsd": "22.50"}],
            "paging": {"start": 0, "count": 100, "total": 1}
        },
        status=200
    )
    
    output = run({
        "service": "ad_analytics",
        "method": "analytics",
        "sdk": "linkedin_ads",
        "arguments": {
            "params": {
                "q": "analytics",
                "pivot": "CAMPAIGN",
                "dateRange.start.day": 1,
                "dateRange.start.month": 10,
                "dateRange.start.year": 2026,
            }
        }
    }, {"access_token": "tok_test"})
    
    assert len(output.records) == 1
    assert output.records[0][1]["record"]["impressions"] == 1500
    assert output.records[0][1]["record"]["clicks"] == 45

@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCreatives",
        json={"message": "Too Many Requests", "serviceErrorCode": 101},
        headers={"Retry-After": "1"},
        status=429
    )
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/adCreatives",
        json={"elements": [{"id": 501, "name": "Creative 1"}]},
        status=200
    )
    
    output = run({"service": "creatives", "method": "list", "sdk": "linkedin_ads"}, {"access_token": "tok_test"})
    assert len(output.records) == 1
    assert output.records[0][1]["record"] == {"id": 501, "name": "Creative 1"}

def test_validation():
    connector = LinkedInAdsConnector()
    assert connector.check_request({"service": "unknown", "method": "list"}) != []
    assert connector.check_request({"service": "campaigns", "method": "unknown"}) != []
    assert connector.check_request({"service": "campaigns", "method": "get"}) != []
    assert connector.check_request({"service": "campaigns", "method": "get", "arguments": {"id": ""}}) != []
    assert connector.check_request({"service": "campaigns", "method": "get", "arguments": {"id": None}}) != []
    assert connector.check_request({"service": "campaigns", "method": "get", "arguments": {"id": "101"}}) == []
    assert connector.check_request({"service": "campaigns", "method": "analytics"}) != []
    assert connector.check_request({"service": "ad_analytics", "method": "analytics"}) == []
    assert connector.check_request({"service": "campaigns", "method": "list", "arguments": "not-a-dict"}) != []
    assert connector.check_request({"service": "campaigns", "method": "list", "arguments": {"params": "not-a-dict"}}) != []

def test_request_validation_error():
    connector = LinkedInAdsConnector()
    with pytest.raises(ConnectorError, match="service 'unknown' is not supported"):
        list(connector.request(None, {"service": "unknown", "method": "list"}, None))

def test_error_translation():
    import requests
    connector = LinkedInAdsConnector()
    
    # Connection & timeout errors
    conn_err = connector.error(requests.ConnectionError("network reset"))
    assert conn_err.retryable is True
    
    timeout_err = connector.error(requests.Timeout("gateway timeout"))
    assert timeout_err.retryable is True
    
    # 429 with Retry-After header
    resp_429 = requests.Response()
    resp_429.status_code = 429
    resp_429.headers["Retry-After"] = "60"
    resp_429._content = b'{"message": "Rate limit exceeded"}'
    err_429 = connector.error(requests.HTTPError("Rate limit", response=resp_429))
    assert err_429.code == 429
    assert err_429.retryable is True
    assert err_429.retry_after == 60
    assert "Rate limit exceeded" in str(err_429)
    
    # 500 server error
    resp_500 = requests.Response()
    resp_500.status_code = 500
    resp_500._content = b'{"message": "Internal error"}'
    err_500 = connector.error(requests.HTTPError("Server error", response=resp_500))
    assert err_500.code == 500
    assert err_500.retryable is True
    
    # 400 bad request (non-retryable)
    resp_400 = requests.Response()
    resp_400.status_code = 400
    resp_400._content = b'{"message": "Bad request"}'
    err_400 = connector.error(requests.HTTPError("Client error", response=resp_400))
    assert err_400.code == 400
    assert err_400.retryable is False
    
    # HTTPError with no response attached
    err_no_resp = connector.error(requests.HTTPError("Detached error"))
    assert err_no_resp.retryable is False
    
    # Non-requests exception
    assert connector.error(KeyError("unrelated")) is None
