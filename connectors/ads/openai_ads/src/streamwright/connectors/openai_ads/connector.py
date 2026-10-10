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
The `openai_ads` connector: OpenAI Advertising API performance and metadata extraction.

Connects to the OpenAI Ads HTTP API using bearer token authentication.
Inherits the StreamWright HTTP engine with cursor pagination, rate-limit retries, and DuckDB SQL transformations.

### 1. `source.yaml` Contract
```yaml
kind: source
name: openai_ads_pipeline
spec:
  config:
    account_ids: {type: list, items: string, description: "Ad account IDs to sync"}
  secrets:
    openai_ads_key: {type: string, required: true}

auth:
  provider: openai_ads
  type: bearer
  token: "{{ secrets.openai_ads_key }}"

http:
  base_url: https://api.ads.openai.com
  paginator: {type: cursor, token_path: last_id, param: after, has_more_path: has_more}
  records: {path: data}
  retry: {codes: [429, 500, 502, 503, 504], max_attempts: 5, backoff: exponential}
```

### 2. `streams/<stream>.yaml` Contract
```yaml
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
    - name: final_campaigns
      select: |
        SELECT 
          record->>'id' AS campaign_id,
          record->>'name' AS campaign_name,
          record->>'status' AS status,
          (record->>'daily_budget')::DOUBLE AS daily_budget,
          record->>'created_at' AS created_at
        FROM raw_campaigns

export:
  campaigns:
    step: final_campaigns
    primary_key: [campaign_id]
```

### 3. Authentication & Security
- `provider`: `openai_ads`
- `type`: `bearer`
- Credentials: `token` must be a `{{ secrets.* }}` reference. Credentials are never written in source configs and are redacted from all logs.
- Security: Sandboxed HTTP transport restricted to read requests against OpenAI Ads endpoints.

### 4. Transform & Data Shaping
- Emits records as extracted JSON items from the `records.path` response envelope (`data`).
- In `transform` steps, raw JSON records are accessible via DuckDB JSON operators (`record->>'field'`).

### 5. Execution & Behavior
- **Transport**: `http` (StreamWright HTTP engine).
- **Pagination**: Configured via cursor paginator (`last_id` -> `after`).
- **Retries**: Configured for 429 rate limits and 5xx server errors with exponential backoff.
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["OpenAIAdsConnector"]


class OpenAIAdsConnector(Connector):
    """OpenAI Ads: an HTTP API declared in the source (auth + http); see examples/sources/ads/openai_ads."""
    spec = ConnectorSpec(
        name="openai_ads",
        title="OpenAI Ads",
        category="advertising",
        transport="http",
        loggers=("urllib3.connectionpool",),
    )
