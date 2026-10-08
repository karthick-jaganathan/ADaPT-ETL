---
layout: default
title: Examples
nav_order: 8
description: "The example sources of StreamWright and how to run them"
permalink: /examples/
---

# Examples

The example sources are the folders in
[`examples/sources/`](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources). Each is a
`source.yaml` plus one file per stream in `streams/`, is checked by `streamwright validate` in CI, and runs end to end in the
tests (against local fakes of the APIs, buckets and databases).

| Source | Connector | Streams |
|---|---|---|
| `files_demo` | `files` | customers, orders, products (from the files committed in its `data/`; runs offline) |
| `s3_demo` | `s3` | customers, events, orders |
| `gcs_demo` | `gcs` | events, orders |
| `postgres_demo` | `postgres` | customers, orders, order_lines |
| `google_ads` | `google_ads` | campaigns, ad groups, keywords, targets, campaign performance, ad group hierarchy |
| `microsoft_ads` | `microsoft_ads` | campaigns, ad groups, keywords, targets, campaign performance (async report), ad group tree |
| `meta_ads` | `meta_ads` | campaigns, ad sets, campaign insights |

## Validate

```bash
streamwright validate examples/sources                    # every example, with the installed connectors' checks
streamwright validate --strict examples/sources/ads/google_ads
streamwright validate --format json examples/sources      # machine-readable findings
```

## Recipe: read local files (offline)

No network, no credentials — the data is committed in the source's `data/`:

```bash
streamwright run examples/sources/readers/files_demo \
  --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files --output jsonl:out/
# → one JSONL file per export under out/, plus state.json
```

## Recipe: read from object storage (S3 / GCS)

Credentials come from `STREAMWRIGHT_SECRET_*` only and are redacted from logs:

```bash
STREAMWRIGHT_SECRET_S3_KEY_ID=AKIA... STREAMWRIGHT_SECRET_S3_SECRET=... \
  streamwright run examples/sources/readers/s3_demo \
  --set bucket_root=s3://my-bucket/exports/ --allow-connector s3 --output jsonl:out/

STREAMWRIGHT_SECRET_GCS_KEY_ID=... STREAMWRIGHT_SECRET_GCS_SECRET=... \
  streamwright run examples/sources/readers/gcs_demo \
  --set bucket_root=gs://my-bucket/exports/ --allow-connector gcs --output jsonl:out/
```

## Recipe: read from PostgreSQL

```bash
STREAMWRIGHT_SECRET_PG_PASSWORD='...' \
  streamwright run examples/sources/readers/postgres_demo \
  --set pg_host=db.example.com --allow-connector postgres --output jsonl:out/
```

## Recipe: extract from an ad platform

SDK connectors handle auth, partitions, pagination and async report jobs:

```bash
streamwright run examples/sources/ads/google_ads   --set customer_ids=...                    --secrets ~/.streamwright/google-secrets.yaml
streamwright run examples/sources/ads/microsoft_ads --set account_ids=... --set customer_id=... --secrets ~/.streamwright/microsoft.yaml
streamwright run examples/sources/ads/meta_ads --set account_ids=...                     --secrets ~/.streamwright/meta-secrets.yaml
```

## Recipe: load into a warehouse

```bash
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --output duckdb:acme.duckdb
pip install "streamwright[dlt]" "dlt[bigquery]"
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --output dlt:bigquery:marketing
```

See [Outputs]({{ site.baseurl }}/core/outputs/) for DuckDB, DuckLake (object storage) and dlt.

## Recipe: run one pipeline across networks

Orchestration runs the same `metadata` pipeline against Google, Microsoft and Meta — the
[shared vocabulary]({{ site.baseurl }}/concepts/#network--shared-vocabulary) maps each network's stream names:

```bash
cd orchestration
python -m streamwright.orchestration.definitions metadata u1    # google_ads account
python -m streamwright.orchestration.definitions metadata u2    # microsoft_ads account
```

See [Orchestration → Running]({{ site.baseurl }}/orchestration/running/).

## A source folder

`examples/sources/readers/files_demo/source.yaml`: the inputs and the connector that reads the files.

```yaml
kind: source
name: files_demo
description: Customers, orders and products from local files.
spec:
  config:
    data_root: {type: string, description: "The folder the files are read from (relative to the working directory)"}
    start_date: {type: date, default: "2026-10-01", description: First day of customers' daily files}

auth:
  provider: files                      # registered by the streamwright-files connector
  roots: ["{{ config.data_root }}"]    # the only folders files can be read from
```

`examples/sources/readers/files_demo/streams/orders.yaml`: one stream - a request, SQL steps that shape its records, and an
export.

```yaml
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

The [source format design](https://github.com/karthick-jaganathan/streamwright/blob/master/docs/design/source-format.md)
walks through the ad platform examples: HTTP and SDK requests, partitions, pagination, async report jobs, incremental
streams and query builders.
