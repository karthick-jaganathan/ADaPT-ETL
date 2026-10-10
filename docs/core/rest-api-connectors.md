---
layout: default
title: Authoring REST API Connectors
parent: Core
nav_order: 5
---

# Authoring REST API Connectors

StreamWright features a unified, declarative HTTP engine for REST API connectors. Rather than writing custom Python networking loops, pagination code, or retry mechanisms in connector packages, REST connectors are declarative: network endpoints, headers, query parameters, paginators, records paths, and authentication are expressed in `source.yaml` and stream YAML definitions.

Connector packages serve as light distribution vehicles (installable via `pip install streamwright-<network>-ads`) that expose package metadata and declare `transport = "http"`.

---

## 1. Anatomy of a REST Connector

A REST connector package has three minimal components:

1. **Python connector class**: subclassing `Connector` with `transport = "http"`.
2. **`pyproject.toml`**: declaring the package and entry point under `streamwright.connectors`.
3. **Example source directory**: demonstrating valid `source.yaml` and stream configurations.

### 1.1 The Connector Class

```python
# src/streamwright/connectors/my_network/connector.py
from streamwright.core.runtime.components import Connector

__all__ = ["MyNetworkConnector"]

class MyNetworkConnector(Connector):
    """
    MyNetwork Ads REST API connector metadata.
    Transport is handled declaratively by StreamWright's HTTP engine.
    """
    name = "my_network"
    transport = "http"
    category = "advertising"
    summary = "MyNetwork Ads"
```

Because `transport = "http"`, StreamWright's runner:
- Skips SDK `connect()` initialization.
- Rejects any requests declaring `sdk: my_network`.
- Directs all calls through `HttpClient` and `Authenticator`.

### 1.2 `pyproject.toml` Entry Point

```toml
[project]
name = "streamwright-my-network"
version = "0.1.0"
dependencies = [
    "streamwright~=0.1.0",
]

[project.entry-points."streamwright.connectors"]
my_network = "streamwright.connectors.my_network.connector:MyNetworkConnector"
```

Notice that `requests` is **not** a dependency of the connector package itself—networking is handled by StreamWright core.

---

## 2. Source Configuration (`source.yaml`)

The `source.yaml` file defines the shared authentication, base URL, default headers, parameters encoding, and default paginator.

```yaml
kind: source
name: my_network_demo
description: MyNetwork Ads marketing metadata and reporting streams.

spec:
  config:
    account_ids: {type: list, items: string, description: "Target ad account IDs"}
    api_version: {type: string, default: "v1", description: "API version string"}
  secrets:
    api_key: {type: string}

auth:
  provider: my_network
  type: bearer
  token: "{{ secrets.api_key }}"

http:
  base_url: https://api.mynetwork.com
  headers:
    X-API-Version: "{{ config.api_version }}"
  params_encoding: plain
  paginator:
    type: cursor
    token_path: pagination.next_cursor
    param: cursor
    has_more_path: pagination.has_more
  records:
    path: data
  retry:
    codes: [429, 500, 502, 503, 504]
    max_attempts: 5
    backoff: exponential
    max_delay: 2m
```

### 2.1 Supported Auth Types

When `provider: <name>` has `transport: "http"`, `type` defines the authentication mechanism:
- `bearer`: `token: "{{ secrets.my_token }}"` (sent in `Authorization: Bearer <token>`).
- `api_key`: `name: "X-API-Key"`, `value: "{{ secrets.api_key }}"`, optional `in: header | query`.
- `basic`: `username: "{{ secrets.username }}"`, `password: "{{ secrets.password }}"`.
- `oauth2_refresh_token`:
  ```yaml
  type: oauth2_refresh_token
  token_url: https://auth.mynetwork.com/oauth/token
  client_id: "{{ secrets.client_id }}"
  client_secret: "{{ secrets.client_secret }}"
  refresh_token: "{{ secrets.refresh_token }}"
  ```

---

## 3. Defining Streams

Streams in `streams/<stream_name>.yaml` define declarative HTTP requests:

```yaml
# streams/campaigns.yaml
description: Campaigns metadata.

partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}

requests:
  - name: raw_campaigns
    http:
      path: /v1/campaigns
      method: GET
      params:
        account_id: "{{ partition.account_id }}"
        limit: 100

transform:
  mode: page
  steps:
    - name: campaigns
      select: |
        SELECT partition->>'account_id'       AS account_id,
               record->>'id'                   AS campaign_id,
               record->>'name'                 AS campaign_name,
               record->>'status'               AS status
        FROM raw_campaigns

export:
  campaigns:
    step: campaigns
    primary_key: [account_id, campaign_id]
```

---

## 4. Paginators Reference

Paginators can be defined at the source level (in `http.paginator`) or per-request (under `requests[].paginator` or `requests[].http.paginator`). Paginators support `in: query` (default) or `in: body` (for POST queries).

### 4.1 `offset`
Paginates via numeric offset / start index.
```yaml
paginator:
  type: offset
  offset_param: offset       # e.g., offset, startIndex, start
  limit_param: limit         # e.g., limit, count
  page_size: 100
  total_path: total_count    # optional: stops when offset >= total
  in: query                  # or "body"
```

### 4.2 `cursor`
Paginates via opaque continuation token.
```yaml
paginator:
  type: cursor
  token_path: paging.next_token   # path in response JSON to extract token
  param: next_token               # query or body parameter name
  has_more_path: paging.has_more  # optional boolean field path
  in: query                       # or "body"
```

### 4.3 `page_number`
Paginates via incrementing 1-based or 0-based page index.
```yaml
paginator:
  type: page_number
  page_param: page
  size_param: page_size           # optional
  page_size: 50
  start: 1                        # default is 1
  total_pages_path: total_pages   # optional
  in: query                       # or "body"
```

### 4.4 `link_header`
Paginates via RFC 8288 `Link: <url>; rel="next"` response headers.
```yaml
paginator:
  type: link_header
```

### 4.5 `none`
Disables pagination for single-entity or single-batch requests:
```yaml
paginator:
  type: none
```

### Next-page links and redirects stay on the API's origin
A next-page URL (`next_url_path`, `link_header`) or a redirect comes from the response, and the request that follows
it carries the source's credentials (`auth` and every `http.headers` value, secrets included). StreamWright therefore
follows them only on the origin (scheme, host, port) of the request that returned them; an `http` → `https` upgrade on
the same host is allowed. A link or redirect to another origin, or a downgrade to `http`, stops the request with an
error instead of sending the credentials there.

---

## 5. Query Parameter Encoding

Configure `http.params_encoding` in `source.yaml` or on individual requests:

- `plain` (default): Query parameters are emitted as plain key-value pairs (`?key=value`). Nested dictionary or list parameters will be flagged as an error during schema validation.
- `dotted`: Nested structures are flattened into dotted and bracketed paths. Useful for Rest.li and nested APIs:
  ```yaml
  params:
    filter:
      status: ACTIVE
    accounts:
      - act_1
      - act_2
  ```
  Encodes to:
  `?filter.status=ACTIVE&accounts[0]=act_1&accounts[1]=act_2`.

---

## 6. Testing REST Connectors

Test your connector using `responses` and `SourceRunner`:

```python
import responses
from streamwright.core.config.loader import load_source
from streamwright.core.engine.runner import SourceRunner
from streamwright.core.runtime.testing import MemoryOutput

@responses.activate
def test_campaigns():
    responses.add(
        responses.GET,
        "https://api.mynetwork.com/v1/campaigns",
        json={"data": [{"id": "1", "name": "Brand"}]},
        status=200,
    )

    source = load_source("examples/sources/ads/my_network")
    output = MemoryOutput()
    SourceRunner(source, {"account_ids": ["act_1"]}, {"api_key": "secret"}, output=output).run()

    assert len(output.records) == 1
```
