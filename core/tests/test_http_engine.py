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

from streamwright.core.net.encoding import encode_params, flatten_dotted
from streamwright.core.net.http import HttpError
from streamwright.core.net.paginators import (
    PagePatch,
    apply_patch,
    paginate,
    paginator_for,
    parse_next_link,
    set_dotted_path,
)


def test_param_encoding_plain():
    params = {"key": "val", "num": 1}
    assert encode_params(params, "plain") == params
    assert encode_params(None, "plain") == {}


def test_param_encoding_dotted():
    nested = {
        "dateRange": {"start": {"year": 2026, "month": 1}, "end": {"year": 2026, "month": 12}},
        "accounts": ["urn:li:adAccount:123", "urn:li:adAccount:456"],
        "simple": "test",
    }
    encoded = encode_params(nested, "dotted")
    assert encoded == {
        "dateRange.start.year": 2026,
        "dateRange.start.month": 1,
        "dateRange.end.year": 2026,
        "dateRange.end.month": 12,
        "accounts[0]": "urn:li:adAccount:123",
        "accounts[1]": "urn:li:adAccount:456",
        "simple": "test",
    }


def test_set_dotted_path_and_apply_patch():
    target = {"pagination": {"pageSize": 100}}
    set_dotted_path(target, "pagination.offset", 200)
    assert target == {"pagination": {"pageSize": 100, "offset": 200}}

    req = {"path": "/v1/items", "params": {"a": 1}, "json": {"pagination": {"pageSize": 100}}}
    patch = PagePatch(params={"b": 2}, body={"pagination.offset": 50}, url=None)
    applied = apply_patch(req, patch)
    assert applied["params"] == {"a": 1, "b": 2}
    assert applied["json"] == {"pagination": {"pageSize": 100, "offset": 50}}
    # Original request json unchanged
    assert req["json"] == {"pagination": {"pageSize": 100}}


def test_parse_next_link():
    assert parse_next_link(None) is None
    assert parse_next_link("") is None
    h1 = '<https://api.example.com/items?page=2>; rel="next", <https://api.example.com/items?page=5>; rel="last"'
    assert parse_next_link(h1) == "https://api.example.com/items?page=2"
    h2 = '<https://api.example.com/items?page=3>; rel=next'
    assert parse_next_link(h2) == "https://api.example.com/items?page=3"
    h3 = '<https://api.example.com/items?page=1>; rel="prev"'
    assert parse_next_link(h3) is None


def test_paginator_none():
    calls = []

    def send(patch):
        calls.append(patch)
        return {"data": [1, 2, 3]}

    pages = list(paginate(send, lambda r: {"type": "none"}, lambda r: r["data"]))
    assert len(pages) == 1
    assert pages[0][1] == [1, 2, 3]
    assert len(calls) == 1
    assert calls[0].params == {}


def test_paginator_offset_query_short_page_stops():
    responses = [
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": 3}]},  # short page (len 1 < page_size 2)
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    spec = {"type": "offset", "offset_param": "offset", "limit_param": "limit", "page_size": 2}
    pages = list(paginate(send, lambda r: spec, lambda r: r["items"]))
    assert len(pages) == 2
    assert len(calls) == 2
    assert calls[0].params == {"offset": 0, "limit": 2}
    assert calls[1].params == {"offset": 2, "limit": 2}


def test_paginator_offset_body_with_total_path():
    responses = [
        {"pagination": {"total": 3}, "elements": [{"id": 10}, {"id": 20}]},
        {"pagination": {"total": 3}, "elements": [{"id": 30}]},
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    spec = {
        "type": "offset",
        "in": "body",
        "offset_param": "pagination.offset",
        "limit_param": "pagination.limit",
        "page_size": 2,
        "total_path": "pagination.total",
    }
    pages = list(paginate(send, lambda r: spec, lambda r: r["elements"]))
    assert len(pages) == 2
    assert len(calls) == 2
    assert calls[0].body == {"pagination.offset": 0, "pagination.limit": 2}
    assert calls[1].body == {"pagination.offset": 2, "pagination.limit": 2}


def test_paginator_page_number():
    responses = [
        {"data": [{"id": 1}, {"id": 2}], "meta": {"total_pages": 2}},
        {"data": [{"id": 3}], "meta": {"total_pages": 2}},
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    spec = {
        "type": "page_number",
        "page_param": "page",
        "size_param": "per_page",
        "page_size": 2,
        "total_pages_path": "meta.total_pages",
    }
    pages = list(paginate(send, lambda r: spec, lambda r: r["data"]))
    assert len(pages) == 2
    assert len(calls) == 2
    assert calls[0].params == {"page": 1, "per_page": 2}
    assert calls[1].params == {"page": 2, "per_page": 2}


def test_paginator_cursor_token():
    responses = [
        {"records": [{"id": 1}], "next_cursor": "cur_2", "has_more": True},
        {"records": [{"id": 2}], "next_cursor": "cur_3", "has_more": True},
        {"records": [{"id": 3}], "next_cursor": None, "has_more": False},
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    spec = {
        "type": "cursor",
        "token_path": "next_cursor",
        "param": "after",
        "has_more_path": "has_more",
    }
    pages = list(paginate(send, lambda r: spec, lambda r: r["records"]))
    assert len(pages) == 3
    assert len(calls) == 3
    assert calls[0].params == {}
    assert calls[1].params == {"after": "cur_2"}
    assert calls[2].params == {"after": "cur_3"}


def test_paginator_cursor_has_more_true_without_token_raises_error():
    responses = [
        {"records": [{"id": 1}], "next_cursor": None, "has_more": True},
    ]

    def send(patch):
        return responses.pop(0)

    spec = {
        "type": "cursor",
        "token_path": "next_cursor",
        "param": "after",
        "has_more_path": "has_more",
    }
    with pytest.raises(HttpError, match="says there are more pages but there is nothing at .next_cursor."):
        list(paginate(send, lambda r: spec, lambda r: r["records"]))


def test_paginator_cursor_repeated_token_raises_error():
    responses = [
        {"records": [{"id": 1}], "next_cursor": "loop_token"},
        {"records": [{"id": 2}], "next_cursor": "loop_token"},
    ]

    def send(patch):
        return responses.pop(0)

    spec = {"type": "cursor", "token_path": "next_cursor", "param": "after"}
    with pytest.raises(HttpError, match="the paginator is not advancing: 'loop_token' repeats"):
        list(paginate(send, lambda r: spec, lambda r: r["records"]))


def test_paginator_repeated_page_records_raises_error():
    responses = [
        {"records": [{"id": 1, "val": "same"}]},
        {"records": [{"id": 1, "val": "same"}]},
    ]

    def send(patch):
        return responses.pop(0)

    spec = {"type": "offset", "offset_param": "offset", "limit_param": "limit", "page_size": 1}
    with pytest.raises(HttpError, match="the paginator is not advancing: a page repeats the previous page"):
        list(paginate(send, lambda r: spec, lambda r: r["records"]))


def test_paginator_link_header():
    class MockResp:
        def __init__(self, items, link=None):
            self.items = items
            self.headers = {"Link": link} if link else {}

    responses = [
        MockResp([{"id": 1}], '<https://api.example.com/items?page=2>; rel="next"'),
        MockResp([{"id": 2}], None),
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    spec = {"type": "link_header"}
    pages = list(paginate(send, lambda r: spec, lambda r: r.items))
    assert len(pages) == 2
    assert len(calls) == 2
    assert calls[0].url is None
    assert calls[1].url == "https://api.example.com/items?page=2"


def test_runner_merge_headers_and_secrets_redaction(caplog):
    import logging
    import responses
    from streamwright.core.engine.runner import SourceRunner, SourceError
    from streamwright.core.runtime.testing import MemoryOutput, page_stream

    caplog.set_level(logging.DEBUG, logger="streamwright.network")

    source = {
        "name": "test_headers",
        "auth": {"type": "bearer", "token": "secret_bearer_token"},
        "http": {
            "base_url": "https://api.example.com",
            "headers": {
                "X-Api-Key": "{{ secrets.my_api_key }}",
                "X-Empty": "",
                "X-Case": "from_source",
            },
        },
        "streams": [
            page_stream(
                "s",
                {
                    "http": {
                        "path": "/data",
                        "headers": {
                            "x-case": "from_request",
                            "X-Omit": "",
                        },
                    }
                },
                "SELECT * FROM records",
            )
        ],
    }

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            "https://api.example.com/data",
            json=[{"id": 1}],
            status=200,
        )
        out = MemoryOutput()
        runner = SourceRunner(source, {}, {"my_api_key": "top_secret_xyz_123"}, output=out)
        runner.run()

        req = rsps.calls[0].request
        assert req.headers["Authorization"] == "Bearer secret_bearer_token"
        assert req.headers["X-Api-Key"] == "top_secret_xyz_123"
        assert "X-Empty" not in req.headers
        assert "X-Omit" not in req.headers
        assert req.headers["x-case"] == "from_request"

    # Assert secret values NEVER appear in any log output
    for record in caplog.records:
        msg = record.getMessage()
        assert "secret_bearer_token" not in msg
        assert "top_secret_xyz_123" not in msg


def test_runner_records_path_missing_raises_error():
    import responses
    from streamwright.core.engine.runner import SourceRunner, SourceError
    from streamwright.core.runtime.testing import MemoryOutput, page_stream

    source = {
        "name": "test_missing_path",
        "auth": {"type": "bearer", "token": "tok"},
        "http": {"base_url": "https://api.example.com"},
        "streams": [
            page_stream(
                "s",
                {
                    "http": {"path": "/items"},
                    "records": {"path": "wrong_key"},
                },
                "SELECT * FROM records",
            )
        ],
    }

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            "https://api.example.com/items",
            json={"valid_key": [1, 2, 3]},
            status=200,
        )
        out = MemoryOutput()
        runner = SourceRunner(source, {}, {}, output=out)
        with pytest.raises(SourceError, match="records path 'wrong_key' not found in the response"):
            runner.run()


def test_runner_empty_records_path_overrides_source_default():
    import responses
    from streamwright.core.engine.runner import SourceRunner
    from streamwright.core.runtime.testing import MemoryOutput, page_stream

    source = {
        "name": "test_empty_records_override",
        "auth": {"type": "bearer", "token": "tok"},
        "http": {
            "base_url": "https://api.example.com",
            "records": {"path": "data"},
        },
        "streams": [
            page_stream(
                "s",
                {
                    "http": {"path": "/single_item"},
                    "records": {},  # empty records overrides source default to mean whole response
                },
                "SELECT * FROM records",
            )
        ],
    }

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            "https://api.example.com/single_item",
            json={"id": 42, "name": "single"},
            status=200,
        )
        out = MemoryOutput()
        runner = SourceRunner(source, {}, {}, output=out)
        runner.run()
        assert len(out.records) == 1
        assert out.records[0][1]["record"] == {"id": 42, "name": "single"}


def test_runner_http_transport_provider_auth_and_rejects_sdk():
    import responses
    from streamwright.core.engine.runner import SourceRunner, SourceError
    from streamwright.core.runtime.components import Connector, register, unregister
    from streamwright.core.runtime.testing import MemoryOutput, page_stream

    class MockHttpConnector(Connector):
        name = "mock_http_conn"
        transport = "http"
        category = "advertising"
        summary = "Mock HTTP connector"

        def connect(self, auth, context):
            raise RuntimeError("connect() must NEVER be called for transport == http")

    register(MockHttpConnector())
    try:
        # Valid source with http requests
        source = {
            "name": "test_mock_http",
            "auth": {
                "provider": "mock_http_conn",
                "type": "bearer",
                "token": "tok_123",
            },
            "http": {"base_url": "https://api.example.com"},
            "streams": [
                page_stream(
                    "s",
                    {"http": {"path": "/endpoint"}},
                    "SELECT * FROM records",
                )
            ],
        }

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "https://api.example.com/endpoint",
                json=[{"id": 100}],
                status=200,
            )
            out = MemoryOutput()
            runner = SourceRunner(source, {}, {}, output=out)
            runner.run()
            assert rsps.calls[0].request.headers["Authorization"] == "Bearer tok_123"

        # Invalid source using sdk request with http-transport provider
        bad_source = {
            "name": "test_bad_sdk",
            "auth": {
                "provider": "mock_http_conn",
                "type": "bearer",
                "token": "tok_123",
            },
            "streams": [
                page_stream(
                    "s",
                    {"sdk": "mock_http_conn", "service": "something", "method": "list"},
                    "SELECT * FROM records",
                )
            ],
        }
        with pytest.raises(SourceError, match="mock_http_conn is a REST API connector: use an http request"):
            SourceRunner(bad_source, {}, {}).run()
    finally:
        unregister("mock_http_conn")



def test_paginator_unknown_type_raises_error():
    with pytest.raises(HttpError, match="unknown paginator type 'offest'"):
        paginator_for({"type": "offest"})


def test_paginator_cursor_next_url_has_more_without_url_raises_error():
    def send(patch):
        return {"data": [{"id": 1}], "has_more": True, "next": None}

    spec = {"type": "cursor", "next_url_path": "next", "has_more_path": "has_more"}
    with pytest.raises(HttpError, match="says there are more pages"):
        list(paginate(send, lambda r: spec, lambda r: r["data"]))


def test_paginator_settings_follow_the_latest_response_and_keep_position():
    responses = [
        {"items": [{"id": 1}, {"id": 2}], "size": 2},
        {"items": [{"id": 3}, {"id": 4}, {"id": 5}], "size": 3},
        {"items": [{"id": 6}], "size": 3},
    ]
    calls = []

    def send(patch):
        calls.append(patch)
        return responses.pop(0)

    def spec_for(response):
        size = response["size"] if response else 2
        return {"type": "offset", "offset_param": "offset", "limit_param": "limit", "page_size": size}

    pages = list(paginate(send, spec_for, lambda r: r["items"]))
    assert len(pages) == 3
    assert [c.params for c in calls] == [{"offset": 0, "limit": 2}, {"offset": 2, "limit": 2},
                                        {"offset": 5, "limit": 3}]


def _client(**kwargs):
    from streamwright.core.net.http import Authenticator, HttpClient, Redactor
    import requests

    session = requests.Session()
    redact = Redactor()
    return HttpClient(base_url="https://api.example.com", session=session, redact=redact,
                      authenticator=Authenticator({"type": "bearer", "token": "tok_123"}, session, redact),
                      headers={"X-Api-Key": "key_123"}, **kwargs)


def test_follow_rejects_next_links_on_another_origin():
    import responses

    client = _client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, "https://api.example.com/items", json={"data": []})
        client.request("GET", "/items")
    assert client.follow("/items?page=2") == "https://api.example.com/items?page=2"
    assert client.follow("https://api.example.com:443/items?page=2") == "https://api.example.com:443/items?page=2"
    for link in ("https://evil.example/steal", "//evil.example/steal", "http://api.example.com/items?page=2",
                 "https://api.example.com:8443/items"):
        with pytest.raises(HttpError, match="another origin"):
            client.follow(link)


def test_request_follows_same_origin_redirects_with_credentials():
    import responses

    client = _client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, "https://api.example.com/old", status=301,
                 headers={"Location": "/new?x=1"})
        rsps.add(responses.GET, "https://api.example.com/new?x=1", json={"ok": True})
        assert client.request("GET", "/old", params={"x": "1"}) == {"ok": True}
        followed = rsps.calls[1].request
        assert followed.url == "https://api.example.com/new?x=1"
        assert followed.headers["X-Api-Key"] == "key_123"
        assert followed.headers["Authorization"].startswith("Bearer ")


def test_request_refuses_cross_origin_redirects():
    import responses

    client = _client()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, "https://api.example.com/items", status=302,
                 headers={"Location": "https://evil.example/steal"})
        rsps.add(responses.GET, "https://evil.example/steal", json={"stolen": True})
        with pytest.raises(HttpError, match="another origin"):
            client.request("GET", "/items")
        assert [call.request.url for call in rsps.calls] == ["https://api.example.com/items"]


def test_runner_does_not_send_credentials_to_a_next_url_on_another_origin():
    import responses
    from streamwright.core.engine.runner import SourceRunner, SourceError
    from streamwright.core.runtime.testing import MemoryOutput, page_stream

    source = {
        "name": "next_url_origin",
        "auth": {"type": "bearer", "token": "{{ secrets.tok }}"},
        "http": {"base_url": "https://api.example.com", "headers": {"X-Key": "{{ secrets.key }}"},
                 "paginator": {"type": "cursor", "next_url_path": "next"}, "records": {"path": "data"}},
        "spec": {"secrets": {"tok": {"type": "string"}, "key": {"type": "string"}}},
        "streams": [page_stream("s", {"http": {"path": "/items"}}, "SELECT * FROM records")],
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, "https://api.example.com/items",
                 json={"data": [{"id": 1}], "next": "http://evil.example/steal"})
        rsps.add(responses.GET, "http://evil.example/steal", json={"data": [{"id": 2}]})
        with pytest.raises(SourceError, match="another origin"):
            SourceRunner(source, {}, {"tok": "TOKEN_X", "key": "HEADER_KEY_X"}, output=MemoryOutput()).run()
        assert [call.request.url for call in rsps.calls] == ["https://api.example.com/items"]
