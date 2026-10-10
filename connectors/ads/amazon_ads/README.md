# Amazon Ads Connector for StreamWright

`streamwright-amazon-ads` is the connector for extracting advertising data from the **Amazon Advertising API** (Sponsored Products, profiles, etc.). Example: [examples/sources/ads/amazon_ads/](../../../examples/sources/ads/amazon_ads/).

## Installation

```bash
pip install ./connectors/ads/amazon_ads
streamwright connectors      # lists amazon_ads
```

## Configuration

In `source.yaml`:

```yaml
spec:
  config:
    api_url:
      type: string
      default: "https://advertising-api.amazon.com"
      description: "Regional endpoint: https://advertising-api.amazon.com (NA), https://advertising-api-eu.amazon.com (EU), https://advertising-api-fe.amazon.com (FE)"
    profile_id:
      type: string
      required: false
      description: "Amazon Advertising Profile ID (sent as Amazon-Advertising-API-Scope)"
  secrets:
    amazon_client_id: {type: string}
    amazon_client_secret: {type: string}
    amazon_refresh_token: {type: string}

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

Alternative static token auth:
```yaml
auth:
  provider: amazon_ads
  type: bearer
  token: "{{ secrets.amazon_access_token }}"
```

## Streams

- `profiles`: Advertiser profiles and marketplaces (`/v2/profiles`).
- `sp_campaigns`: Sponsored Products campaigns (`/v2/sp/campaigns`).
- `sp_ad_groups`: Sponsored Products ad groups (`/v2/sp/adGroups`).
- `sp_ads`: Sponsored Products product ads (`/v2/sp/productAds`).
- `sp_keywords`: Sponsored Products keywords (`/v2/sp/keywords`).

## Errors & Retries

HTTP 429 (rate limits) and HTTP 5xx server errors are handled by StreamWright's HTTP engine with exponential backoff, honoring the `Retry-After` header when provided.
