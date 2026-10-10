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
The `amazon_ads` connector: Amazon Advertising API Sponsored Products, Profiles, and Ads extraction.

Connects to the Amazon Advertising API using OAuth2 refresh token grant or static Bearer token.
Inherits the StreamWright HTTP engine with offset pagination, regional endpoint configuration, and DuckDB SQL transformations.

### 1. `source.yaml` Contract
```yaml
kind: source
name: amazon_ads_pipeline
spec:
  config:
    api_url: {type: string, default: "https://advertising-api.amazon.com"}
    profile_id: {type: string, required: false, description: "Amazon Advertising Profile ID"}
  secrets:
    amazon_client_id: {type: string, required: true}
    amazon_client_secret: {type: string, required: true}
    amazon_refresh_token: {type: string, required: true}

auth:
  provider: amazon_ads
  type: oauth2_refresh_token
  token_url: https://api.amazon.com/auth/o2/token
  client_id: "{{ secrets.amazon_client_id }}"
  client_secret: "{{ secrets.amazon_client_secret }}"
  refresh_token: "{{ secrets.amazon_refresh_token }}"

http:
  base_url: "{{ config.api_url }}"
  headers:
    Amazon-Advertising-API-ClientId: "{{ secrets.amazon_client_id }}"
    Amazon-Advertising-API-Scope: "{{ config.profile_id }}"
  paginator:
    type: offset
    offset_param: startIndex
    limit_param: count
    page_size: 100
```

### 2. `streams/<stream>.yaml` Contract
```yaml
requests:
  - name: raw_sp_campaigns
    http:
      path: /v2/sp/campaigns
      method: GET

transform:
  mode: page
  steps:
    - name: final_sp_campaigns
      select: |
        SELECT 
          record->>'campaignId' AS campaign_id,
          record->>'name' AS campaign_name,
          record->>'campaignType' AS campaign_type,
          record->>'state' AS state,
          (record->>'dailyBudget')::DOUBLE AS daily_budget
        FROM raw_sp_campaigns

export:
  sp_campaigns:
    step: final_sp_campaigns
    primary_key: [campaign_id]
```

### 3. Authentication & Security
- `provider`: `amazon_ads`
- `type`: `oauth2_refresh_token` or `bearer`
- Credentials: `client_id`, `client_secret`, and `refresh_token` must be `{{ secrets.* }}` references. Credentials and tokens are never written in source configs and are redacted from all logs.
- Security: Sandboxed HTTP transport restricted to read requests against Amazon Advertising endpoints.

### 4. Transform & Data Shaping
- Emits records as extracted JSON items from the response body.
- In `transform` steps, raw JSON records are accessible via DuckDB JSON operators (`record->>'field'`).

### 5. Execution & Behavior
- **Transport**: `http` (StreamWright HTTP engine).
- **Regional Endpoints**: North America (`https://advertising-api.amazon.com`), Europe (`https://advertising-api-eu.amazon.com`), Far East (`https://advertising-api-fe.amazon.com`).
- **Pagination**: Configured via offset paginator (`startIndex`/`count`).
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["AmazonAdsConnector"]


class AmazonAdsConnector(Connector):
    """
    Amazon Advertising API connector metadata.
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="amazon_ads",
        title="Amazon Ads",
        category="advertising",
        transport="http",
    )
