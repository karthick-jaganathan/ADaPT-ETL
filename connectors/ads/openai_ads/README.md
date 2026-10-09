# StreamWright OpenAI Ads connector

The `openai_ads` connector for [streamwright](../../../core/README.md): the OpenAI Advertiser API. Example:
the source folder [examples/sources/ads/openai_ads/](../../../examples/sources/ads/openai_ads/).

## Install

```bash
make install-openai-ads    # from the repository root; or: pip install ./connectors/ads/openai_ads
streamwright connectors             # lists openai_ads (with its network logger)
```

## Auth

| Key | Meaning |
|---|---|
| `advertiser_api_key` | required: an OpenAI Advertiser API key |

## Requests

```yaml
requests:
  - name: raw_campaigns
    sdk: openai_ads
    service: campaigns            # ad_accounts, campaigns, ad_groups, ads
    method: list                  # list, get, or insights
    arguments:
      params:
        ad_account_id: "{{ partition.account_id }}"
        limit: 500
```

- **Services**: `ad_accounts`, `campaigns`, `ad_groups`, `ads`.
- **Methods**:
  - `list`: lists objects, paged by the connector using `after` cursor tokens.
  - `get`: retrieves a single object by its `id`.
  - `insights`: retrieves delivery insights for `campaigns`, `ad_groups`, or `ads` by `id` (e.g. `/v1/campaigns/{id}/insights`).
- **Hierarchy & Trees**: Child entities can be read either at the account level (`ad_groups.yaml`, `ads.yaml`) or hierarchically by parent ID using request-level partitions (`ad_group_tree.yaml`, `ad_tree.yaml`) so that parent details are joined with child records.

## Errors

HTTP 429 (rate limits) and HTTP 5xx server errors are retried, honoring the `Retry-After` header when provided. Other HTTP errors fail with the API's status code and error message.

## Logs

Requests are sent via `requests`: urllib3 logs connection details on `urllib3.connectionpool`.
With `--log streamwright.network=DEBUG`, streamwright logs each request's and response's headers and body, with the `advertiser_api_key` masked.
