# Apple Ads Connector for StreamWright

`streamwright-apple-ads` is the connector for extracting campaigns, ad groups, keywords, and reporting analytics from the **Apple Ads Platform API**. Example: [examples/sources/ads/apple_ads/](../../../examples/sources/ads/apple_ads/).

## Installation

```bash
pip install ./connectors/ads/apple_ads
streamwright connectors      # lists apple_ads
```

## Configuration

In `source.yaml`:

```yaml
spec:
  config:
    ad_account_id: {type: string, description: "Apple Ads ad account ID (sent as X-AP-Context)"}
    campaign_ids: {type: list, items: string, required: false, description: "List of campaign IDs for child streams"}
  secrets:
    apple_ads_token: {type: string}

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

## Streams

- `campaigns`: Campaigns and budget configurations (`/v1/campaigns/query`).
- `ad_groups`: Ad groups and default bidding (`/v1/adgroups/query`).
- `keywords`: Targeted keywords and match types (`/v1/keywords/query`).
- `campaign_reports`: Campaign reporting analytics (`/v1/reports/apps/campaigns/query`).

## Errors & Retries

HTTP 429 (rate limits) and HTTP 5xx server errors are handled by StreamWright's HTTP engine with exponential backoff, honoring the `Retry-After` header when provided.
