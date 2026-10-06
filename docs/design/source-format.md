---
layout: default
title: Source configuration format
nav_exclude: true
permalink: /design/source-format/
---
{% raw %}
<!-- raw: the {{ }} references below are part of the format, not Jekyll's Liquid -->

# Source configuration format

**Status:** design accepted (see [Decisions](#decisions)). `adapt validate` checks sources (`kind: source`), the JSON Schema is `docs/schemas/source.schema.json`, `adapt run` (the `adapt-core` package) runs them, and the connectors `adapt-google-ads`, `adapt-microsoft-ads` and `adapt-facebook-ads` add the SDK-backed APIs, and the reader connectors `adapt-files` (local files), `adapt-s3` and `adapt-gcs` (object storage) and `adapt-postgres` (PostgreSQL databases) read files and databases. The examples below are the source folders in `examples/sources/`. The legacy kinds (`authorization`, `connector`, `serializer`, `pipeline`) have been removed (see [Rollout](#rollout)): this is the only format.

## Summary

Legacy configs (the `authorization`, `connector`, `serializer` and `pipeline` kinds) describe
*Python calls* (`module` / `class` / `method` / kwargs). That is flexible, but customers need to
know SDK internals, the YAML can run any Python, and the language cannot express what real APIs need: more than one
request per run, pagination, async report jobs, incremental state, retries.

The source format lets customers describe *what to extract*: a **source** with **streams**. Each stream reads its
named `requests`, shapes their records with named SQL `transform` steps (DuckDB queries that can join the stream's
requests and earlier steps), and writes the steps its `export` names. Streams are self-contained: joins across
streams and totals over full history run in the warehouse after loading. Vendor-specific code (SDK clients, query
builders, response conversion) moves into vetted **connectors** that YAML can name but not import. One source (a folder
with a `source.yaml` and a file per stream) replaces today's authorization + connector + serializer + pipeline files.

## Goals

1. Customers write *what* to fetch and how to shape it, not Python call graphs.
2. Express real ad-API needs: partitions (accounts × date windows), pagination, async report jobs, incremental sync
   with lookback, rate limits and retries.
3. Safe to run customer YAML on a hosted platform: no arbitrary imports, secrets confined to `auth`.
4. Fully statically checkable by `adapt validate`, including every `{{ reference }}`.
5. Keep the legacy format's strengths (SDK-backed calls to gRPC/SOAP APIs, streaming), and shape records with
   standard SQL instead of a transform library.

**Non-goals:** warehouse loaders of our own (emit a standard protocol, and load through dlt), scheduling and
orchestration, general-purpose programming in YAML (no loops or arbitrary expressions), and transforms across streams
or sources (they run in the warehouse after loading).

## At a glance

A source is a folder: `source.yaml` holds what the streams share, and each stream (one entity, or one report) is a
file in `streams/`, named after the stream. The folder is one source: one sign-in and one state per run. A stream
reads only its own requests; another stream can be its `from_stream` parent, whose export gives its partitions.

```text
google_ads/                         adapt run google_ads
├── source.yaml                     kind, name, spec, auth, http
├── streams/
│   ├── ad_groups.yaml              the stream `ad_groups`
│   ├── campaigns.yaml
│   ├── campaign_performance.yaml
│   └── keywords.yaml
```

```text
requests ─▶ a table per request ─▶ transform: named SQL steps ─▶ export: the steps written, by name
                                   (each step is a table for later steps)
```

```yaml
# source.yaml: what the streams share
kind: source
name: <source name>
spec:      # inputs: config (visible) and secrets (redacted), with types and defaults
auth:      # how to authenticate: oauth2_refresh_token | api_key | bearer | basic | <connector provider>
http:      # optional defaults for HTTP streams: base_url, headers, rate_limit, retry
```

```yaml
# streams/<stream name>.yaml: what to extract
partitions:    # list values or a parent stream's export; combined as a cartesian product
incremental:   # cursor field, start, window size, lookback
requests:      # named requests: http | sdk (connector) | async_job, each with its paginator, records and partitions
transform:     # mode (page | run) and steps: named SQL (DuckDB) queries over the requests and earlier steps
export:        # export name -> {step, primary_key}: the output tables
```

A small source can instead be one file: `source.yaml`'s keys plus `streams`, a list with a `name` on each stream.

## Worked examples

Each example is a source folder in `examples/sources/`. The blocks below show each `source.yaml` and the main streams;
[Metadata streams](#metadata-streams) lists every stream. The streams follow one convention: each request is named
`raw_<entity>`, and a stream's last step and its export are named after the stream.

### 1. Google Ads — SDK requests, typed GAQL, daily windows, incremental (and the legacy campaign pipeline)

```text
examples/sources/ads/google_ads/
├── source.yaml                     inputs and the Google Ads sign-in, shared by every stream
├── streams/
│   ├── ad_group_hierarchy.yaml
│   ├── ad_groups.yaml
│   ├── audience_targets.yaml
│   ├── campaign_performance.yaml
│   ├── campaigns.yaml
│   ├── keywords.yaml
│   └── location_targets.yaml
```

```yaml
# google_ads/source.yaml
kind: source
name: google_ads
description: Campaigns with their settings, and campaign performance with one row per campaign per day.

spec:
  config:
    customer_ids: {type: list, items: string, description: Customer IDs without dashes}
    login_customer_id: {type: string, required: false}
    start_date: {type: date, default: "-30d"}
    campaign_ids: {type: list, items: integer, required: false, description: Only these campaigns (campaigns)}
    channel_types: {type: list, items: string, required: false, description: "e.g. SEARCH,PERFORMANCE_MAX (campaigns)"}
  secrets:
    developer_token: {type: string}
    client_id: {type: string}
    client_secret: {type: string}
    refresh_token: {type: string}

auth:
  provider: google_ads                 # registered by the adapt-google-ads connector
  developer_token: "{{ secrets.developer_token }}"
  client_id: "{{ secrets.client_id }}"
  client_secret: "{{ secrets.client_secret }}"
  refresh_token: "{{ secrets.refresh_token }}"
  login_customer_id: "{{ config.login_customer_id }}"
  api_version: v25
```

The legacy campaign connector + serializer pair, as one stream: the request reads each customer's campaigns, and one
step shapes each page of rows:

```yaml
# google_ads/streams/campaigns.yaml
partitions:
  - {name: customer_id, values: "{{ config.customer_ids }}"}
requests:
  - name: raw_campaigns
    sdk: google_ads
    service: GoogleAdsService
    method: search_stream
    arguments:
      customer_id: "{{ partition.customer_id }}"
      query:
        gaql:
          select: [customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone,
                   customer.auto_tagging_enabled, customer.tracking_url_template,
                   customer.conversion_tracking_setting.conversion_tracking_id,
                   customer.remarketing_setting.google_global_site_tag, campaign.id, campaign.name,
                   campaign.status, campaign.advertising_channel_type, campaign.bidding_strategy_type,
                   campaign.start_date_time, campaign.end_date_time, campaign.target_cpa.target_cpa_micros,
                   campaign.target_roas.target_roas, campaign_budget.id, campaign_budget.amount_micros,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
                   metrics.conversions_value]
          from: campaign
          where:
            - {field: campaign.status, op: IN, type: enum, value: [ENABLED, PAUSED]}
            - {field: campaign.id, op: IN, type: int, value: "{{ config.campaign_ids }}", skip_if_empty: true}
            - {field: campaign.advertising_channel_type, op: IN, type: enum, value: "{{ config.channel_types }}",
               skip_if_empty: true}
transform:
  mode: page
  steps:
    - name: campaigns
      select: |
        SELECT record->>'$.customer.id'                                             AS customer_id,
               record->>'$.customer.descriptive_name'                               AS customer_name,
               record->>'$.customer.currency_code'                                  AS customer_currency,
               record->>'$.customer.time_zone'                                      AS customer_timezone,
               (record->>'$.customer.auto_tagging_enabled')::BOOLEAN                AS auto_tagging_enabled,
               record->>'$.customer.tracking_url_template'                          AS tracking_url_template,
               record->>'$.customer.conversion_tracking_setting.conversion_tracking_id' AS conversion_tracking_id,
               record->>'$.customer.remarketing_setting.google_global_site_tag'     AS google_global_site_tag,
               record->>'$.campaign.id'                                             AS campaign_id,
               record->>'$.campaign.name'                                           AS campaign_name,
               CASE record->>'$.campaign.status' WHEN 'ENABLED' THEN 'active' WHEN 'PAUSED' THEN 'paused'
                    WHEN 'REMOVED' THEN 'removed' END AS status,
               record->>'$.campaign.advertising_channel_type'                       AS advertising_channel_type,
               record->>'$.campaign.bidding_strategy_type'                          AS bidding_strategy_type,
               (record->>'$.campaign.start_date_time')::TIMESTAMP::DATE             AS start_date,
               (record->>'$.campaign.end_date_time')::TIMESTAMP::DATE               AS end_date,
               record->>'$.campaign_budget.id'                                      AS budget_id,
               round((record->>'$.campaign_budget.amount_micros')::BIGINT * 0.000001, 2) AS budget_amount,
               round((record->>'$.campaign.target_cpa.target_cpa_micros')::BIGINT * 0.000001, 2) AS target_cpa,
               round((record->>'$.campaign.target_roas.target_roas')::DOUBLE, 2)    AS target_roas,
               coalesce((record->>'$.metrics.impressions')::BIGINT, 0)              AS impressions,
               coalesce((record->>'$.metrics.clicks')::BIGINT, 0)                   AS clicks,
               coalesce(round((record->>'$.metrics.cost_micros')::BIGINT * 0.000001, 2), 0) AS cost,
               coalesce(round((record->>'$.metrics.conversions')::DOUBLE, 2), 0)    AS conversions,
               coalesce(round((record->>'$.metrics.conversions_value')::DOUBLE, 2), 0) AS conversion_value,
               coalesce(round(clicks / nullif(impressions, 0), 6), 0)               AS ctr,
               coalesce(round(cost / nullif(clicks, 0), 2), 0)                      AS cpc,
               coalesce(round(cost / nullif(conversions, 0), 2), 0)                 AS cost_per_conversion,
               coalesce(round(conversion_value / nullif(cost, 0), 2), 0)            AS roas
        FROM raw_campaigns
export:
  campaigns:
    step: campaigns
    primary_key: [customer_id, campaign_id]
```

One row per campaign per day, read incrementally:

```yaml
# google_ads/streams/campaign_performance.yaml
partitions:
  - {name: customer_id, values: "{{ config.customer_ids }}"}
incremental:
  cursor_field: date
  start: "{{ config.start_date }}"
  window: 7d
  lookback: 30d                    # conversions are restated for weeks; re-read the last 30 days each run
requests:
  - name: raw_campaign_performance
    sdk: google_ads
    service: GoogleAdsService
    method: search_stream
    arguments:
      customer_id: "{{ partition.customer_id }}"
      query:
        gaql:                      # the gaql query builder (adapt-google-ads) writes the escaped query
          select: [customer.id, campaign.id, campaign.name, campaign.status, segments.date,
                   metrics.impressions, metrics.clicks, metrics.cost_micros]
          from: campaign
          where:
            - {field: segments.date, op: BETWEEN, type: date, value: ["{{ window.start }}", "{{ window.end }}"]}
            - {field: campaign.status, op: IN, type: enum, value: [ENABLED, PAUSED]}
transform:
  mode: page
  steps:
    - name: campaign_performance
      select: |                    # SQL (DuckDB) over each page of rows: ids and counts are text, money is in micros
        SELECT record->>'$.customer.id'                                  AS customer_id,
               record->>'$.campaign.id'                                  AS campaign_id,
               record->>'$.campaign.name'                                AS campaign_name,
               CASE record->>'$.campaign.status' WHEN 'ENABLED' THEN 'active' WHEN 'PAUSED' THEN 'paused'
                    WHEN 'REMOVED' THEN 'removed' END AS status,
               (record->>'$.segments.date')::DATE                        AS date,
               coalesce((record->>'$.metrics.impressions')::BIGINT, 0)   AS impressions,
               coalesce((record->>'$.metrics.clicks')::BIGINT, 0)        AS clicks,
               coalesce(round((record->>'$.metrics.cost_micros')::BIGINT * 0.000001, 2), 0) AS cost,
               round(clicks / nullif(impressions, 0), 6)                 AS ctr
        FROM raw_campaign_performance
export:
  campaign_performance:
    step: campaign_performance
    primary_key: [customer_id, campaign_id, date]
```

Daily performance with each campaign's settings is a join of these two streams' tables, made in the warehouse after
loading (see [Across streams](#across-streams)).

Ad groups with their campaign, in one stream with few calls. `ad_group_hierarchy` runs in `run` mode: it lists each
customer's campaigns (`raw_campaigns`), then reads their ad groups with `batch_size: 200` on the request partition,
so `partition.campaign_ids` is a list of up to 200 of the customer's campaign ids and each query reads the ad groups
of all of them (`campaign.id IN (...)`): 1000 campaigns take 5 ad group queries, not 1000. Its last step joins the
two:

```yaml
# google_ads/streams/ad_group_hierarchy.yaml
# Each customer's ad groups with their campaign: the ad groups of up to 200 campaigns per query (batch_size).
partitions:
  - {name: customer_id, values: "{{ config.customer_ids }}"}
requests:
  - name: raw_campaigns
    sdk: google_ads
    service: GoogleAdsService
    method: search_stream
    arguments:
      customer_id: "{{ partition.customer_id }}"
      query:
        gaql:
          select: [customer.id, campaign.id, campaign.name, campaign.status]
          from: campaign
          where:
            - {field: campaign.status, op: IN, type: enum, value: [ENABLED, PAUSED]}
  - name: raw_ad_groups
    partitions:                    # campaign_ids: a list of up to 200 of this customer's campaign ids per query
      - {name: campaign_ids, from: raw_campaigns, field: campaign.id, batch_size: 200}
    sdk: google_ads
    service: GoogleAdsService
    method: search_stream
    arguments:
      customer_id: "{{ partition.customer_id }}"
      query:
        gaql:
          select: [customer.id, campaign.id, ad_group.id, ad_group.name, ad_group.status]
          from: ad_group
          where:
            - {field: campaign.id, op: IN, type: int, value: "{{ partition.campaign_ids }}"}
transform:
  mode: run                        # the ad group queries take their campaign ids from the campaign query
  steps:
    - name: campaign_rows
      select: |
        SELECT record->>'$.customer.id'   AS customer_id,
               record->>'$.campaign.id'   AS campaign_id,
               record->>'$.campaign.name' AS campaign_name,
               CASE record->>'$.campaign.status' WHEN 'ENABLED' THEN 'active' WHEN 'PAUSED' THEN 'paused'
                    WHEN 'REMOVED' THEN 'removed' END AS campaign_status
        FROM raw_campaigns
    - name: ad_group_rows
      select: |
        SELECT record->>'$.customer.id'   AS customer_id,
               record->>'$.campaign.id'   AS campaign_id,
               record->>'$.ad_group.id'   AS ad_group_id,
               record->>'$.ad_group.name' AS ad_group_name,
               CASE record->>'$.ad_group.status' WHEN 'ENABLED' THEN 'active' WHEN 'PAUSED' THEN 'paused'
                    WHEN 'REMOVED' THEN 'removed' END AS ad_group_status
        FROM raw_ad_groups
    - name: ad_group_hierarchy
      select: |
        SELECT a.customer_id, a.campaign_id, c.campaign_name, c.campaign_status,
               a.ad_group_id, a.ad_group_name, a.ad_group_status
        FROM ad_group_rows a
        LEFT JOIN campaign_rows c USING (customer_id, campaign_id)
export:
  ad_group_hierarchy:
    step: ad_group_hierarchy
    primary_key: [customer_id, ad_group_id]
```

### 2. A plain HTTP API — HTTP, OAuth refresh, pagination, rate limit

An illustrative source (not a folder in `examples/sources/`) for an API with no connector: built-in HTTP requests,
OAuth refresh-token sign-in, offset pagination, retries and a rate limit. The `example.com` URLs stand in for the API
you target; check its pagination style.

```yaml
# http_api/source.yaml (illustrative)
kind: source
name: http_api

spec:
  config:
    account_ids: {type: list, items: string}
  secrets:
    client_id: {type: string}
    client_secret: {type: string}
    refresh_token: {type: string}

auth:
  type: oauth2_refresh_token
  token_url: https://auth.example.com/oauth/v2/accessToken
  client_id: "{{ secrets.client_id }}"
  client_secret: "{{ secrets.client_secret }}"
  refresh_token: "{{ secrets.refresh_token }}"

http:
  base_url: https://api.example.com/rest
  headers: {Api-Version: "202509"}
  rate_limit: {requests: 1000, per: 1h}
  retry: {codes: [429, 500, 502, 503, 504], max_attempts: 3, backoff: exponential}
```

```yaml
# http_api/streams/campaigns.yaml (illustrative)
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}
requests:
  - name: raw_campaigns
    http:
      path: "/adAccounts/{{ partition.account_id }}/adCampaigns"
      params: {q: search}
    paginator: {type: offset, offset_param: start, limit_param: count, page_size: 100}
    records: {path: elements}
transform:
  mode: page
  steps:
    - name: campaigns
      select: |
        SELECT record->>'id'                             AS campaign_id,
               partition->>'account_id'                  AS account_id,
               record->>'name'                           AS campaign_name,
               record->>'status'                         AS status,
               (record->>'$.dailyBudget.amount')::DOUBLE AS daily_budget
        FROM raw_campaigns
export:
  campaigns:
    step: campaigns
    primary_key: [campaign_id]
```

### 3. Microsoft Ads — async report job (not expressible in the legacy format), and the account hierarchy

```text
examples/sources/ads/microsoft_ads/
├── source.yaml
└── streams/
    ├── ad_group_tree.yaml
    ├── ad_groups.yaml
    ├── audience_targets.yaml
    ├── campaign_performance.yaml
    ├── campaigns.yaml
    ├── keywords.yaml
    └── location_targets.yaml
```

```yaml
# microsoft_ads/source.yaml
kind: source
name: microsoft_ads

spec:
  config:
    account_ids: {type: list, items: string}
    customer_id: {type: string}
    start_date: {type: date, default: "-30d"}
  secrets:
    developer_token: {type: string}
    client_id: {type: string}
    client_secret: {type: string, required: false, description: For web apps; leave out for desktop and mobile apps}
    refresh_token: {type: string}

auth:
  provider: microsoft_ads              # registered by the adapt-microsoft-ads connector
  developer_token: "{{ secrets.developer_token }}"
  client_id: "{{ secrets.client_id }}"
  client_secret: "{{ secrets.client_secret }}"
  refresh_token: "{{ secrets.refresh_token }}"
  customer_id: "{{ config.customer_id }}"
```

The report job: submit it, poll until it is done, then download its file.

```yaml
# microsoft_ads/streams/campaign_performance.yaml
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}
incremental: {cursor_field: date, start: "{{ config.start_date }}", window: 30d, lookback: 30d}
requests:
  - name: raw_campaign_performance
    async_job:
      submit:
        sdk: microsoft_ads
        service: ReportingService
        method: SubmitGenerateReport
        arguments:
          ReportRequest:
            "@type": CampaignPerformanceReportRequest   # SOAP type, built by the connector
            Aggregation: Daily
            Format: Csv
            ExcludeReportHeader: true
            ExcludeReportFooter: true
            Columns: [TimePeriod, AccountId, CampaignId, CampaignName, Impressions, Clicks, Spend]
            Scope: {AccountIds: ["{{ partition.account_id }}"]}
            Time: {CustomDateRangeStart: "{{ window.start }}", CustomDateRangeEnd: "{{ window.end }}"}
      poll:
        method: PollGenerateReport
        arguments: {ReportRequestId: "{{ submit.result }}"}
        every: 15s
        timeout: 30m
        done_when: {path: Status, equals: Success}
        fail_when: {path: Status, equals: Error}
      download: {url: "{{ poll.ReportDownloadUrl }}", format: csv, compression: zip}
transform:
  mode: page
  steps:
    - name: campaign_performance
      select: |                    # SQL (DuckDB) over each page of report rows: the report's values are text
        SELECT record->>'AccountId'                          AS account_id,
               record->>'CampaignId'                         AS campaign_id,
               record->>'CampaignName'                       AS campaign_name,
               (record->>'TimePeriod')::DATE                 AS date,
               coalesce((record->>'Impressions')::BIGINT, 0) AS impressions,
               coalesce((record->>'Clicks')::BIGINT, 0)      AS clicks,
               coalesce((record->>'Spend')::DOUBLE, 0)       AS spend
        FROM raw_campaign_performance
export:
  campaign_performance:
    step: campaign_performance
    primary_key: [account_id, campaign_id, date]
```

The ad groups of each campaign: the export of the `campaigns` stream supplies (account, campaign) pairs, and each
request names its account in the `CustomerAccountId` header, since the operation only takes the campaign's ID.

```yaml
# microsoft_ads/streams/ad_groups.yaml
partitions:
  - {from_stream: campaigns, fields: [account_id, campaign_id]}   # partition.account_id and partition.campaign_id
requests:
  - name: raw_ad_groups
    sdk: microsoft_ads
    service: CampaignManagementService
    method: GetAdGroupsByCampaignId
    headers: {CustomerAccountId: "{{ partition.account_id }}"}    # the request names only the campaign
    arguments: {CampaignId: "{{ partition.campaign_id }}", ReturnAdditionalFields: [AdGroupType]}
    records: {path: AdGroup}
transform:
  mode: page
  steps:
    - name: ad_groups
      select: |
        SELECT partition->>'account_id'             AS account_id,
               partition->>'campaign_id'            AS campaign_id,
               record->>'Id'                        AS ad_group_id,
               record->>'Name'                      AS ad_group_name,
               record->>'Status'                    AS status,
               record->>'AdGroupType'               AS ad_group_type,
               (record->>'$.CpcBid.Amount')::DOUBLE AS cpc_bid,
               record->>'Language'                  AS language,
               record->>'Network'                   AS network
        FROM raw_ad_groups
export:
  ad_groups:
    step: ad_groups
    primary_key: [account_id, ad_group_id]
```

The same hierarchy can be one stream with two requests. `ad_group_tree` runs in `run` mode: it lists each account's
campaigns (`raw_campaigns`), shapes them in the step `campaign_rows`, reads the ad groups of each of those campaigns
(`raw_ad_groups`, with a request partition `from: campaign_rows`), and joins the two in its last step. The
partition's `fields` include `account_id`, the stream partition's name, so each account's calls use only that
account's campaigns:

```yaml
# microsoft_ads/streams/ad_group_tree.yaml
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}
requests:
  - name: raw_campaigns
    sdk: microsoft_ads
    service: CampaignManagementService
    method: GetCampaignsByAccountId
    arguments:
      AccountId: "{{ partition.account_id }}"
      CampaignType: [Search, Shopping, DynamicSearchAds, Audience, PerformanceMax]
    records: {path: Campaign}
  - name: raw_ad_groups
    partitions:
      - {from: campaign_rows, fields: [account_id, campaign_id]}   # account_id: this account's campaigns only
    sdk: microsoft_ads
    service: CampaignManagementService
    method: GetAdGroupsByCampaignId
    headers: {CustomerAccountId: "{{ partition.account_id }}"}
    arguments: {CampaignId: "{{ partition.campaign_id }}", ReturnAdditionalFields: [AdGroupType]}
    records: {path: AdGroup}
transform:
  mode: run                        # two requests, the second partitioned by a step: in dependency order
  steps:
    - name: campaign_rows
      select: |
        SELECT partition->>'account_id'         AS account_id,
               record->>'Id'                    AS campaign_id,
               record->>'Name'                  AS campaign_name,
               record->>'Status'                AS campaign_status,
               record->>'CampaignType'          AS campaign_type,
               record->>'BudgetType'            AS budget_type,
               (record->>'DailyBudget')::DOUBLE AS daily_budget,
               record->>'TimeZone'              AS time_zone
        FROM raw_campaigns
    - name: ad_group_rows
      select: |
        SELECT partition->>'account_id'             AS account_id,
               partition->>'campaign_id'            AS campaign_id,
               record->>'Id'                        AS ad_group_id,
               record->>'Name'                      AS ad_group_name,
               record->>'Status'                    AS ad_group_status,
               record->>'AdGroupType'               AS ad_group_type,
               (record->>'$.CpcBid.Amount')::DOUBLE AS cpc_bid,
               record->>'Language'                  AS language,
               record->>'Network'                   AS network
        FROM raw_ad_groups
    - name: ad_group_tree
      select: |
        SELECT a.account_id, a.campaign_id, c.campaign_name, c.campaign_status, c.campaign_type,
               c.budget_type, c.daily_budget, c.time_zone,
               a.ad_group_id, a.ad_group_name, a.ad_group_status, a.ad_group_type, a.cpc_bid, a.language, a.network
        FROM ad_group_rows a
        LEFT JOIN campaign_rows c USING (account_id, campaign_id)
export:
  ad_group_tree:
    step: ad_group_tree
    primary_key: [account_id, ad_group_id]
```

### 4. Facebook Ads — Marketing API edge, daily insights shaped with SQL

```yaml
# facebook_ads/source.yaml
kind: source
name: facebook_ads
description: Campaign insights, one row per campaign per day.

spec:
  config:
    account_ids: {type: list, items: string, description: Ad account IDs without the act_ prefix}
    start_date: {type: date, default: "-30d"}
  secrets:
    access_token: {type: string}
    app_id: {type: string, required: false}
    app_secret: {type: string, required: false}

auth:
  provider: facebook_ads               # registered by the adapt-facebook-ads connector
  access_token: "{{ secrets.access_token }}"
  app_id: "{{ secrets.app_id }}"
  app_secret: "{{ secrets.app_secret }}"
  api_version: v26.0
```

```yaml
# facebook_ads/streams/campaign_insights.yaml
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}
incremental:
  cursor_field: date
  start: "{{ config.start_date }}"
  window: 7d
  lookback: 28d                    # attribution windows restate results for up to 28 days
retry: {max_attempts: 5, max_delay: 5m}   # throttling can last minutes
requests:
  - name: raw_campaign_insights
    sdk: facebook_ads
    service: AdAccount
    method: get_insights           # an edge: paged by the connector
    arguments:
      id: "act_{{ partition.account_id }}"
      fields: [account_id, campaign_id, campaign_name, date_start, impressions, clicks, spend]
      params:
        level: campaign
        time_increment: 1
        time_range: {since: "{{ window.start }}", until: "{{ window.end }}"}
        limit: 500
transform:
  mode: page
  steps:
    - name: campaign_insights
      select: |                    # SQL (DuckDB) over each page of records: insights numbers are text
        SELECT record->>'account_id'                         AS account_id,
               record->>'campaign_id'                        AS campaign_id,
               record->>'campaign_name'                      AS campaign_name,
               (record->>'date_start')::DATE                 AS date,
               coalesce((record->>'impressions')::BIGINT, 0) AS impressions,
               coalesce((record->>'clicks')::BIGINT, 0)      AS clicks,
               coalesce((record->>'spend')::DOUBLE, 0)       AS spend,
               round(spend / nullif(clicks, 0), 4)           AS cpc
        FROM raw_campaign_insights
export:
  campaign_insights:
    step: campaign_insights
    primary_key: [campaign_id, date]
```

### 5. Files — local CSV, JSON lines and Parquet files, picked by a glob or a regex

Examples 5 to 7 are file and database sources. They are read through reader connectors, like the ad APIs through SDK
connectors: `files` reads local files, `s3` and `gcs` read object storage, and `postgres` reads a PostgreSQL database
(there is no `https` connector). Each reads with DuckDB, on a connection of the connector's own (never the transform
sandbox), and gives each row as one record.

Local files: `auth: {provider: files, roots: [...]}` names the local folders files can be read from, and each request
is `sdk: files`, `method: read`. The roots are the boundary: every path is checked to be inside one before any file
is read, and the connector has no URLs, no `httpfs` and no credentials (its DuckDB connection never has external
access). This example reads the small files committed in its `data/` folder, so it runs offline:

```bash
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --allow-connector files
```

```text
examples/sources/readers/files_demo/
├── source.yaml                     the data folder (`data_root`): the connector's only root
├── streams/
│   ├── customers.yaml
│   ├── orders.yaml
│   └── products.yaml
└── data/                           customers/<day>.jsonl, orders/<region>.csv, products.parquet
```

```yaml
# files_demo/source.yaml
kind: source
name: files_demo
description: Customers, orders and products from local files.

spec:
  config:
    data_root: {type: string, description: "The folder the files are read from (relative to the working directory)"}
    start_date: {type: date, default: "2026-10-01", description: First day of customers' daily files}

auth:
  provider: files                      # registered by the adapt-files connector
  roots: ["{{ config.data_root }}"]    # the only folders files can be read from
```

A glob reads every file it matches, in name order; `filename` adds each file's path to its records:

```yaml
# files_demo/streams/orders.yaml
requests:
  - name: raw_orders
    sdk: files
    service: file
    method: read
    arguments:
      path: "orders/*.csv"
      format: csv
      options: {header: true, filename: true}
transform:
  mode: page
  steps:
    - name: orders
      select: |
        SELECT (record->>'order_id')::BIGINT          AS order_id,
               regexp_extract(record->>'filename', '([^/]+)\.csv$', 1) AS region,
               (record->>'product_id')::BIGINT        AS product_id,
               (record->>'quantity')::INTEGER         AS quantity,
               (record->>'amount')::DECIMAL(12,2)     AS amount,
               (record->>'ordered_on')::DATE          AS ordered_on
        FROM raw_orders
export:
  orders:
    step: orders
    primary_key: [order_id]
```

A path can use references, so an incremental stream reads one file per day; a day without its file is skipped with a
warning (`on_missing: skip`, the default; `error` fails the request):

```yaml
# files_demo/streams/customers.yaml
incremental:
  cursor_field: signed_up
  start: "{{ config.start_date }}"
  window: 1d
requests:
  - name: raw_customers
    sdk: files
    service: file
    method: read
    arguments:
      path: "customers/{{ window.start }}.jsonl"
      format: jsonl
      on_missing: skip
transform:
  mode: page
  steps:
    - name: customers
      select: |
        SELECT record->>'id'                          AS customer_id,
               record->>'name'                        AS customer_name,
               record->'address'->>'city'             AS city,
               (record->>'signed_up')::DATE           AS signed_up
        FROM raw_customers
export:
  customers:
    step: customers
    primary_key: [customer_id]
```

- `path`: a file, a glob (`*`, `?`, `[ab]`, `**`) or a list of them, relative to the first root or absolute inside
  any root. `{{ config.* }}`, `{{ partition.* }}` and `{{ window.* }}` references are rendered first, and a whole
  reference to a list (e.g. a `batch_size` partition of file names) reads each file of the list.
- `match` and `recursive` pick files by a regex instead of a glob: `path` is then one folder inside a root, and the
  files whose path relative to it fully matches `match` (a literal Python regex, `re.fullmatch`) are read, sorted by
  that relative path. `recursive` (`true` or `false`, default `false`) looks in the folder's sub-folders too, whose
  names the relative path then has (`(.*/)?orders_\d{4}-\d{2}-\d{2}\.csv` selects daily files at any depth). Each
  file it selects is checked to be inside a root, once symbolic links are followed, before any file is read.
  `adapt validate` refuses an invalid or empty regex, a reference in `match`, and a glob or a list `path` with it.
- `format`: `csv`, `tsv`, `json`, `jsonl`, `parquet`, or `auto` (the default: by the file's extension, as
  `products` reads `products.parquet`); `options` are the format's reader options (`header`, `delimiter`, `columns`,
  `compression`, `filename`, ...).
- Each row is one record, a JSON object, in pages of at most 1,000 records. CSV values are text, so steps cast them;
  partitions, windows, `batch_size` and `records.explode` work as for any request.

```yaml
requests:
  - name: raw_orders
    sdk: files
    method: read
    arguments:
      path: orders                                 # a folder inside a root, not a glob
      match: '\d{4}/orders_\d{4}-\d{2}-\d{2}\.csv' # fully matched against each file's path relative to it
      recursive: true                              # sub-folders too (default false): orders/2026/orders_...csv
      format: csv
```

See `connectors/readers/files/README.md` for every option and the errors.

### 6. Object storage — S3 and Google Cloud Storage

Objects are read by two more reader connectors, through DuckDB's `httpfs` extension: `s3` (Amazon S3, or an
S3-compatible store such as MinIO) and `gcs` (Google Cloud Storage, through its S3-compatible access with an HMAC
key). Their requests are those of `files`, on `service: object`: `sdk: s3` or `sdk: gcs`, `method: read`,
`arguments: {path, format, options, on_missing, match, recursive}`. `auth` names the URL prefixes that can be read
(`roots`) and the credentials, which are `{{ secrets.* }}` references only:

```bash
ADAPT_SECRET_S3_KEY_ID=... ADAPT_SECRET_S3_SECRET=... adapt run examples/sources/readers/s3_demo \
    --set bucket_root=s3://my-bucket/exports/ --allow-connector s3 --output jsonl:out
```

```text
examples/sources/readers/s3_demo/
├── source.yaml                     the bucket prefix (`bucket_root`): the only root, its region and the access key
├── streams/
│   ├── customers.yaml              a JSON lines object per day
│   ├── events.yaml                 Parquet parts in date folders, picked by a regex
│   └── orders.yaml                 a glob of CSV objects
```

```yaml
# s3_demo/source.yaml
kind: source
name: s3_demo
description: Customers, orders and events from objects in an S3 bucket.

spec:
  config:
    bucket_root: {type: string, default: "s3://acme-data/exports/", description: "The URL prefix objects are read from (s3://bucket/prefix/)"}
    region: {type: string, default: us-east-1, description: The bucket's region}
    start_date: {type: date, default: "2026-10-01", description: First day of customers' daily objects}
  secrets:
    s3_key_id: {type: string, description: The access key id}
    s3_secret: {type: string, description: The secret access key}

auth:
  provider: s3                         # registered by the adapt-s3 connector
  roots: ["{{ config.bucket_root }}"]  # the only URL prefixes objects can be read from
  key_id: "{{ secrets.s3_key_id }}"    # credentials: secret references only, never written in the source
  secret: "{{ secrets.s3_secret }}"
  region: "{{ config.region }}"        # settings: literal values or references
```

`match` picks objects by a regex, fully matched against each key relative to the `path` folder, in key order; other
objects there (e.g. `_SUCCESS` markers) are not read:

```yaml
# s3_demo/streams/events.yaml
requests:
  - name: raw_events
    sdk: s3
    service: object
    method: read
    arguments:
      path: events/
      match: 'day=\d{4}-\d{2}-\d{2}/part-\d+\.parquet'
      recursive: true
      format: parquet
      options: {filename: true}
transform:
  mode: page
  steps:
    - name: events
      select: |
        SELECT (record->>'event_id')::BIGINT          AS event_id,
               record->>'kind'                        AS kind,
               (record->>'value')::DECIMAL(12,2)      AS value,
               regexp_extract(record->>'filename', 'day=([0-9-]+)/', 1)::DATE AS day
        FROM raw_events
export:
  events:
    step: events
    primary_key: [event_id]
```

Google Cloud Storage is the same with `provider: gcs`, `gs://` roots and an HMAC key, and no other settings; the
streams of `gcs_demo` are those of `s3_demo` with `sdk: gcs` (`events`, `orders`):

```yaml
# gcs_demo/source.yaml
kind: source
name: gcs_demo
description: Orders and events from objects in a Google Cloud Storage bucket.

spec:
  config:
    bucket_root: {type: string, default: "gs://acme-data/exports/", description: "The URL prefix objects are read from (gs://bucket/prefix/)"}
  secrets:
    gcs_key_id: {type: string, description: The HMAC key's access id}
    gcs_secret: {type: string, description: The HMAC key's secret}

auth:
  provider: gcs                        # registered by the adapt-gcs connector
  roots: ["{{ config.bucket_root }}"]  # the only URL prefixes objects can be read from
  key_id: "{{ secrets.gcs_key_id }}"   # credentials: secret references only, never written in the source
  secret: "{{ secrets.gcs_secret }}"
```

- `auth`: `s3` takes `roots` (`s3://bucket/prefix/`), `key_id` and `secret`, an optional `session_token`, and the
  optional settings `region`, `endpoint` (an S3-compatible store's `host[:port]`), `url_style` (`vhost` or `path`)
  and `use_ssl`; `gcs` takes `roots` (`gs://bucket/prefix/`), `key_id` and `secret` only. `key_id`, `secret` and
  `session_token` are each one `{{ secrets.* }}` reference: `adapt validate` refuses anything else, and `adapt run`
  refuses a value that is not a secret of the run, so a literal credential never connects. The settings are literal
  values or references.
- The credentials become one temporary DuckDB secret, scoped to the roots and set with bound parameters, on the
  connector's own connection; they are `***` in every log line, error and run summary.
- No `?` and no `%`: httpfs reads a URL's query parameters as connection settings, so
  `s3://acme/exports/x.csv?s3_endpoint=evil.example.com` would send a request signed with the configured credentials
  to another host. Any `?` or `%` in a root, a path (as written or rendered) or a key a listing returns is refused,
  so `?` is not a glob character for these connectors (only `*`, `[ab]` and `**`).
- Containment: a URL is read only if it has a root's scheme, exactly its bucket and a key under its prefix
  (`s3://acme/` does not hold `s3://acme-evil/...`, `s3://acme/in/` does not hold `s3://acme/inside.csv`); `..`, `.`
  and empty folders, `#`, backslashes, `user@` and ports are refused before anything is read, and DuckDB's
  `allowed_directories` are the roots too.

See `connectors/readers/s3/README.md` and `connectors/readers/gcs/README.md` for every setting, the errors and the tests.

### 7. PostgreSQL — read-only queries with bound parameters

A database is read through a connector too: `auth: {provider: postgres, dsn: "{{ secrets.* }}"}` attaches a PostgreSQL
database `READ_ONLY` with DuckDB's postgres scanner, through a temporary DuckDB secret that holds the DSN (so DuckDB's
system views show no path), and each request is `sdk: postgres` with `method: query` (a SELECT) or `method: table`
(the connector writes the SELECT). The DSN is a secret only, redacted with its password:

```bash
ADAPT_SECRET_PG_DSN=... adapt run examples/sources/readers/postgres_demo --allow-connector postgres --output jsonl:out
```

```text
examples/sources/readers/postgres_demo/
├── source.yaml                     the DSN (a secret) and the statement timeout
├── streams/
│   ├── customers.yaml              the table method
│   ├── order_lines.yaml            two queries: a day's orders, then their lines in batches
│   └── orders.yaml                 a query per day
```

```yaml
# postgres_demo/source.yaml
kind: source
name: postgres_demo
description: Customers, orders and order lines from a PostgreSQL database.

spec:
  config:
    start_date: {type: date, default: "-7d", description: First day of orders (read a day at a time)}
    customer_status: {type: string, default: active, description: Only customers with this status (customers)}
    pg_host: {type: string, default: localhost, description: The PostgreSQL server's host name}
    pg_port: {type: integer, default: 5432, description: Its port}
    pg_database: {type: string, default: shop, description: The database}
    pg_user: {type: string, default: reader, description: A role that can only read}
    pg_sslmode: {type: string, default: prefer, description: "libpq sslmode: disable, allow, prefer, require, ..."}
  secrets:
    pg_password: {type: string, description: The role's password}

auth:
  provider: postgres                       # registered by the adapt-postgres connector
  host: "{{ config.pg_host }}"             # the connection keys are config: literal text or references
  port: "{{ config.pg_port }}"
  database: "{{ config.pg_database }}"
  user: "{{ config.pg_user }}"
  password: "{{ secrets.pg_password }}"    # the only credential: a secret reference only
  sslmode: "{{ config.pg_sslmode }}"
  options: {application_name: adapt-postgres-demo, connect_timeout: "10"}  # extra libpq connection parameters
  statement_timeout: 5min                  # Postgres' statement_timeout for every statement the connector runs
```

The query is DuckDB SQL over the attached database, not text sent to Postgres as written: tables are
`schema.table`, filters and columns are pushed down to Postgres, and values are named parameters (`$since`), bound
from `params`, never pasted into the query. Incremental streams bind the window:

```yaml
# postgres_demo/streams/orders.yaml
incremental:
  cursor_field: updated_on
  start: "{{ config.start_date }}"
  window: 1d
requests:
  - name: raw_orders
    sdk: postgres
    service: database
    method: query
    arguments:
      query: |
        SELECT id, customer_id, status, total::VARCHAR AS total, currency, updated_at
        FROM public.orders
        WHERE updated_at >= $since AND updated_at < $until + INTERVAL 1 DAY
      params:
        since: "{{ window.start }}"
        until: "{{ window.end }}"
transform:
  mode: page
  steps:
    - name: orders
      select: |
        SELECT (record->>'id')::BIGINT                AS order_id,
               (record->>'customer_id')::BIGINT       AS customer_id,
               record->>'status'                      AS status,
               (record->>'total')::DECIMAL(18,2)      AS total,
               record->>'currency'                    AS currency,
               (record->>'updated_at')::TIMESTAMPTZ   AS updated_at,
               (record->>'updated_at')::TIMESTAMPTZ::DATE AS updated_on
        FROM raw_orders
export:
  orders:
    step: orders
    primary_key: [order_id]
```

A `batch_size` partition binds a list, read with `= ANY($ids)` in one query per batch:

```yaml
# postgres_demo/streams/order_lines.yaml
incremental:
  cursor_field: ordered_on
  start: "{{ config.start_date }}"
  window: 1d
requests:
  - name: raw_orders
    sdk: postgres
    service: database
    method: query
    arguments:
      query: |
        SELECT id, updated_at FROM public.orders
        WHERE updated_at >= $since AND updated_at < $until + INTERVAL 1 DAY
      params:
        since: "{{ window.start }}"
        until: "{{ window.end }}"
  - name: raw_lines
    partitions:
      - {name: ids, from: raw_orders, field: id, batch_size: 500}
    sdk: postgres
    service: database
    method: query
    arguments:
      query: |
        SELECT order_id, line_number, product_id, quantity, unit_price
        FROM public.order_lines
        WHERE order_id = ANY($ids::BIGINT[])
      params:
        ids: "{{ partition.ids }}"
transform:
  mode: run                        # the line queries take their order ids from the order query
  steps:
    - name: order_lines
      select: |
        SELECT (l.record->>'order_id')::BIGINT            AS order_id,
               (l.record->>'line_number')::INTEGER        AS line_number,
               (l.record->>'product_id')::BIGINT          AS product_id,
               (l.record->>'quantity')::INTEGER           AS quantity,
               (l.record->>'unit_price')::DECIMAL(12,2)   AS unit_price,
               (o.record->>'updated_at')::TIMESTAMPTZ::DATE AS ordered_on
        FROM raw_lines l
        JOIN raw_orders o ON (o.record->>'id')::BIGINT = (l.record->>'order_id')::BIGINT
export:
  order_lines:
    step: order_lines
    primary_key: [order_id, line_number]
```

`customers` uses the `table` method: `{schema, table, columns, where}`, with `where` items
`{column, op, value, type}` whose names the connector quotes and whose values it binds.

- Read-only: a query must be one SELECT (`WITH`, `FROM`-first and `VALUES` too); write keywords, a second statement,
  table functions and functions that run text of their own are refused before it runs (`QUERY_REFUSED`). The
  database is attached `READ_ONLY`, so writes fail even when the role could write, and the connection then has
  external access off and its configuration locked.
- The query reads the database's own tables only: DuckDB's system and catalog views are refused (`duckdb_*`,
  `pragma_*`, the `system`, `temp`, `pg_catalog` and `information_schema` schemas), and so are `SHOW`, `DESCRIBE` and
  `SUMMARIZE`.
- Each row is one record, a JSON object, in pages of at most 1,000 records. A `numeric` without a precision comes
  through as exact text (DuckDB's `pg_numeric_as_varchar`), never a rounded double. Steps read JSON numbers as
  doubles, so a column that needs more than about 17 digits (big integers, money) is selected as text
  (`total::VARCHAR`) and cast in the step (`::DECIMAL(18,2)`).

See `connectors/readers/postgres/README.md` for the `table` method's operators, the errors and the tests.

### Metadata streams

Each folder reads an account's entities as well, one stream per entity, so they can be selected and scheduled apart
from the performance streams (e.g. entities daily, performance hourly):

| Source | Streams |
|---|---|
| `google_ads` | `campaigns`, `ad_groups`, `keywords`, `location_targets`, `audience_targets`, `campaign_performance`, `ad_group_hierarchy` (two requests, run mode, batched ad group queries) |
| `microsoft_ads` | `campaigns`, `ad_groups` (from `campaigns`), `keywords` (from `ad_groups`), `location_targets` (from `campaigns`), `audience_targets` (from `ad_groups`), `campaign_performance`, `ad_group_tree` (two requests, run mode) |
| `facebook_ads` | `campaigns`, `ad_sets` (with their targeting), `campaign_insights` |
| `files_demo` | `customers` (a JSON lines file per day), `orders` (a glob of CSV files), `products` (a Parquet file) |
| `s3_demo` | `customers` (a JSON lines object per day), `orders` (a glob of CSV objects), `events` (Parquet parts picked by a regex) |
| `gcs_demo` | `orders` (a glob of CSV objects), `events` (Parquet parts picked by a regex) |
| `postgres_demo` | `orders` (a query per day), `customers` (the table method), `order_lines` (two queries, run mode, batched order ids) |

Google reads each entity with one GAQL query per customer (`ad_group`, `ad_group_criterion`, `campaign_criterion`),
and `ad_group_hierarchy` reads campaigns and their ad groups in one stream, the ad groups of up to 200 campaigns per
query (`batch_size`). Microsoft has no query language: its entities follow the hierarchy with `from_stream`
partitions, one call per campaign or ad group (the Bulk API would read a large account in one file), and
`ad_group_tree` reads campaigns and their ad groups in one stream and exports them as one table. Facebook reads the
ad account's edges. The file, object storage and PostgreSQL examples have no account hierarchy: each stream reads
one entity from its files, objects or tables. Tables that combine streams, such as daily performance with each
campaign's settings or totals per account, are made in the warehouse after loading (see
[Across streams](#across-streams)).

## Language reference (draft)

### Files

- A source folder has a `source.yaml` (or `source.yml`) and a `streams/` folder. Each `.yaml` / `.yml` file in
  `streams/` is one stream, named after the file (`streams/ad_groups.yaml` is the stream `ad_groups`), so stream
  files have no `name`; `source.yaml` has everything except `streams`. Other files (a README) are ignored, and
  folders inside `streams/` are an error, so no stream is silently skipped. A `models/` folder is an error (models
  were removed): a stream's `transform` steps shape its own requests, and joins across streams run in the warehouse.
- The folder is read as one document: streams run in file name order (`from_stream` parents first), share `spec`,
  `auth` (one sign-in per run), the `http` rate limit and the state, and can be each other's `from_stream` parents.
- `adapt run` and `adapt validate` take the folder, its `source.yaml` or its `streams/` folder; adapt validate also
  takes one of its stream files (given one, `adapt run` stops with the command to run). Given a directory,
  adapt validate checks each source folder in it as one source. Findings point to the file, line and the path inside
  that file.
- Any other YAML file is a single-file source with a `streams` list. YAML anchors work within one file.
- Editors: `docs/schemas/source.schema.json` for `source.yaml` and single files, `docs/schemas/stream.schema.json`
  for stream files.

### Header and `spec`

`kind: source`, `name`, optional `description`, and an optional `version`: omitting it means the current
format (`1`); a new version is only introduced for a breaking change. Other top-level keys must start with `x-` (e.g.
`x-defaults: &defaults ...` to hold YAML anchors). `spec.config` and `spec.secrets` declare every input
with a `type` (`string`, `integer`, `number`, `boolean`, `date`, `list`), `required` (default true), `default` and
`description`. Dates accept absolute values or offsets from today (`-30d`). The spec drives validation, `adapt check`
and platform UI forms; secrets are redacted everywhere. SQL steps read config values as `$name` parameters (see
[`transform`](#transform-sql-steps)).

### References: `{{ ... }}`

- Scopes: `config`, `secrets` (only inside `auth`), `partition`, `window` (`start`, `end`), `response` (paginators),
  `submit` / `poll` (async jobs), `today`.
- A value that is exactly one reference keeps its type (`"{{ config.customer_ids }}"` stays a list); mixed text renders
  to a string.
- A fixed set of filters, no arbitrary expressions: `default(x)`, `join(sep)`, `date(format)`, `int`, `lower`, `upper`.
- Always quote: a YAML value starting with `{` is a mapping.
- `adapt validate` resolves every reference statically: `{{ config.custmer_ids }}` is an error with a suggestion.
- References are not allowed in SQL steps: they read config values as `$name` parameters instead.

### `auth`

Built-in: `oauth2_refresh_token`, `api_key` (header or query), `bearer`, `basic`. Connectors add providers such as
`google_ads` and `microsoft_ads`, which also build the SDK client used by sdk requests; a source with a connector
provider has only sdk requests. The reader connectors' providers name what can be read instead of an account:

| Provider | Keys |
|---|---|
| `files` | `roots`: the local folders files can be read from (no URLs, no secrets, no credentials) |
| `s3` | `roots` (`s3://bucket/prefix/`), `key_id`, `secret`, optional `session_token` (each one `{{ secrets.* }}` reference), and the optional settings `region`, `endpoint`, `url_style`, `use_ssl` (literal values or references) |
| `gcs` | `roots` (`gs://bucket/prefix/`), `key_id`, `secret` (an HMAC key, each one `{{ secrets.* }}` reference) |
| `postgres` | `dsn` (a libpq DSN, one `{{ secrets.* }}` reference) and an optional `statement_timeout` |

### `requests`

`requests` is a stream's non-empty list of named requests: the only way a stream reads data. Each request is a table
of the stream's SQL, named after it, with one row per record it read (see [`transform`](#transform-sql-steps)). By
convention a request is named `raw_<entity>`, e.g. `raw_campaigns`.

| Key | Meaning |
|---|---|
| `name` | required; unique among the stream's requests and steps (names ignore case), and not the name of a DuckDB built-in table or view |
| `http`, `sdk`, `async_job` | exactly one request kind (below) |
| `paginator`, `records` | optional: how this request's responses are paged, and where its records are (below) |
| `partitions` | optional request-level partitions, only with `transform.mode: run` (below) |

- `http`: `path` (joined to `http.base_url`), `method` (default GET), `params`, `headers`, `json`.
- `sdk`: `sdk` (connector), `service`, `method`, `arguments` and optional `headers`, per-request headers the connector
  takes (e.g. Microsoft Ads' `CustomerAccountId`; others stop the run before it starts). The connector allows only
  read-only calls and converts responses to plain data, read with the request's `records` like HTTP responses
  (replacing the legacy `post_processor`). An async job's `poll` uses its `submit` headers.
- Reader connectors: `sdk` can also name a connector that reads files, objects or a database rather than an API SDK.
  `sdk: files` (`service: file`, `method: read`) reads local csv, tsv, json, jsonl and parquet files, and `sdk: s3`
  and `sdk: gcs` (`service: object`, `method: read`) read the same formats from object storage, all with
  `arguments: {path, format, options, on_missing, match, recursive}`: `path` is a file, a glob or a list of them, or,
  with `match` (a literal Python regex fully matched against each file's path relative to the `path` folder), a
  folder, searched in its sub-folders too with `recursive: true` (default `false`). `sdk: postgres`
  (`service: database`, `method: query`, `arguments: {query, params}`, or `method: table`,
  `arguments: {schema, table, columns, where}`) runs read-only SELECT queries. All read with DuckDB, on a connection
  of the connector's own, each row one record (examples 5 to 7 in [Worked examples](#worked-examples)). There is no
  other request kind for them.
- `async_job`: `submit` (an sdk request), `poll` (`method` of the same service, `arguments`, `every`, `timeout`,
  `done_when`, `fail_when`) and `download` (`url`, `format`: csv / jsonl / json, `compression`: zip / gzip) or
  `results` (an sdk request). The submit and poll responses are the `submit` and `poll` scopes; one that is not a
  mapping (e.g. a job ID) is `submit.result`. `poll` runs every `every` until `done_when` matches; `fail_when` or
  `timeout` fails the request like an API error. Files are downloaded without the API's credentials (report URLs are
  pre-signed, and their query strings are redacted); csv and jsonl give one record per row (empty csv cells are
  null), json is read with `records.path`, and a missing URL means no data.
- Query builders are components: in a request (`http` or `sdk`), a mapping with a single key that is an
  installed query builder's name, such as `query: {gaql: {...}}`, is replaced by the query text the builder writes
  from it, after its references are rendered with their types. The builder checks, quotes and escapes every value,
  so inputs are never concatenated into queries. Only calls written in the source are built: a value from inputs
  or responses that looks like one stays data. `gaql` comes with adapt-google-ads (typed `where` items with
  `type`: int, string, enum, date, and `skip_if_empty: true` to drop an item without a value).

A stream reads its requests for each of its partitions. A request that uses `{{ window.start }}` or
`{{ window.end }}` runs once per partition and window; the others run once per partition, and their rows have null
`window_start` and `window_end`.

### `partitions`, `paginator`, `records`

- `partitions` (stream level): items are `{name, values}` (a list or a list reference), `{name, from_stream, field}`
  (one partition per distinct value of a column of a parent stream's export) or `{from_stream, fields: [...]}` (one
  partition per distinct combination of several columns, named after them: `{from_stream: campaigns, fields:
  [account_id, campaign_id]}` gives `partition.account_id` and `partition.campaign_id`). Rows missing a value give no
  partition. Several items combine as a cartesian product. `from_stream` names another stream of the source that has
  exactly one export. The parent runs first and writes its export, and the child gets the values of the rows it
  wrote in this run. If the parent skipped partitions, the child runs with the partitions it gets and is incomplete
  too (see [Execution semantics](#execution-semantics)). A value from a `DECIMAL` column is the double it was before
  decimals were exact when that double is its exact value (`2.6`, `100.0`), so requests, `partition->>'name'` in
  steps, keys made of it and its bookmarks keep their text; any other value stays the exact decimal
  (`12345678901234567890.123456`). Request partitions from a step's columns do the same.
- Request-level partitions (`partitions` on a `requests` item, only with `transform.mode: run`) are crossed with the
  stream partition, and the request's `partition` holds both. Items are `{name, values}`, `{name, from: X, field: F}`
  or `{from: X, fields: [...]}`, where `X` is an earlier request of the stream (`F` is a dotted path in its `record`)
  or a step of the stream (`F` is one of its columns), never another stream: for that, use a `from_stream`
  partition. A request source gives only the records it read under the current stream partition. When an item's
  partition names (`name`, or the names in `fields`) include a stream partition's name, only the rows with the
  current stream partition's value there are used, as with `from_stream` fields; otherwise a step source gives all
  its rows. Values are distinct, and a row with a missing value gives no partition. A run makes `values` once per
  stream partition, before the request: they can use `config`, `today` and the stream's partitions, not the window
  or the request's own partitions.
- `batch_size: N` (a whole number, at least 1) on a request partition, `{name, values, batch_size}`,
  `{name, from: X, field: F, batch_size}` or `{from: X, fields: [...], batch_size}`, groups the item's distinct
  values, in the order they were first seen, into lists of up to `N` (the last one smaller): one request per list,
  and `partition.<name>` holds the list, not a value. On `{from: X, fields: [...]}`, exactly one field is not a
  stream partition's name: its values are batched and its name holds the list, and the other fields keep only the
  current stream partition's rows. No values give no request, and the lists are still crossed with the stream
  partition and the other items. A `from:` request gives only the records it read under the current stream
  partition, so each stream partition batches only its own values; a `from:` step has the rows of every stream
  partition, and keeps only the current one's when the stream partition fields are listed in `fields`:
  `{from: campaign_rows, fields: [customer_id, campaign_id], batch_size: 200}` lists each customer's campaign ids in
  `partition.campaign_id`. A reference that is a whole value keeps its type, so
  `"{{ partition.<name> }}"` stays a list, and GAQL's `op: IN` writes it as `IN (...)`:
  `{field: campaign.id, op: IN, type: int, value: "{{ partition.campaign_ids }}"}` in
  `google_ads/streams/ad_group_hierarchy.yaml` (above). Use a batched name only where a list is valid: checks do not
  catch it, and GAQL fails when it builds a query with a list for a single value (e.g. `op: "="`). `batch_size` is
  an error on `{from: X, fields: [...]}` unless exactly one field is not a stream partition (one list holds one
  field's values), on `{name, from: <step>, field: F}` in a stream with partitions (its lists would mix every stream
  partition's values: use `{from: <step>, fields: [<stream partitions>, F], batch_size}`), on stream partitions
  (each one is a value with its own state), and on a `from:` item whose `name` is a stream partition's (it has no
  values of its own). Windows and bookmarks are per stream partition, as before.
- `paginator` (per request): `none` (SDK streams and cursors), `offset`, `cursor` (`token_path`, `param`, or a
  next-URL path), `page_number`.
- `records` (per request): `path` to the record list in the response; `explode: <field>` emits one record per nested
  item, with parent fields copied (the legacy `extended_array`).

### `incremental`

`cursor_field` (a column of an export), `start`, `window` (request size in whole days, e.g. `1d`, `7d`), `lookback`
(whole days re-read on every run). A stream with `incremental` needs a request that uses `{{ window.start }}` or
`{{ window.end }}`. State is kept per stream and partition: a `page` stream saves it after every window, so a failed
run resumes where it stopped, and a `run` stream after its exports are written. The saved bookmark is the last
complete day (never today, which is still changing) and never moves back. A run resumes the day after the bookmark,
re-reading `lookback` days but not before `start`; a bookmark older than `start` wins, so no days are skipped. A
`from_stream` parent runs from its own bookmarks, like any stream. Streams without `incremental` save no bookmarks.
A bookmark saved under a partition's key from before decimals were exact (its `DECIMAL` values as doubles, e.g.
`{"amount": 1.2345678901234567e+19}`) is found and moved to the partition's exact key.

### `transform`: SQL steps

`transform` has a `mode` and `steps`, a non-empty list of named DuckDB queries that shape what the stream's requests
read. Each step is a table for later steps of the stream, and [`export`](#export) names the steps that are written.

| Key | Meaning |
|---|---|
| `mode` | `page` or `run`; required (below) |
| `steps[].name` | unique among the stream's requests and steps (names ignore case), and not the name of a DuckDB built-in table or view |
| `steps[].select` | one DuckDB `SELECT` over the stream's request tables and earlier steps, with `$name` config parameters and no `{{ }}` references |
| `steps[].description` | optional text |

Each request is a table named after it, with one row per record it read:

| Column | Type | Holds |
|---|---|---|
| `record` | JSON | the record as the API returned it: `record->>'$.campaign.name'`, `(record->>'clicks')::BIGINT` |
| `partition` | JSON | the partition's values (with request partitions, those too): `partition->>'account_id'` |
| `config` | JSON | the config values (never secrets): `config->>'currency'` |
| `window_start`, `window_end` | DATE | the incremental window (null without `incremental`, and for requests that do not use the window) |
| `today` | DATE | the run's date |

A step reads its own stream's request tables (by request name) and earlier steps of the stream, and nothing else: no
other streams, exports or files. The records as returned are one step: `SELECT record FROM raw_campaigns`.

| Mode | Requests | Steps | Exports and state | Good for |
|---|---|---|---|---|
| `page` | exactly one, without request partitions | run in list order on each page: the request's table holds that page's records | each page's rows are written as they come; state after each window | shaping records page by page, in flat memory |
| `run` | one or more; request partitions allowed | run once each, in dependency order, over all the records the run read | keys checked, then rows written ordered by the key; state after the exports | joining requests, request partitions from a step, totals of a run |

In `run` mode a request runs after the request or step its partitions come `from:`, and a step after the requests
and earlier steps it reads; list order breaks ties. A step can read any earlier step. Here step 4 reads steps 1, 2
and 3, and two steps are exported:

```yaml
incremental: {cursor_field: date, start: "{{ config.start_date }}", window: 1d}
requests:
  - name: raw_campaigns              # no {{ window.* }}: read once per stream partition
    http: {path: /campaigns}
    records: {path: data}
  - name: raw_daily_stats            # read once per window
    http: {path: /stats, params: {day: "{{ window.start }}"}}
    records: {path: data}
transform:
  mode: run
  steps:
    - name: daily                    # 1: reads a request
      select: |
        SELECT record->>'campaign_id' AS campaign_id, window_start AS date,
               (record->>'clicks')::BIGINT AS clicks
        FROM raw_daily_stats
    - name: campaign_names           # 2: reads a request
      select: SELECT record->>'id' AS campaign_id, record->>'name' AS campaign_name FROM raw_campaigns
    - name: totals                   # 3: reads step 1
      select: SELECT date, sum(clicks) AS clicks FROM daily GROUP BY date
    - name: report                   # 4: reads steps 1, 2 and 3
      select: |
        SELECT d.campaign_id, n.campaign_name, d.date, d.clicks,
               round(d.clicks / nullif(t.clicks, 0), 4) AS click_share
        FROM daily d
        JOIN totals t USING (date)
        LEFT JOIN campaign_names n USING (campaign_id)
export:
  campaign_daily: {step: report, primary_key: [campaign_id, date]}
  daily_totals: {step: totals, primary_key: [date]}
```

`$name` in a step is a bound parameter: the value of the config input `name` (`spec.config`, after `--config`,
`--set` and defaults). DuckDB binds the value, so it is never part of the SQL text, and secrets are never
parameters. Its type follows `spec.config`: `VARCHAR` for a string, `BIGINT` for an integer, `DOUBLE` for a number,
`BOOLEAN`, `DATE`, and for a list, a list of its item type (`VARCHAR[]` for strings). A value that is not given and
has no default is null. Test a value against a list with `x = ANY($ids)`, `list_contains($ids, x)` or `x IN $ids`.
`adapt validate` reports a `$name` that is not a config input, and a list that IN or a comparison takes as one
value (`x IN ($ids)`, `x = $ids`), which would fail on every row. With `client: {type: string}` in `spec.config`:

```yaml
transform:
  mode: page
  steps:
    - name: campaigns
      select: |
        SELECT $client       AS client,
               record->>'id' AS campaign_id
        FROM raw_campaigns
```

- A step's columns are its fields, with JSON Schema types from their SQL types (DATE and TIMESTAMP as ISO text,
  timestamps with time zones in UTC, DECIMAL as numbers, INTERVAL as seconds, JSON as data, NaN and infinities as
  null). Column names are letters, digits and `_`, and differ in more than case (DuckDB and most warehouses ignore
  it). Column aliases can be reused in the same `SELECT` (`round(spend / nullif(clicks, 0), 4) AS cpc`). A missing
  key is null; read text with `->>` (a JSON value cast to text keeps its quotes).
- Inputs reach the query only as columns and `$name` parameters, never as SQL text: `{{ references }}` are not
  allowed in a step.
- `adapt validate` checks the keys and forms, names, page-mode rules and the references of `export.step`, `from:`
  and `from_stream`. `adapt validate` and `adapt run` also compile every step with DuckDB before anything is
  fetched, against empty request tables and earlier steps, with typed placeholders for `$name` parameters: unknown
  tables, columns or parameters, reading a later step, cycles, names that clash with DuckDB's built-in tables and
  views, and export keys, `cursor_field`, `from:` fields and `from_stream` fields that are not columns are reported
  with file and line.
- DuckDB runs embedded and locked down: each step is one SELECT statement that reads only the stream's request
  tables and earlier steps (and its own `WITH` queries, which have plain names that are not those of DuckDB's tables
  and views, such as `pg_settings`, or of the tables the step can read) and the table functions `range`,
  `generate_series`, `unnest`, `json_each` and `json_tree`; no files, network, extensions, settings or logs; 1 thread
  and 1 GB of memory, spilling to a private temporary folder that is removed after the run. In `page` mode the steps
  of one page get 60 seconds, and each step makes at most 1,000,000 rows; in `run` mode each step gets 10 minutes.
- In `page` mode a step that fails (e.g. a cast) fails its window like a failed request (`on_partition_error`). In
  `run` mode it fails the run, naming the stream and the step (`stream 'x': step 'y': ...`). `TRY_CAST` turns values
  that do not convert into null.
- These limits are best effort inside the process: DuckDB checks the time between batches of rows, and counts only
  the memory of its own buffers. A platform that runs customers' sources runs each connector run in its own
  container, with hard memory, CPU, disk and time limits.

Common shapes, and the transforms of `fields` they replace (`fields`, the YAML field list that SQL replaced, is the
git tag `fields-dsl`):

| Shape | SQL |
|---|---|
| a nested value as text (`from`, `type: string`) | `record->>'$.campaign.id' AS campaign_id` |
| a number with a default (`type: integer`, `default`) | `coalesce((record->>'$.metrics.clicks')::BIGINT, 0) AS clicks` |
| micros to money (`currency`) | `round((record->>'$.metrics.cost_micros')::BIGINT * 0.000001, 2) AS cost` (exact) |
| a mapping (`enum`) | `CASE record->>'status' WHEN 'ENABLED' THEN 'active' WHEN 'PAUSED' THEN 'paused' END AS status` (other values: null) |
| a date from a date-time (`type: date`) | `(record->>'start_date_time')::TIMESTAMP::DATE AS start_date` |
| a ratio of other columns (`derive: ratio`) | `round(clicks / nullif(impressions, 0), 6) AS ctr` |
| conditions (`derive: case`) | `CASE WHEN clicks > 100 THEN 'high' WHEN clicks > 0 THEN 'low' ELSE 'none' END AS bucket` |
| a partition value (`value`) | `partition->>'account_id' AS account_id` |
| a config value (`value`) | `$currency AS currency`: a `$name` parameter, typed by `spec.config` |
| a list or an object (`type: array`, `object`) | `(record->'$.geo.countries')::VARCHAR[] AS countries`, `(record->'targeting')::MAP(VARCHAR, JSON) AS targeting` |
| a value that may not convert (`on_record_error: skip`) | `TRY_CAST(record->>'id' AS BIGINT) AS id`: null instead of failing the page |

- Money: multiplying by a decimal (`* 0.000001`) keeps the arithmetic in DECIMAL, which is exact, and `round` rounds
  half away from zero. DOUBLE arithmetic carries binary errors (`round(1.005::DOUBLE, 2)` is 1.0).
- A JSON column (`record->'x'`) can hold any JSON value, so its schema is open and dlt types it from the values
  (an object becomes columns and child tables). Cast it to keep an object or a list as one column: a MAP, STRUCT
  or list column has an `object` or `array` schema and loads as JSON.

#### Across streams

Steps see only the records their own stream read in this run. Joins across streams (daily performance with each
campaign's settings), joins across sources and totals over full history belong in the warehouse after loading: SQL
over the tables that `--output duckdb:`, `ducklake:` or `dlt:` loaded, or dbt models. For example, after
`adapt run examples/sources/ads/google_ads --output duckdb:warehouse.duckdb`:

```sql
SELECT p.customer_id, p.campaign_id, p.date, p.cost, c.advertising_channel_type, c.bidding_strategy_type
FROM google_ads.campaign_performance p
LEFT JOIN google_ads.campaigns c USING (customer_id, campaign_id)
```

For incremental streams, keep the date and the partition in each export's grain: each run writes whole days for each
partition again, and loaders merge them by `primary_key`. The previous design, in which steps read other streams'
exports, is the git tag `transform-cross-stream`.

### `export`

`export` maps export names to steps of the stream; a stream needs at least one. Each export is one output: a Singer
stream, a file, or a table in DuckDB, DuckLake or a dlt destination.

| Key | Meaning |
|---|---|
| `step` | the step whose rows are written: a step of this stream, not a request (for the records as returned, a step `SELECT record FROM <request>`) |
| `primary_key` | optional; columns of the step, for de-duplication and merges downstream |
| `description` | optional text |

- Export names are unique in the source (names ignore case). An export can have its own stream's name, or the name
  of one of its stream's steps or requests, but not another stream's name. By convention, a stream with one export
  names it after the stream.
- In `run` mode an export's `primary_key` must be unique and not null, or the run fails naming the export, and rows
  are written ordered by the key. In `page` mode keys are not checked across pages; loaders merge on them.
- Every stream that runs writes all of its exports. `--stream` and the client settings' `streams` take stream or
  export names (an export name selects its stream); the selected streams run with their `from_stream` parents, which
  write their exports too.
- A stream that is a `from_stream` parent has exactly one export: its rows give the children's partitions.
- Exports are outputs only: no step reads them, in their own stream or another.

### Removed keys

Keys of earlier versions of the format are errors, each with a hint:

| Removed | Use instead |
|---|---|
| `request` | `requests` with one named item |
| stream-level `select`, `raw: true` | `transform` steps; for the records as returned, a step `SELECT record FROM <request>` |
| stream-level `primary_key` | `primary_key` on the export |
| stream-level `paginator`, `records` | `paginator` and `records` on the request item |
| `transform_mode` | `transform.mode` |
| `export: {}` (a stream that writes nothing) | at least one export |
| a stream without `requests` (SQL over other streams' exports) | joins across streams in the warehouse ([Across streams](#across-streams)) |
| top-level `models`, a `models/` folder | `transform` steps in a stream |
| `fields`, `on_record_error` | `transform` steps; `TRY_CAST` for values that may not convert |

### Errors, retries and rate limits

`retry` (`codes`: status codes or provider error codes, `max_attempts`, `backoff`, `max_delay`) and `rate_limit`
(`requests` per period) are set in `http` or per stream. SDK connectors map their errors onto the same policy: they
retry their APIs' throttling and temporary errors themselves, and `retry.codes` adds provider error codes; a streamed
SDK response that fails after records were read is not retried. The `rate_limit` in `http` is shared by all of the
source's streams; a stream's own `rate_limit` replaces it for that stream. When retries run out the stream fails,
unless the stream sets `on_partition_error: skip`: then the partition is skipped, and the stream is incomplete (see
[Execution semantics](#execution-semantics)).

### Client settings and secrets

One source folder serves every client. What differs per client is a small settings file, passed with `--config`, and
the client's secrets:

```yaml
# Acme's settings for google_ads (clients/acme/google_ads.yaml); never secrets
config:                                      # values for spec.config
  login_customer_id: "1234567890"
  customer_ids: ["1112223333", "4445556666"]
  start_date: 2025-01-01
streams: [campaigns, campaign_performance]   # optional: stream or export names; by default every stream runs
```

`--set NAME=VALUE` overrides a value and `--stream NAME` replaces `streams` (e.g. to run metadata streams daily and
performance streams hourly). Secrets come from `--secrets FILE` or `ADAPT_SECRET_<NAME>` variables, which a scheduler
fills from a secret store, so source folders and client settings can be kept in Git. Config values also reach SQL
steps (`$name`) and file names (`{{ config.<name> }}`); secrets reach neither.

### Output

`adapt run google_ads --config clients/acme/google_ads.yaml --secrets secrets.yaml --state state.json` writes
newline-delimited SCHEMA / RECORD / STATE messages to stdout, compatible with Singer targets: one Singer stream per
export, with a schema from the types of its step's columns.

`--output jsonl:DIR`, `csv:DIR`, `tsv:DIR` or `parquet:DIR` writes local files instead: one file per export, by
default `<export>.<date>.<time>.<unique>.jsonl` (or `.csv`, `.tsv`, `.parquet`), and `state.json`, all renamed into
place when the run succeeds.
`--file-name TEMPLATE` names each export's file instead, with `{{ export }}`, `{{ source }}` (the source's name),
`{{ today }}` (YYYY-MM-DD), `{{ timestamp }}` (the run's start in UTC, YYYYMMDDTHHMMSSZ) and `{{ config.<name> }}`
(values after `--config` and `--set`; never secrets). For a source with a `client` config input set to `acme`,
`--file-name "{{ config.client }}/{{ export }}_{{ today }}.jsonl"` writes the export `campaigns` to
`DIR/acme/campaigns_2026-10-04.jsonl`. The result is a relative path inside `DIR` (not absolute, no `..`, not
leading outside `DIR` through symbolic links; folders are made), every export gets its own path, and a file with the
same name is replaced; `state.json` keeps its name, so a path is not `state.json` and does not start with a folder of
that name (in any case), and a folder `DIR/state.json` stops any run. A template with other references, or that does
not give such paths, stops the run (exit status 2) before any request; a symbolic link that leads a path outside
`DIR` by the time the files are renamed into place fails the run, and no file is written.
`--file-name` works only with these four file outputs, and gives the whole path, extension included.

`tsv` files hold the values of `csv` files (a header row; a null is an empty field, a list is comma-joined, a mapping
is JSON), tab-separated; a field with a tab, a quote or a line break is quoted (the csv module's `excel-tab`
dialect). `parquet` files keep the column types of each export's step: `DECIMAL(p,s)`, `DATE`,
`TIMESTAMP WITH TIME ZONE`, `BIGINT`, `BOOLEAN`, `DOUBLE`, `VARCHAR` and so on. Other types are stored as the DuckDB
writer stores them (`INTERVAL` as seconds, `UUID`, `ENUM` and `BLOB` as text), with two changes: `HUGEINT` is
`DECIMAL(38,0)`, which stays exact, and nested values (lists, structs, maps, `JSON`) are JSON text.
Parquet records are staged on disk while the run goes, and DuckDB writes the zstd-compressed files when it succeeds.

`--output dlt:DESTINATION[:DATASET]` loads the run into any [dlt](https://dlthub.com/docs) destination (DuckDB,
BigQuery, Snowflake, Postgres, files, ...) when it succeeds: one table per export written by the run, typed by its
columns and merged on `primary_key` (without one, the exports of full-refresh streams replace their table and those
of incremental streams are appended), with the run's state saved in, and always read from, the destination.
`--output duckdb:PATH[:SCHEMA]` or `--output ducklake:CATALOG[:SCHEMA]` loads the run into a DuckDB database file or
DuckLake catalog with the same table-per-export shape, exact DuckDB column types, and the state committed in the same
transaction as the data.
For these outputs, the unkeyed exports of a full-refresh stream that skipped partitions keep their last complete
table, with a warning.

When an output is written, it logs one line per export on `adapt.output`, such as
`wrote out/acme/campaigns_2026-10-04.parquet: 29 records, 6.1 KB` or
`loaded acme_google.campaigns: 29 rows (merge on customer_id, campaign_id)`. A failed run writes no files and loads
no tables.

### Logging and run summary

Logs go to stderr, through Python's standard logging: named loggers and standard levels. `adapt.source` logs the
run's progress at `INFO` (each stream's start and end, each window or partition read, reads that keep paging every 30
seconds or 100 pages, the run's end) and warnings. `adapt.network` logs one line per HTTP request or SDK response,
and the waits for rate limits, at `INFO`, retries at `WARNING`, and headers and bodies at `DEBUG`. `adapt.output` logs
what the output wrote. SDKs log on their own loggers, which connectors name (`network_loggers`) and `adapt connectors`
lists; adapt never turns them on. By default the `adapt` loggers are at `INFO`, `adapt.network` is at `WARNING`, and
every other logger is at `WARNING`.

`adapt run` and `adapt validate` take `--log-level LEVEL` (the `adapt` loggers), `--log NAME=LEVEL` (any logger, e.g.
`--log adapt.network=INFO` or `--log google.ads.googleads.client=DEBUG`; `root` for every logger), `--log-format`
(`text`, the default, or `json`: one object per line, with each line's fields such as `stream`, `partition`,
`window`, `request`, `records` and `duration_ms`), `--log-config FILE` (a `logging.config.dictConfig` file in YAML or
JSON, whose handlers replace adapt's; handlers that exist stay open, and `disable_existing_loggers` never disables
the `adapt` loggers or the ones `--log` names) and `--log-max-chars N` (longer messages are cut; default 20000).
Every line is redacted (see [Security](#security)).

`adapt run --summary FILE` writes the run summary as JSON, atomically, whatever the outcome: `status` (`ok` or
`failed`), `started_at`, `finished_at`, `duration_s`, `source`, `streams` (each stream's partitions, failed
partitions, windows, pages, requests per request, retries, records read, records written per export and duration),
`outputs` (one entry per export: its records, and its file and size or its table), `state` (the bookmarks) and, when
the run failed, `error` (redacted and cut as log lines are).

## Security

1. YAML cannot name Python modules or classes. Components are registered through Python entry points:
   connectors (auth providers and the SDK calls they allow, group `adapt.connectors`) and query builders (group
   `adapt.query_builders`); paginators are built in. Sdk requests name API services and methods, and each connector
   allows only read-only ones. Records are shaped by SQL that runs in a locked-down DuckDB (see `transform`).
2. Platforms allow-list components (`adapt run --allow-connector NAME` and `adapt validate --allow-connector NAME`, for
   connectors and query builders alike, e.g. `google_ads` and `gaql`), and each connector runs in its own environment.
3. `{{ secrets.* }}` is only allowed inside `auth` (a validation error elsewhere), so secrets cannot leak into URLs,
   parameters or output. Secret values, and the tokens a run obtains (OAuth access, refresh and ID tokens, and the
   tokens connectors register with `context.secret()`), are redacted from errors and from every log line: adapt's and
   the SDKs', through every handler, `--log-config` ones included. The values of headers, URL parameters and form
   fields named like credentials (`Authorization`, `Cookie`, `sig`, and names containing `token`, `key`, `secret`,
   `password`, `signature` or `credential`) are masked too, and the bodies of token responses are not logged. SDK
   request logs stay off unless the operator names their loggers (`--log`).
4. Inputs never become SQL text: `{{ }}` references are not allowed in steps, which read partition and config values
   as JSON columns and config values as `$name` parameters, bound by DuckDB and typed by `spec.config`. Secrets are
   never parameters or columns.
5. Custom logic is a connector: versioned, reviewed and run in the sandboxed worker, not inline YAML.
6. Files, objects and databases are read through allow-listed connectors too (`files`, `s3`, `gcs`, `postgres`;
   `--allow-connector files` and so on); there is no `https` reader. The boundary is in `auth`: `roots` for files and
   objects (every path, and every file or key a glob, a `match` or a listing selects, is checked to be inside a root
   before anything is read, and the connector's DuckDB connection can reach nothing else) and the `dsn` for a database.
   `files` reads local folders only, with no extension, no credentials and external access off. Credentials and DSNs
   come from `{{ secrets.* }}` only (a literal credential is refused) and are redacted, the DSN with its password.
   `s3` and `gcs` refuse any `?` or `%` in a URL, so a query string such as `?s3_endpoint=` cannot send signed
   requests elsewhere, and check the scheme, the bucket and the prefix exactly. `postgres` attaches the database
   through a temporary DuckDB secret, so its DSN is not shown, and refuses DuckDB's system and catalog views. All
   only read: files and objects are never written, and a database is attached `READ_ONLY` and takes one SELECT per
   request, its values bound.

## Execution semantics

```text
order the selected streams and their from_stream parents: parents first, ties in source order
check every stream (SQL, connectors, query builders) before the first request
for each stream, in that order:
  partitions = its partition items crossed (from_stream: the values in the rows of the parent's export)
  if transform.mode is page:
    for each partition and window:
      for each page of its one request:
        the page's records are the request's table; run the steps in order; write each export's rows
      emit STATE after the window
  if transform.mode is run:
    run its requests and steps in dependency order:
      a request: for each partition (and window, if it uses the window), each request partition and page
                 (a request partition with batch_size: one per list of up to N of its values)
      a step: once, over all the rows read before it
    check each export's primary key, then write each export's rows, ordered by the key
    emit STATE for the partitions that completed
  if it skipped partitions, or a from_stream parent is incomplete: mark its exports partial
```

Every stream that runs writes all of its exports, and a stream waits only for its `from_stream` parents. Streams
without `incremental` save no bookmarks. With `on_partition_error: skip`, a partition that fails is skipped, and the
next run reads it again from its last saved bookmark; in `run` mode its rows are left out of the steps. A stream that
skipped partitions is incomplete, and so are its `from_stream` children, which run with the partitions they get.
For an incomplete full-refresh stream, DuckDB, DuckLake and dlt keep the last complete table of each unkeyed export.

## Legacy kinds → source format

| Legacy | Source format |
|---|---|
| `authorization` file, `initializer` + `callable` | `auth` block (built-in type or connector provider) |
| connector `client` (`from_authorizer`, `instance`) | a request's `sdk` or `http` |
| `method` + `arguments` | a `requests` item |
| `query_builder` + `sql_filter` + `format_as` | a query builder component, e.g. `gaql` with typed `where` items |
| `external_input` / `auth_input` | `{{ config.x }}` / `{{ secrets.x }}`, declared in `spec`; `$x` in SQL steps |
| `reserved_store_input`, `post_processor` | removed; connectors return plain records |
| serializer `inline` / `derived` / `constants` | the stream's `transform` steps: columns read from `record`, computed, or taken from `partition` or `$name` config parameters |
| `object` + `from` | a JSON path: `record->>'$.campaign.name'` |
| `ignore` blocks for nulls | `coalesce(...)`, `TRY_CAST(...)` |
| `extended_array` | a request's `records.explode` |
| `export` | the stream's `export`, written by `--output` (`--file-name` names files) |
| `pipeline` with `forward_to` | implicit: requests → transform steps → exports → output |

There are no external users of the legacy kinds, so there is no migration tool: the example sources are rewritten
by hand, and custom `callable` / `instance` usage becomes a component.

## Legacy problems this removes

| Legacy problem | Source format |
|---|---|
| inputs are pasted into query strings (the legacy engine now rejects unsafe values) | typed, escaped `where` items; bound `$name` parameters in SQL |
| YAML can import and call any module (the legacy engine has an opt-in allowlist) | registered components only |
| credentials on the command line / anywhere in YAML | `spec.secrets`, scoped to `auth`, redacted |
| one API call per run | partitions × windows × pages × async jobs |
| no incremental state | `incremental` with checkpoints and lookback |
| `##IGNORE##` can reach the API | `skip_if_empty` on typed `where` items |
| null handling needs an `ignore` block per field | `coalesce` in SQL |
| inline / derived / constants run in a fixed order; `derived` can't see `extended_array` children | SQL steps over the (exploded) records |
| four files wired by namespace and `forward_to` | one source: a folder with a file per stream |

## Rollout

1. Done: spec (`adapt-core/source/source_spec.py`, first in the removed `adapt-utils`), `adapt validate` support
   and the JSON Schema; the
   examples above are validated in CI from `examples/sources/`.
2. Done: the runtime for HTTP sources is the `adapt-core` package (`adapt run`, see
   `adapt-core/README.md`): built-in auth, partitions, paginators, incremental state, retries and rate limits,
   record shaping, Singer output and local writers. HTTP sources run end to end in the tests, against a
   local fake API. Incremental windows are whole days for now.
3. Done: connectors `google_ads` (`adapt-google-ads`: sdk + gaql), `microsoft_ads` (`adapt-microsoft-ads`: sdk +
   async reports) and `facebook_ads` (`adapt-facebook-ads`: sdk), with async jobs in the runtime. Query builders
   became components later (decision 9): `gaql` ships with adapt-google-ads.
   The three ad example sources run end to end in the tests, against local fakes of the APIs and the real SDKs.
4. In progress: run the three ad sources against the live APIs, then remove the legacy kinds. Google Ads ran against a
   live account on 2026-10-03: the `campaigns` stream matched the legacy connector + serializer on all 29 campaigns
   (the account had no ad traffic, so `campaign_performance` returned no rows). Microsoft Ads ran against a live account
   the same day: web-app sign-in, `GetCampaignsByAccountId` (3 campaigns) and the report job (submit, poll) work; the
   account had no traffic in the past year, so the reports had no file to download. Facebook Ads ran against a live
   account the same day: its daily campaign insights since 2023-09-03 (36 rows, 9 campaigns) add up exactly to the
   account's lifetime impressions, clicks and spend. The metadata streams
   ran against the same accounts: Google 25 ad groups, 86 keywords, 18 location and 10 audience targets; Microsoft 3
   campaigns, 14 ad groups, 1 keyword and 2 location targets through the account hierarchy (no audiences); Facebook
   105 campaigns and 107 ad sets with their targeting.
5. Done (2026-10-04): SQL shaping (decision 10). Every example stream had a `select` (replaced by `transform` steps
   in item 8). On the live Google, Microsoft and Facebook accounts their records were the same as with `fields`
   (Facebook's times are now in UTC), and Facebook's daily insights for December 2024 matched the API's own totals.
   Two changes by design: money from micros is exact decimal arithmetic (`fields` rounded binary values: 2,675,000
   micros gave 2.67, now 2.68), and a status outside an example's mapping is null instead of failing the run.
6. Done (2026-10-04): transform streams replaced the removed SQL chain files (decision 11), and Python 3.10+ with
   DuckDB 1.5.2+ (decision 12). The examples showed each form: Google's and Facebook's `daily_report` (no request;
   three and two steps over two streams' exports, removed in item 8) and Microsoft's `ad_group_tree` (named requests,
   request partitions from a step). On the live accounts, Facebook's December 2024 totals matched its insights, and
   Microsoft's tree had the same 14 ad groups as `ad_groups`.
7. Done (2026-10-04): the DuckDB and DuckLake writer (decision 13), with table loading, schema evolution and state in
   one transaction. Transform exports use the same table shape.
8. Done (2026-10-04): one stream form and self-contained streams; cross-stream SQL removed (tag
   transform-cross-stream). Every stream has named `requests`, `transform: {mode, steps}` and at least one `export`,
   and the keys of the other forms are errors with a hint ([Removed keys](#removed-keys)). Steps read only their own
   stream's requests and earlier steps (decision 11), so the `daily_report` streams were removed: their joins belong
   in the warehouse. Steps read config values as `$name` parameters (decision 15), and `--file-name` names output
   files (decision 16). The examples keep their columns and keys.
9. Done (2026-10-04): TSV and Parquet files (decision 17), logging on Python's standard model (decision 18), and
   progress lines and the run summary (decision 19). Every output logs what it wrote, and the connectors name their SDK
   loggers and mask the tokens their SDKs get. Tests run full network logging with known secrets and tokens and check
   that none is logged.
10. Done (2026-10-05): file, object storage and database sources as reader connectors (decisions 21 and 22), with
    `sdk` requests and no change to the core: `files` (`adapt-files`) reads local files, by a glob or a `match`
    regex; `s3` (`adapt-s3`) and `gcs` (`adapt-gcs`) read object storage through `httpfs`; and `postgres`
    (`adapt-postgres`) runs read-only queries. A first version read object storage in `files` too; it was split
    the same day, so `files` never reaches the network, and no `https` reader was kept. The examples `files_demo`
    (offline, its files committed), `s3_demo` and `gcs_demo` (against a recording DuckDB stand-in for the buckets)
    and `postgres_demo` (against a local DuckDB stand-in for Postgres) run end to end in the tests;
    `ADAPT_TEST_PG_DSN`, `ADAPT_TEST_S3` and `ADAPT_TEST_GCS` add tests against a real database and real buckets.
11. Done (2026-10-05): the legacy kinds were removed (decision 6), with their packages (`adapt-utils`,
    `adapt-connector`, `adapt-serializer`, `adapt-pipeline`), configs, JSON Schemas (`docs/schemas/v1`) and tests.
    The helper modules the source format uses (YAML loading, `adapt validate`, source files, the spec, the exporter's
    atomic files) moved into `adapt-core`, which now also installs `adapt validate`. The last legacy code is at the
    git tag `legacy-pipeline-v0.0.1`.

## Decisions

Accepted on 2026-10-03 (the recommendations from the review). They can be revisited until the phase 2 runtime work
starts.

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Output protocol | Singer-compatible messages; an Airbyte adapter later | Works with existing Singer targets on day one |
| 2 | Template language | Restricted `{{ }}` references with a fixed set of filters, not sandboxed Jinja | Every reference can be checked statically |
| 3 | Where transforms run | In flight: each stream's SQL `transform` steps shape what its own requests read, per page (`mode: page`) or over the run's records (`mode: run`); joins across streams or sources and totals over full history run in the warehouse after loading (`select` replaced `fields` on 2026-10-04; later that day `transform` steps replaced `select` and `raw: true`, and steps stopped reading other streams) | Serves both transform-in-flight and load-then-transform users; in flight, a stream needs only what it read |
| 4 | File layout | A source folder: `source.yaml` plus one file per stream in `streams/`, read as one source; a single file still works for small sources (decided later on 2026-10-03, replacing "one file per source") | Ad sources have dozens of entities (campaigns, ad groups, keywords, targeting, ads, performance); a file per stream keeps files short and reviews focused, while the folder keeps one sign-in, one state and `from_stream` across files, and is validated as a whole |
| 5 | Connector packaging | One connector per vendor SDK, installed per connector environment | Isolates SDK dependency conflicts (protobuf, grpc, suds) |
| 6 | Legacy kinds | One format: remove them once the runtime runs the four example sources (decided later on 2026-10-03, replacing "keep the v1 pipeline kind") | Nobody depends on them yet; one format halves the docs, checks and tests |
| 7 | Loading into warehouses | dlt as an optional loader (`adapt-core[dlt]`, `--output dlt:...`), not a rebuild of the runtime on dlt (decided later on 2026-10-03) | dlt brings destinations, merges and schema evolution; its `rest_api` covers neither SDK APIs nor async reports, its Python configs are not a safe customer contract, and it needs Python 3.10+ |
| 8 | Per-client settings | A `--config` file per client and source (`config` values and the `streams` to run), never secrets; secrets come from a secret store at run time (decided later on 2026-10-03) | One source folder serves every client, so clients differ only in data that a platform can store, generate and review |
| 9 | Query builders | Components in the `adapt.query_builders` entry-point group, usable from `http` and `sdk` requests; `gaql` ships with adapt-google-ads, and `sql_where` (unused) is removed. `adapt validate` checks the references inside builder calls; `adapt validate` and `adapt run` add the builders' own checks (decided later on 2026-10-03, replacing built-in `gaql` / `sql_where`) | A query language is vendor knowledge: in the core, every change to it needed a core release, and each new vendor would add its own. As components, each builder evolves with its connector, HTTP sources can use them too (e.g. a SOQL builder), and the core keeps only what every builder shares: finding calls, rendering typed values and allow-lists |
| 10 | SQL shaping | SQL `transform` steps, DuckDB queries over the stream's request tables, are how a stream shapes its records; the records as returned are a step `SELECT record FROM <request>`. `fields`, the YAML field list SQL replaced, is removed (its last version is the git tag `fields-dsl`), and DuckDB is a dependency of adapt-core (decided later on 2026-10-03; `fields` removed on 2026-10-04; the stream-level `select` and `raw: true` replaced by steps later on 2026-10-04) | Customers write standard SQL instead of learning a transform language, with all of DuckDB's functions. It is fast (one query per page, inputs loaded as one JSON array), typed statically (`DESCRIBE` gives the columns before any data) and safe to host (inputs are columns and bound parameters, never SQL text; DuckDB runs locked down). One way to shape records halves the docs, checks and tests |
| 11 | Chained transformations | Steps within one stream: a stream has named `requests`, named SQL `transform` steps (each a table for later steps, `mode: page` or `run`) and named `export`s, and a step reads only its own stream's request tables and earlier steps. Joins across streams or sources and totals over full history run in the warehouse after loading (decided on 2026-10-04: models were replaced that day by transform streams whose steps also read other streams' exports, and those cross-stream reads, transform-only streams and internal streams were removed later that day; the git tag `transform-cross-stream` has them) | A stream's output depends only on what it read, so each stream is selected, scheduled, retried and checked on its own, with its own state; no stream waits for, holds back or skips another. Data an API gives in several calls (an account's campaigns and their ad groups) still joins in one stream with several requests. The warehouse has every run's rows, so joins and totals there are complete, where a run sees only the days it read |
| 12 | Python and DuckDB versions | Python 3.10+ for adapt-core and the connectors, with DuckDB 1.5.2+ (decided on 2026-10-04, replacing 3.9+ and DuckDB 1.4+) | Python 3.9 is past its end of life; DuckDB 1.4, the newest for 3.9, could not spill a run's tables to disk in a test where 1.5 did; dlt and DuckLake need them anyway |
| 13 | Loading into DuckDB and DuckLake | `--output duckdb:PATH[:SCHEMA]` loads into a DuckDB database file, and `--output ducklake:CATALOG[:SCHEMA]` loads into a DuckLake catalog file. Each export is one table in the schema (default: the source name). Records are staged locally, then all tables and `SCHEMA._adapt_state` are committed in one transaction. Tables are created on first load, new columns are added, type changes fail, file names that clash with the schema are rejected, and keyed tables merge while unkeyed full-refresh outputs replace and unkeyed incremental outputs append with a warning. Partial full-refresh outputs keep their last complete table (decided on 2026-10-04) | DuckDB and DuckLake need no dlt, so dlt stays the loader for other warehouses. The writer keeps exact DuckDB types from the export steps, saves state atomically with data, and lets DuckLake expose Parquet data plus a catalog for full-history and cross-source transforms |
| 14 | Export side effects | Every stream that runs writes all of its exports, including `from_stream` parents that run only for their children; a stream needs at least one export, and selecting an export selects its stream (decided on 2026-10-04; `export: {}`, which made a stream internal, was removed with cross-stream reads later that day) | Output is predictable: what runs is written, and a parent's export holds the values its children were partitioned by. Without cross-stream reads, a stream that writes nothing has no use |
| 15 | Config values in SQL | `$name` in a step is a DuckDB parameter bound to the config input `name` and typed by `spec.config`; secrets are never parameters, `{{ }}` references stay out of SQL, and `adapt validate` reports a `$name` that is not a config input (decided on 2026-10-04) | Rows often need a client's values, such as its name or currency. A bound value cannot change the query, and its type is known before any data, so steps still compile and are checked before a run. SQL needs no template syntax, and the `config` JSON column remains for other uses |
| 16 | Output file names | `--file-name TEMPLATE` for `--output jsonl:DIR` and `csv:DIR`, with `{{ export }}`, `{{ source }}`, `{{ today }}`, `{{ timestamp }}` and `{{ config.<name> }}`: a relative path inside `DIR`, subfolders allowed, different for every export and checked before any request; the default names do not change (decided on 2026-10-04) | Downstream jobs find files by client, export and date, which the unique default names do not give. The template uses the same `{{ }}` references as sources, without secrets, and the checks keep every file inside `DIR` and apart from the others |
| 17 | TSV and Parquet files | `--output tsv:DIR` writes the values of csv files, tab-separated (the csv module's `excel-tab` dialect), and `--output parquet:DIR` one Parquet file per export, typed by its step's columns as the DuckDB writer stores them, except `HUGEINT` as `DECIMAL(38,0)` and nested values as JSON text, compressed with zstd. Both name their files like the other file outputs (`--file-name` included) and write `state.json`; Parquet records are staged on disk, and DuckDB writes the files when the run succeeds (decided on 2026-10-04) | Spreadsheets and bulk loaders read TSV, and tabs are rarer in values than commas. Parquet keeps exact types (decimals, dates, timestamps with time zones), and data lakes and warehouses load it directly; `DECIMAL(38,0)` keeps `HUGEINT` values exact where DuckDB would write a `DOUBLE`, and JSON text in a plain string column is what every Parquet reader can read. Staging keeps memory flat for large exports, a failed run writes nothing, and the DuckDB writer's types keep files and tables alike |
| 18 | Logging | Python's standard logging model: named loggers (`adapt.source` for progress, `adapt.network` for API calls, `adapt.output` for what was written, and the SDKs' own loggers by their names) and the standard levels, set with `--log-level` and `--log NAME=LEVEL`; text or JSON lines on stderr (`--log-format`), or the handlers of a `logging.config.dictConfig` file (`--log-config`). Every line of every logger, through every handler, is redacted (secrets, the tokens a run obtains, credentials by name) and cut at `--log-max-chars`. Connectors name their SDK loggers (`network_loggers`), and adapt never turns them on (decided on 2026-10-04, replacing ad-hoc SDK logging setup such as google-ads' `basicConfig` snippet, and the fixed `[adapt] LEVEL message` lines) | Operators already know loggers and levels, and log platforms take JSON lines or a handler of their own, so there is nothing new to learn and no custom modes to keep. One setup covers every logger, so an SDK's payload logs are redacted like adapt's own lines, and they are on only when an operator asks for them |
| 19 | Progress and run summary | `adapt.source` logs each stream's start and end, each window or partition read, reads that keep paging (every 30 seconds or 100 pages) and the run's end; `--summary FILE` writes the run summary as JSON (status, times, each stream's counts, the outputs written, the state and the error), atomically, whatever the outcome. The counts come from one metrics object per run, fed by the runner, the HTTP client and the connector context (decided on 2026-10-04) | Long runs show that they are moving, and where; schedulers get the outcome and the counts without parsing logs. One metrics object, without global state, keeps the log lines and the summary in agreement and is simple to test |
| 20 | Batched request partitions | `batch_size: N` on a request partition (`{name, values}`, `{name, from, field}`, or `{from, fields}` with exactly one field that is not a stream partition): one request per list of up to `N` distinct values, which the name holds, e.g. GAQL `campaign.id IN (...)`; a step's values in a partitioned stream are batched only with `{from, fields}` listing the stream partition fields, so each stream partition lists only its own; not on stream partitions, and windows and state stay per stream partition. `adapt connectors` lists only the installed connectors and their SDK loggers; query builders such as `gaql` are still used and checked by `adapt validate` (decided on 2026-10-05) | APIs that filter by a list read a large account in a few calls (1000 campaigns' ad groups in 5 GAQL queries, not 1000), within the query's limits, without a new request type. Stream partitions stay single values, so bookmarks do not change. `adapt connectors` answers which connectors and SDK loggers are installed |
| 21 | File and object storage sources | Files and objects are read by reader connectors, not a request kind of the core, one connector per kind of storage: `files` (`adapt-files`) reads local folders only (`auth: {provider: files, roots: [...]}`; no URLs, no `httpfs`, no credentials, external access always off), and `s3` (`adapt-s3`) and `gcs` (`adapt-gcs`) read `s3://` and `gs://` prefixes through DuckDB's `httpfs` (`auth: {provider: s3, roots, key_id, secret, ...}`, or `provider: gcs`; credentials from secrets only). Requests (`method: read`) read csv, tsv, json, jsonl and parquet with DuckDB's readers, each row one record, picked by a path, a glob or a list, or by a `match` regex fully matched below a folder (`recursive` for sub-folders). Every path, and every file or key a glob, a regex or a listing selects, is checked inside a root before anything is read; `s3` and `gcs` refuse any `?` or `%` in a URL and check scheme, bucket and prefix exactly. There is no `https` reader (decided on 2026-10-05, replacing a first version in which `files` also read `s3://`, `gs://` and `https://` roots with credentials blocks) | A connector keeps the core unchanged and each reader optional, allow-listed (`--allow-connector files`) and versioned like the ad APIs, and partitions, windows, `batch_size`, retries and steps work as they are. Split connectors keep local reading free of network access and credentials, and give each object store only its own scheme and settings. DuckDB already shapes records, reads every format and streams large files in bounded memory; refusing `?` and `%` closes httpfs' URL settings (`?s3_endpoint=` would send signed requests to another host), and the roots in `auth` give deployments one place to decide what can be read. A regex picks files a glob cannot (date folders, no `_SUCCESS` markers) |
| 22 | Database sources | PostgreSQL is read by the `postgres` connector (`adapt-postgres`), not a request kind of the core: `auth: {provider: postgres, dsn: "{{ secrets.* }}"}` and `sdk: postgres` requests, `method: query` (one SELECT in DuckDB's SQL over the attached database, with `$name` values bound from `params`) or `method: table`. The database is attached `READ_ONLY` through DuckDB's postgres scanner and a temporary DuckDB secret holding the DSN; write keywords, other statements, functions that run text of their own and DuckDB's system and catalog views (`duckdb_*`, `pragma_*`, `system`, `pg_catalog`, `information_schema`, `SHOW`, `DESCRIBE`, `SUMMARIZE`) are refused before a query runs, and an unconstrained `numeric` is read as exact text (decided on 2026-10-05) | The same connector model as files and the ad APIs, with no new driver: DuckDB's scanner pushes filters down, pages large results and binds values, a list included (`= ANY($ids)` for `batch_size`). Read-only is enforced three times (the query check, the attach and a locked connection), so a write fails even when the role could write. Attaching through a secret keeps the DSN out of DuckDB's views, and refusing them keeps queries to the database's own tables; the DSN stays a redacted secret |
{% endraw %}
