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
The `linkedin_ads` connector: LinkedIn Marketing Solutions API campaigns, creatives, and analytics extraction.

Connects to the LinkedIn REST API using OAuth2 bearer token authentication.
Inherits the StreamWright HTTP engine with offset pagination, dotted query params encoding, and DuckDB SQL transformations.

### 1. `source.yaml` Contract
```yaml
kind: source
name: linkedin_ads_pipeline
spec:
  config:
    account_ids: {type: list, items: string, description: "Sponsored Ad Account IDs"}
    api_version: {type: string, default: "202401"}
  secrets:
    linkedin_access_token: {type: string, required: true}

auth:
  provider: linkedin_ads
  type: bearer
  token: "{{ secrets.linkedin_access_token }}"

http:
  base_url: https://api.linkedin.com
  headers:
    LinkedIn-Version: "{{ config.api_version }}"
    X-Restli-Protocol-Version: "2.0.0"
  params_encoding: dotted
  paginator:
    type: offset
    offset_param: start
    limit_param: count
    page_size: 100
    total_path: paging.total
  records:
    path: elements
```

### 2. `streams/<stream>.yaml` Contract
```yaml
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}

requests:
  - name: raw_campaigns
    http:
      path: /rest/adCampaigns
      method: GET
      params:
        q: search
        search.account.values[0]: "urn:li:sponsoredAccount:{{ partition.account_id }}"

transform:
  mode: page
  steps:
    - name: final_campaigns
      select: |
        SELECT 
          partition->>'account_id' AS account_id,
          (record->>'id')::BIGINT AS campaign_id,
          record->>'name' AS campaign_name,
          record->>'status' AS status,
          (record->>'$.dailyBudget.amount')::DOUBLE AS daily_budget
        FROM raw_campaigns

export:
  campaigns:
    step: final_campaigns
    primary_key: [account_id, campaign_id]
```

### 3. Authentication & Security
- `provider`: `linkedin_ads`
- `type`: `bearer`
- Credentials: `token` must be a `{{ secrets.* }}` reference. Credentials are never written in source configs and are redacted from all logs.
- Security: Sandboxed HTTP transport restricted to read requests against LinkedIn REST API endpoints.

### 4. Transform & Data Shaping
- Emits records as extracted JSON items from the `records.path` response envelope (`elements`).
- In `transform` steps, raw JSON records are accessible via DuckDB JSON operators (`record->>'field'`).

### 5. Execution & Behavior
- **Transport**: `http` (StreamWright HTTP engine).
- **Pagination**: Configured via offset paginator (`start`/`count` bounded by `paging.total`).
- **Encoding**: Uses `params_encoding: dotted` for Rest.li nested URL parameter syntax.
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["LinkedInAdsConnector"]


class LinkedInAdsConnector(Connector):
    """
    LinkedIn Marketing Developer Platform connector metadata.
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="linkedin_ads",
        title="LinkedIn Ads",
        category="advertising",
        transport="http",
    )
