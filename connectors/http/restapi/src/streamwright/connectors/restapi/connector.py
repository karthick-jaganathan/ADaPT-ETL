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

"""
The `restapi` connector: Declarative HTTP extraction for any REST API.

Connects to REST endpoints, handles Bearer/Basic/API Key/OAuth2 authentication,
manages rate limits and retries, and paginates responses automatically.

### 1. `source.yaml` Contract
```yaml
kind: source
name: my_rest_source
spec:
  config:
    base_url: {type: string, default: "https://api.example.com/v1"}
  secrets:
    api_token: {type: string, required: true}

auth:
  type: bearer                  # "bearer" | "api_key" | "basic" | "oauth2_refresh_token"
  token: "{{ secrets.api_token }}"
  # For api_key: {type: api_key, name: "X-API-Key", in: "header", value: "{{ secrets.api_token }}"}
  # For basic: {type: basic, username: "...", password: "{{ secrets.pwd }}"}
  # For oauth2: {type: oauth2_refresh_token, token_url: "...", client_id: "...", client_secret: "{{ secrets.sec }}", refresh_token: "{{ secrets.tok }}"}

http:
  base_url: "{{ config.base_url }}"
  headers:
    Accept: "application/json"
  rate_limit: {requests: 10, per: 1s}
  retry: {codes: [429, 500, 502, 503], max_attempts: 5, backoff: exponential}
```

### 2. `streams/<stream>.yaml` Contract
```yaml
requests:
  - name: list_items
    url: "/items"               # Appended to http.base_url
    method: GET                 # GET | POST
    params:
      status: "active"
    records: "data.items"       # JSONPath or dot-notation to record list (optional)
    paginator:
      type: cursor              # "cursor" | "page" | "offset" | "link_header"
      cursor_param: "starting_after"
      cursor_path: "meta.next_cursor"
      # For page:   {type: page, page_param: "page", page_size: 100, page_size_param: "limit"}
      # For offset: {type: offset, offset_param: "offset", limit: 100, limit_param: "limit"}
      # For link:   {type: link_header}

transform:
  - name: items_clean
    select: |
      SELECT 
        CAST(id AS VARCHAR) AS item_id,
        name,
        price::DECIMAL(10,2) AS price,
        strptime(created_at, '%Y-%m-%dT%H:%M:%SZ') AS created_at
      FROM list_items

export:
  items:
    step: items_clean
    primary_key: [item_id]
```

### 3. Authentication & Secrets
- `auth.type`: `bearer` (needs `token`), `api_key` (needs `name`, `value`, optional `in: "header"|"query"`),
  `basic` (needs `username`, `password`), `oauth2_refresh_token` (needs `token_url`, `client_id`, `client_secret`, `refresh_token`, optional `scopes`).
- Secrets masking: Tokens and passwords are automatically redacted from all network logs.

### 4. Transform & Data Shaping
- Emits rows matching the records extracted from the HTTP response payload.
- In `transform` steps, each request's name (`list_items`) is available as a relational table in DuckDB SQL.
- Use DuckDB functions for JSON unnesting (`unnest()`, `json_extract()`), string operations, and type casting.

### 5. Execution & Behavior
- **Transport**: `http` (Native declarative requests session).
- **Retries**: Automatic exponential or constant backoff on specified HTTP status codes.
- **Paging**: Automatically loops until paginator finds no next page, empty response, or termination condition.
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["RestApiConnector", "HttpConnector"]


class RestApiConnector(Connector):
    """
    Generic REST API connector metadata.
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="restapi",
        title="REST API",
        category="http",
        transport="http",
    )


class HttpConnector(Connector):
    """
    Generic HTTP connector metadata (alias of restapi).
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="http",
        title="HTTP",
        category="http",
        transport="http",
    )
