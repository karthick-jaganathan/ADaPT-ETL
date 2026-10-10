# StreamWright LinkedIn Ads connector

The `linkedin_ads` connector for [streamwright](../../../core/README.md): the LinkedIn Marketing Developer Platform REST API. Example:
the source folder [examples/sources/ads/linkedin_ads/](../../../examples/sources/ads/linkedin_ads/).

<!-- TODO(linkedin-restli2): support full Rest.li 2.0 query syntax (List(...) and (k:v)) in a future restli2 encoder -->

## Install

```bash
pip install ./connectors/ads/linkedin_ads
streamwright connectors      # lists linkedin_ads
```

## Configuration

In `source.yaml`:

```yaml
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

## Streams

- `ad_accounts`: Sponsored ad accounts (`/rest/adAccounts`).
- `campaign_groups`: Campaign groups (`/rest/adCampaignGroups`).
- `campaigns`: Ad campaigns (`/rest/adCampaigns`).
- `creatives`: Ad creatives (`/rest/adCreatives`).
- `campaign_performance`: Delivery metrics (`/rest/adAnalytics`).

## Errors & Retries

HTTP 429 (rate limits) and HTTP 5xx server errors are handled by StreamWright's HTTP engine with exponential backoff, honoring the `Retry-After` header when provided.
