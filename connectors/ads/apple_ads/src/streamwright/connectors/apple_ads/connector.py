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
The `apple_ads` connector: Apple Search Ads API campaigns, ad groups, keywords, and reports extraction.

Connects to the Apple Ads REST API using bearer token authentication.
Inherits the StreamWright HTTP engine with body-based offset pagination, account context headers, and DuckDB SQL transformations.

### 1. `source.yaml` Contract
```yaml
kind: source
name: apple_ads_pipeline
spec:
  config:
    ad_account_id: {type: string, description: "Apple Ads ad account ID"}
  secrets:
    apple_ads_token: {type: string, required: true}

auth:
  provider: apple_ads
  type: bearer
  token: "{{ secrets.apple_ads_token }}"

http:
  base_url: https://api.ads.apple.com
  headers:
    X-AP-Context: "adAccountId={{ config.ad_account_id }}"
  paginator:
    type: offset
    in: body
    offset_param: pagination.offset
    limit_param: pagination.limit
    page_size: 100
    total_path: pagination.totalCount
  records:
    path: result
```

### 2. `streams/<stream>.yaml` Contract
```yaml
requests:
  - name: raw_campaigns
    http:
      path: /v1/campaigns/query
      method: POST
      json:
        pagination:
          pageSize: 100
          fetchTotalCount: true

transform:
  mode: page
  steps:
    - name: final_campaigns
      select: |
        SELECT 
          (record->>'id')::BIGINT AS campaign_id,
          record->>'name' AS campaign_name,
          record->>'status' AS status,
          (record->>'$.dailyBudgetAmount.amount')::DOUBLE AS daily_budget
        FROM raw_campaigns

export:
  campaigns:
    step: final_campaigns
    primary_key: [campaign_id]
```

### 3. Authentication & Security
- `provider`: `apple_ads`
- `type`: `bearer`
- Credentials: `token` must be a `{{ secrets.* }}` reference. Credentials and tokens are never written in source configs and are redacted from all logs.
- Security: Sandboxed HTTP transport restricted to read requests against Apple Ads endpoints.

### 4. Transform & Data Shaping
- Emits records as extracted JSON items from the `records.path` response envelope (`result`).
- In `transform` steps, raw JSON records are accessible via DuckDB JSON operators (`record->>'field'`).

### 5. Execution & Behavior
- **Transport**: `http` (StreamWright HTTP engine).
- **Pagination**: Configured via body-based offset paginator (`in: body`).
- **Context**: Requires `X-AP-Context` header specifying `adAccountId`.
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["AppleAdsConnector"]


class AppleAdsConnector(Connector):
    """
    Apple Ads Platform API connector metadata.
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="apple_ads",
        title="Apple Ads",
        category="advertising",
        transport="http",
    )
