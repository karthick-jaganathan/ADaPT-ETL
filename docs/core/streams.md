---
layout: default
title: Streams
parent: streamwright
nav_order: 3
permalink: /core/streams/
---

# Streams: requests, SQL steps and exports

A stream does three things: **fetch** records with its `requests`, **shape** them with SQL `steps`, and **write** the
result as `exports`. Here is a small HTTP stream (the illustrative `http_api` source of the
[source format design]({{ site.baseurl }}/design/source-format/#worked-examples)):

{% raw %}
```yaml
partitions:
  - {name: account_id, values: "{{ config.account_ids }}"}
requests:
  - name: raw_campaigns                    # a table for the steps: one row per record
    http: {path: "/adAccounts/{{ partition.account_id }}/adCampaigns", params: {q: search}}
    paginator: {type: offset, offset_param: start, limit_param: count, page_size: 100}
    records: {path: elements}
transform:
  mode: page                               # the steps run on each page of the stream's one request
  steps:
    - name: campaigns
      select: |
        SELECT record->>'id'                             AS campaign_id,
               partition->>'account_id'                  AS account_id,
               record->>'name'                           AS campaign_name,
               (record->>'$.dailyBudget.amount')::DOUBLE AS daily_budget
        FROM raw_campaigns
export:
  campaigns: {step: campaigns, primary_key: [campaign_id]}
```
{% endraw %}

## Requests become tables

Each request is a table named after it, one row per record. Its columns are `record` (JSON, as the API returned it),
`partition` and `config` (JSON), and `window_start`, `window_end`, `today` (DATE). Read text with `->>`; a missing key
is null. For the records exactly as returned, write `SELECT record FROM raw_campaigns`.

## SQL steps shape the records

- A step is one `SELECT` over its own stream's requests and earlier steps; each step is itself a table for later ones.
  Its columns are its fields, typed by the query (dates as ISO text, timestamps in UTC).
- `$name` binds a `spec.config` value as a typed parameter — e.g. with `client: {type: string}`,
  `SELECT $client AS client, ...`. Inputs are only columns and parameters, never SQL text
  ({% raw %}`{{ references }}`{% endraw %} are not allowed in SQL), and secrets are never parameters.
- Test a value against a list with `x = ANY($ids)`, `list_contains($ids, x)` or `x IN $ids`. (`x IN ($ids)` and
  `x = $ids` compare against the *whole* list and are reported as mistakes.)

## Exports write the result

`export` maps each output name to the step that is written: `{step, primary_key?, description?}`, at least one. Each
export is one output table or stream; its name is unique in the source. By convention the request is `raw_<entity>` and
the last step and the export are named after the stream.

## Checked before anything is fetched

`streamwright validate` and `streamwright run` compile every step up front: unknown tables, columns or `$name` parameters, reading a
later step, cycles, and export keys / `cursor_field` / `from:` fields that are not columns are all reported with file
and line.

## The DuckDB sandbox

Steps run on a locked-down embedded DuckDB:

- One `SELECT` per step, reading only its stream's tables (and its own `WITH` queries) plus the table functions
  `range`, `generate_series`, `unnest`, `json_each`, `json_tree`. No files, network, extensions or settings.
- 1 thread, 1 GB of memory, spilling to a private temp folder removed after the run. `page` mode: 60 s per page and at
  most 1,000,000 rows per step; `run` mode: 10 minutes per step. `TRY_CAST` turns bad values into null.
- These limits are best-effort inside the process. To run sources you do not trust, run each in its own container with
  hard memory, CPU, disk and time limits.

## `page` vs `run` mode

- **`page`** reads one request, with no request partitions. The steps run on each page, each export is written per
  page, and state is saved after each window.
- **`run`** can read several requests. Requests and steps run in dependency order (a request partitioned `from:` a step
  runs after it), each step once over everything the run read. Export keys must be unique and non-null, rows are
  written ordered by them, and state is saved after the exports. A request using {% raw %}`{{ window.* }}`{% endraw %}
  runs once per partition and window; others once per partition.

## Request partitions and batching (`run` mode)

A request partition is `{name, values}`, `{name, from: X, field: F}` or `{from: X, fields: [...]}`, where `X` is an
earlier request (`F` is a dotted path in its `record`) or a step (`F` is a column). `values` are built once per stream
partition — from `config`, `today` and the stream's partitions, not the window. A `from:` request yields only the
records it read under the current stream partition; to keep a step's rows to the current partition, list the partition's
name in `fields` (e.g. `{from: campaign_rows, fields: [account_id, campaign_id]}`).

`batch_size: N` makes one request per list of up to `N` distinct values (in first-seen order), with `partition.<name>`
holding that list — useful for APIs that take an `IN (...)` filter. A reference that is a whole value keeps its type, so
{% raw %}`"{{ partition.<name> }}"`{% endraw %} stays a list. On `{from: X, fields: [...]}`, exactly one field may be
non-partition (its values are batched); `batch_size` is an error otherwise, on stream partitions, and on a `from:` item
named after a stream partition. Use a batched name only where a list is valid (GAQL's `op: "="` with a list fails). The
lists are still crossed with the stream partition, and windows and state stay per stream partition.

## One stream at a time

Streams are self-contained — no step reads another stream. Join streams (or total full history) in the warehouse after
loading, e.g. with DuckDB:

```bash
streamwright run examples/sources/ads/google_ads --set customer_ids=... --output duckdb:warehouse.duckdb
duckdb warehouse.duckdb "SELECT p.*, c.advertising_channel_type FROM google_ads.campaign_performance p
  LEFT JOIN google_ads.campaigns c USING (customer_id, campaign_id)"
```

Selecting an export (`--stream NAME`) selects its stream, and the selected streams run with their `from_stream`
parents. (The earlier cross-stream form — `request`, `select`, `raw: true`, `transform_mode` — is the git tag
`transform-cross-stream`; those keys are errors now, each with a hint.)
