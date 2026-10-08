---
layout: default
title: Outputs
parent: streamwright
nav_order: 4
permalink: /core/outputs/
---

# Outputs: DuckDB, DuckLake and dlt

Loading a run into DuckDB or DuckLake (local, or on object storage with a Postgres catalog), or into any dlt destination. For Singer messages and files, see [the command line]({{ site.baseurl }}/core/cli/#output-files).

## Loading into DuckDB or DuckLake

`--output` loads a run into a DuckDB database or a DuckLake catalog, in one transaction.

| `--output` | Loads into |
|---|---|
| `duckdb:PATH[:SCHEMA]` | a DuckDB database file, created if it is missing |
| `ducklake:CATALOG[:SCHEMA]` | a DuckLake catalog file, created if it is missing |

- The schema defaults to the source's `name`, and each export becomes one table in that schema.
- `~` is expanded in `PATH`, `CATALOG` and `STREAMWRIGHT_DUCKLAKE_DATA_PATH`.

```bash
streamwright run examples/sources/ads/meta_ads --set account_ids=... --output duckdb:warehouse.duckdb
export STREAMWRIGHT_DUCKLAKE_DATA_PATH=warehouse-data
streamwright run examples/sources/ads/meta_ads --set account_ids=... --output ducklake:warehouse.ducklake:meta_ads
```

Query the result:

```bash
duckdb warehouse.duckdb "SELECT * FROM meta_ads.campaign_insights"
```

### All or nothing

- Records are staged locally while the run goes.
- Nothing reaches the destination unless the run succeeds.
- Then all tables and the state are written in one transaction.
- A failed run or load leaves the destination unchanged.

### File and schema names

- For `duckdb`, the file name without its extension cannot equal the schema, ignoring case.
- Use another file name such as `warehouse.duckdb`, or pass a schema, so DuckDB can tell the catalog from the schema.

### Column types and nulls

- Column types come from each export step's columns, as DuckDB types such as `DATE`, `BIGINT`, `DECIMAL` and
  `TIMESTAMP WITH TIME ZONE`.
- Nested values (lists, structs, maps and JSON) are JSON columns.
- `INTERVAL` is stored as seconds (`DOUBLE`).
- `BLOB`, `UUID` and `ENUM` are stored as text.
- A step `SELECT record FROM raw_campaigns` gives one JSON column, `record`.
- A missing or null value is SQL `NULL` in the table, including JSON columns.
- Nested JSON nulls inside lists or objects are kept.

### Tables and new columns

- Tables are created on the first load.
- New columns are added to existing tables.
- If a column's type changed, the load fails with the table, column, old type and new type.

### Merge, replace or append

| Export | How it is loaded |
|---|---|
| with a `primary_key` | merged on it: this run's rows replace rows with the same key, and within one run the last record with a key wins |
| without a key, from a full-refresh stream | replaces the table's rows |
| without a key, from an incremental stream | appended, with a warning |

- If a full-refresh stream skipped partitions (or its `from_stream` parent did), its unkeyed exports keep the rows of
  the last complete run instead, with a warning.
- In `run` mode keys are checked (unique, not null) before anything is written.

### State

- The state is saved in the same transaction, in `SCHEMA._streamwright_state` (one row per source).
- Every run without `--state` reads it from there.
- Use one schema per client, such as `--output duckdb:warehouse.duckdb:acme_google_ads`, to keep tables and bookmarks
  apart when runs do not overlap.

### One load at a time

- Run one load at a time per DuckDB file or DuckLake catalog file.
- DuckDB locks the file, so overlapping runs fail when they load after reading their data.
- Give parallel runs separate files, such as one file per client, or run them one after another.

### DuckLake data files and extensions

- DuckLake data files go in `STREAMWRIGHT_DUCKLAKE_DATA_PATH` when it is set, or next to the catalog by DuckLake's default.
- DuckDB's `ducklake` extension is installed on first use (once, from DuckDB's extension repository).
- So are `httpfs` and `postgres` when they are needed (see below).
- MySQL DuckLake catalogs are not supported yet.

Use dlt instead for other warehouses.

### DuckLake on object storage and a Postgres catalog

A DuckLake warehouse can keep its data files on S3 (or an S3-compatible store such as MinIO or LocalStack) and its
catalog in Postgres, which takes concurrent writers.

Both are opt-in; a local catalog file with local data files works as before.

```bash
export STREAMWRIGHT_DUCKLAKE_DATA_PATH=s3://streamwright-warehouse/u1/
export STREAMWRIGHT_DUCKLAKE_S3_ENDPOINT=localhost:4566 STREAMWRIGHT_DUCKLAKE_S3_URL_STYLE=path STREAMWRIGHT_DUCKLAKE_S3_USE_SSL=false
export STREAMWRIGHT_DUCKLAKE_S3_KEY_ID=test STREAMWRIGHT_DUCKLAKE_S3_SECRET=test       # LocalStack's
export STREAMWRIGHT_DUCKLAKE_CATALOG_SCHEMA=streamwright_lake                          # optional: the catalog's own schema
streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --allow-connector files \
  --output "ducklake:postgres:dbname=lake host=localhost user=streamwright"
```

| Variable | Meaning |
| --- | --- |
| `STREAMWRIGHT_DUCKLAKE_DATA_PATH` | where data files go: a folder, or `s3://BUCKET/PREFIX/` |
| `STREAMWRIGHT_DUCKLAKE_S3_KEY_ID`, `STREAMWRIGHT_DUCKLAKE_S3_SECRET` | S3 credentials (both or neither); `STREAMWRIGHT_DUCKLAKE_S3_SESSION_TOKEN` for temporary ones |
| `STREAMWRIGHT_DUCKLAKE_S3_REGION` | the bucket's region, e.g. `us-east-1` |
| `STREAMWRIGHT_DUCKLAKE_S3_ENDPOINT` | `host[:port]` of an S3-compatible store, without a scheme (e.g. `localhost:4566`; `host.docker.internal:4566` from a container) |
| `STREAMWRIGHT_DUCKLAKE_S3_URL_STYLE` | `path` or `vhost` |
| `STREAMWRIGHT_DUCKLAKE_S3_USE_SSL` | `true` or `false` (https or http) |
| `STREAMWRIGHT_DUCKLAKE_DATA_INLINING_ROW_LIMIT` | DuckLake's data inlining: inserts of fewer rows stay in the catalog instead of a data file; `0` turns it off. `0` by default when the data path is on S3, DuckLake's default otherwise |
| `STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD` | the Postgres catalog's password |
| `STREAMWRIGHT_DUCKLAKE_CATALOG_SCHEMA` | the schema of the catalog's own tables (DuckLake's `METADATA_SCHEMA`) |

**S3**

- S3 is set up when `STREAMWRIGHT_DUCKLAKE_DATA_PATH` starts with `s3://` or any `STREAMWRIGHT_DUCKLAKE_S3_*` variable is set.
- DuckDB's `httpfs` is loaded, and the settings become a temporary DuckDB `s3` secret, scoped to the data path's
  bucket.
- Without credentials DuckDB's defaults apply.
- Values are bound parameters, never in a query's text.
- Credentials are never logged: messages that would show one show `***`.

**Postgres catalog**

- `ducklake:postgres:DSN[:SCHEMA]` takes a libpq connection string (quote it: it has spaces); a last `:NAME` is the
  schema.
- DuckDB's `postgres` extension is loaded before the catalog is attached.
- The connection string cannot hold a password. `STREAMWRIGHT_DUCKLAKE_CATALOG_PASSWORD` gives it, as DuckDB's default
  postgres secret, out of the command line, the query and the logs.
- The first load creates the catalog's tables (in `STREAMWRIGHT_DUCKLAKE_CATALOG_SCHEMA` when it is set, so they stay apart
  from other tables of the database).

**State**

- The state is kept in the DuckLake, `SCHEMA._streamwright_state`, as with a local catalog, so a run resumes from the last
  one wherever it ran.

## Loading with dlt

`--output dlt:DESTINATION[:DATASET]` loads a run into a [dlt](https://dlthub.com/docs) destination: DuckDB, BigQuery,
Snowflake, Postgres, Redshift, Databricks, filesystem/S3 and more. Install dlt and the destination's extra:

```bash
pip install "streamwright[dlt]" "dlt[bigquery]"
export DESTINATION__BIGQUERY__CREDENTIALS__PROJECT_ID=... # or .dlt/secrets.toml: see dlt's docs for each destination
streamwright run examples/sources/ads/google_ads --set customer_ids=... --output dlt:bigquery:marketing
```

### Tables

- Each export becomes a table in the dataset (default: the source's `name`).
- Column types come from its step's columns.
- A JSON column (such as `record` from `SELECT record FROM raw_campaigns`) is typed and flattened by dlt.

### How exports are loaded

| Export | How it is loaded |
|---|---|
| with a `primary_key` | merged on it |
| without one, from a full-refresh stream | replaces its table |
| without one, from an incremental stream | appended, with a warning, since the days read again (lookback and today) are loaded again |

- If a full-refresh stream skipped partitions, its unkeyed exports keep their last complete table instead and log a
  warning.
- On the `filesystem` destination (local files, S3, GCS, Azure), exports with a `primary_key` are written as Delta
  tables, the format in which dlt can merge them: `pip install "dlt[deltalake]"`.

### Staging and state

- Records are staged locally while the source runs; nothing is loaded unless the run succeeds.
- The state is saved in the destination with the data, and every run takes it from there (each run uses a new dlt
  working folder).
- So runs into different warehouses keep separate states, and any machine can run the next load.
- `--state FILE` replaces it.
- A failed load leaves nothing behind: the next run reads again from the last completed load.

### Good to know

- dlt's usage telemetry is off unless the environment sets `RUNTIME__DLTHUB_TELEMETRY`.
- Run one load at a time per source and dataset.
