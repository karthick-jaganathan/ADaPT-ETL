# StreamWright Google Ads connector

The `google_ads` connector for [streamwright](../../../core/README.md): the `google_ads` auth provider builds a
[google-ads](https://pypi.org/project/google-ads/) client, and `sdk: google_ads` requests run Google Ads Query
Language (GAQL) queries. Example: the source folder [examples/sources/ads/google_ads/](../../../examples/sources/ads/google_ads/).

## Install

```bash
make install-google-ads      # from the repository root; or: pip install ./connectors/ads/google_ads (installs google-ads)
streamwright connectors             # lists google_ads with its SDK logger (gaql is still checked by streamwright validate)
```

## Auth

| Key | Meaning |
|---|---|
| `developer_token`, `client_id`, `client_secret`, `refresh_token` | required (OAuth for an installed or web app) |
| `login_customer_id` | the manager account to call through; dashes are removed |
| `api_version` | e.g. `v25`; must be supported by the installed google-ads library (default: its latest) |

## Requests

| Service | Method | Arguments | Records |
|---|---|---|---|
| `GoogleAdsService` | `search_stream` (recommended), `search` | `customer_id`, `query` | one per row |
| `CustomerService` | `list_accessible_customers` | none | `{customer_id, resource_name}` |

- Write queries with the `gaql` query builder (below), so values are escaped. Plain GAQL text works too, but text
  containing references is rejected.
- Rows are plain dicts keyed like GAQL fields (`record->>'$.metrics.clicks'`, `record->>'$.ad_group.type'` in a
  `transform` step), as in the API's JSON: int64 values are text, enums are names, and fields the API does not return
  are left out, so cast and default them: `coalesce((record->>'$.metrics.clicks')::BIGINT, 0)`.
- The example folder reads campaigns, ad groups, keywords, location targets and audience targets with one query per
  customer each (`ad_group`, `ad_group_criterion` and `campaign_criterion`), and daily campaign performance.
- Only these read-only methods can be called.

## GAQL queries

The `gaql` query builder (a component of this package, in the `streamwright.query_builders` group) writes Google Ads Query
Language text from a mapping, checking, quoting and escaping every value:

```yaml
arguments:
  customer_id: "{{ partition.customer_id }}"
  query:
    gaql:
      select: [campaign.id, campaign.name, metrics.clicks]
      from: campaign
      where:
        - {field: segments.date, op: BETWEEN, type: date, value: ["{{ window.start }}", "{{ window.end }}"]}
        - {field: campaign.status, op: IN, type: enum, value: [ENABLED, PAUSED]}
        - {field: campaign.id, op: IN, type: int, value: "{{ config.campaign_ids }}", skip_if_empty: true}
      order_by: [metrics.clicks DESC]
      limit: 1000
```

- `where` items are joined with AND. `op`: `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN`, `NOT IN`, `LIKE`, `NOT LIKE`,
  `BETWEEN` (two values), `IS NULL`, `IS NOT NULL` (no value), `CONTAINS ANY`, `CONTAINS ALL`, `CONTAINS NONE`.
- `type` says how values are written: `int` (whole numbers only), `string` (quoted, escaped), `enum` (bare names:
  letters, digits and `_`) and `date` (`YYYY-MM-DD`, `today` or offsets such as `-7d`).
- `skip_if_empty: true` drops an item whose value (or a `BETWEEN` bound) is missing, for optional filters.
- `streamwright validate` and `streamwright run` check the mapping before anything is fetched; platforms that allow-list
  components allow `gaql` as well as `google_ads`.

## Errors

Quota errors (RESOURCE_EXHAUSTED, waiting as long as Google asks, up to `retry.max_delay`: raise it to wait longer
than 60 seconds), internal and transient errors (INTERNAL, UNAVAILABLE, DEADLINE_EXCEEDED) are retried. A
`search_stream` that fails after its first batch is not retried, since its rows were already read; the window fails
and the next run resumes it. Other errors fail the window with their Google Ads error codes and the request ID.

## Logs

The client logs its calls on the logger `google.ads.googleads.client`, which `streamwright connectors` lists: one line per call
at `INFO`, and each request and response at `DEBUG`. streamwright never turns it on; name it with `--log`:

```bash
streamwright run examples/sources/ads/google_ads --set customer_ids=1112223333 \
  --log streamwright.network=DEBUG --log google.ads.googleads.client=DEBUG
```

This replaces google-ads' logging snippet (`logging.basicConfig()`, then the logger `google.ads.googleads.client` at
`DEBUG`). The client's lines are redacted like streamwright's own: the developer token, the client secret, the refresh token
and the access tokens the credentials get, refreshed ones too, are `***`. `--log streamwright.network=INFO` alone gives
streamwright's line per response, after its stream, request, partition and window, e.g.
`google_ads GoogleAdsService.search_stream page 1: 10,000 record(s), 2.10 s, attempt 1`; a `search_stream` call gives
a page per streamed batch.
