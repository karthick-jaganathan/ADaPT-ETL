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

from streamwright.core.engine.runner import SourceRunner, SourceError
from streamwright.core.config.loader import load_source
from streamwright.core.runtime.testing import MemoryOutput
from streamwright.core.config.inputs import resolve_inputs


def _load_amazon_source(stream_name=None):
    source = load_source("examples/sources/ads/amazon_ads")
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
def test_oauth2_refresh_token_and_headers():
    responses.add(
        responses.POST,
        "https://api.amazon.com/auth/o2/token",
        json={"access_token": "access_tok_456", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/profiles",
        json=[
            {
                "profileId": 12345,
                "countryCode": "US",
                "currencyCode": "USD",
                "timezone": "America/Los_Angeles",
                "accountInfo": {
                    "marketplaceStringId": "ATVPDKIKX0DER",
                    "type": "seller",
                    "name": "Acme Store",
                },
            }
        ],
        match=[responses.matchers.query_param_matcher({"startIndex": "0", "count": "100"})],
        status=200,
    )

    source = _load_amazon_source("profiles")
    secrets = {
        "amazon_client_id": "client_123",
        "amazon_client_secret": "secret_123",
        "amazon_refresh_token": "refresh_123",
    }
    config = {"profile_id": "prof_999"}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    assert len(responses.calls) == 2
    token_call = responses.calls[0]
    assert token_call.request.url == "https://api.amazon.com/auth/o2/token"

    api_call = responses.calls[1]
    assert api_call.request.headers["Authorization"] == "Bearer access_tok_456"
    assert api_call.request.headers["Amazon-Advertising-API-ClientId"] == "client_123"
    assert api_call.request.headers["Amazon-Advertising-API-Scope"] == "prof_999"

    table = _records(output, "profiles")
    assert len(table) == 1
    assert table[0]["profile_id"] == "12345"
    assert table[0]["account_name"] == "Acme Store"


@responses.activate
def test_scope_header_omitted_when_profile_id_not_set():
    responses.add(
        responses.POST,
        "https://api.amazon.com/auth/o2/token",
        json={"access_token": "access_tok_456", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/profiles",
        json=[{"profileId": 1, "countryCode": "US"}],
        status=200,
    )

    source = _load_amazon_source("profiles")
    secrets = {
        "amazon_client_id": "client_123",
        "amazon_client_secret": "secret_123",
        "amazon_refresh_token": "refresh_123",
    }
    config = {}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    api_call = responses.calls[1]
    assert "Amazon-Advertising-API-Scope" not in api_call.request.headers


@responses.activate
def test_regional_endpoint():
    responses.add(
        responses.POST,
        "https://api.amazon.com/auth/o2/token",
        json={"access_token": "access_tok_456", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://advertising-api-eu.amazon.com/v2/profiles",
        json=[{"profileId": 2, "countryCode": "UK"}],
        status=200,
    )

    source = _load_amazon_source("profiles")
    secrets = {
        "amazon_client_id": "client_123",
        "amazon_client_secret": "secret_123",
        "amazon_refresh_token": "refresh_123",
    }
    config = {"api_url": "https://advertising-api-eu.amazon.com"}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    api_call = responses.calls[1]
    assert api_call.request.url.startswith("https://advertising-api-eu.amazon.com/v2/profiles")


@responses.activate
def test_sp_campaigns_offset_pagination():
    responses.add(
        responses.POST,
        "https://api.amazon.com/auth/o2/token",
        json={"access_token": "access_tok_456", "expires_in": 3600},
        status=200,
    )
    # Page 1: 100 items (full page)
    page1 = [{"campaignId": f"cmp_{i}", "name": f"Campaign {i}", "state": "enabled"} for i in range(100)]
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/sp/campaigns",
        json=page1,
        match=[responses.matchers.query_param_matcher({"startIndex": "0", "count": "100"})],
        status=200,
    )
    # Page 2: 1 item (< 100 items -> short page stops pagination)
    page2 = [{"campaignId": "cmp_100", "name": "Campaign 100", "state": "enabled"}]
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/sp/campaigns",
        json=page2,
        match=[responses.matchers.query_param_matcher({"startIndex": "100", "count": "100"})],
        status=200,
    )

    source = _load_amazon_source("sp_campaigns")
    secrets = {
        "amazon_client_id": "client_123",
        "amazon_client_secret": "secret_123",
        "amazon_refresh_token": "refresh_123",
    }
    config = {"profile_id": "prof_1"}
    output = MemoryOutput()

    _runner(source, config, secrets, output=output).run()

    # 1 token call + 2 API calls = 3 calls
    assert len(responses.calls) == 3
    table = _records(output, "sp_campaigns")
    assert len(table) == 101


@responses.activate
def test_rate_limit_retry():
    responses.add(
        responses.POST,
        "https://api.amazon.com/auth/o2/token",
        json={"access_token": "access_tok_456", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/profiles",
        json={"message": "Too Many Requests"},
        headers={"Retry-After": "0"},
        status=429,
    )
    responses.add(
        responses.GET,
        "https://advertising-api.amazon.com/v2/profiles",
        json=[{"profileId": 1, "countryCode": "US"}],
        status=200,
    )

    source = _load_amazon_source("profiles")
    secrets = {
        "amazon_client_id": "client_123",
        "amazon_client_secret": "secret_123",
        "amazon_refresh_token": "refresh_123",
    }
    output = MemoryOutput()

    _runner(source, {}, secrets, output=output).run()

    # 1 token call + 2 GET calls = 3 calls
    assert len(responses.calls) == 3
    table = _records(output, "profiles")
    assert len(table) == 1


def test_missing_auth():
    source = _load_amazon_source("profiles")
    with pytest.raises(SourceError, match="token"):
        _runner(source, {}, {}, output=MemoryOutput()).run()


def _runner(source, config, secrets, **kwargs):
    """A SourceRunner with the config defaults applied, as `streamwright run` resolves its inputs."""
    spec = source.get("spec") or {}
    config = resolve_inputs(spec.get("config"), config, "config")
    return SourceRunner(source, config, secrets, **kwargs)
