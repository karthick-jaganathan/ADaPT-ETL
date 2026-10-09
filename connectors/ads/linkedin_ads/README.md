# StreamWright LinkedIn Ads connector

The `linkedin_ads` connector for [streamwright](../../../core/README.md): the LinkedIn Marketing Developer Platform REST API. Example:
the source folder [examples/sources/ads/linkedin_ads/](../../../examples/sources/ads/linkedin_ads/).

## Install

```bash
make install-linkedin-ads    # from the repository root; or: pip install ./connectors/ads/linkedin_ads
streamwright connectors      # lists linkedin_ads (with its network logger)
```

## Auth

| Key | Meaning |
|---|---|
| `access_token` | required: a LinkedIn OAuth 2.0 access token |
| `api_version` | optional: API version header (`LinkedIn-Version`), defaults to `202401` |

## Requests

```yaml
requests:
  - name: campaigns_list
    sdk: linkedin_ads
    service: campaigns            # ad_accounts, campaign_groups, campaigns, creatives, ad_analytics
    method: list                  # list, get, or analytics
    arguments:
      params:
        q: search
        search.account.values[0]: "urn:li:sponsoredAccount:{{ partition.account_id }}"
        count: 100
```

- **Services**:
  - `ad_accounts`: Sponsored ad accounts (`/rest/adAccounts`).
  - `campaign_groups`: Campaign groups (`/rest/adCampaignGroups`).
  - `campaigns`: Ad campaigns (`/rest/adCampaigns`).
  - `creatives`: Ad creatives (`/rest/adCreatives`).
  - `ad_analytics`: Delivery metrics (`/rest/adAnalytics`).
- **Methods**:
  - `list`: lists objects using LinkedIn Rest.li offset pagination (`start` / `count`).
  - `get`: retrieves a single object by its `id` / URN.
  - `analytics`: queries performance and delivery analytics by date range, pivot, and time granularity.

## Errors

HTTP 429 (rate limits) and HTTP 5xx server errors are retried, honoring the `Retry-After` header when provided. Other HTTP errors fail with the API's status code and error message.

## Logs

Requests are sent via `requests`: urllib3 logs connection details on `urllib3.connectionpool`.
With `--log streamwright.network=DEBUG`, streamwright logs each request's and response's headers and body, with the `access_token` masked.
