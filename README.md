# ADaPT - Adaptive Data Pipeline Toolkit

**Extract data from APIs, files and databases with YAML source configurations, shape it with SQL, and write it to
files or a warehouse.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## Overview

A source is described by a folder: `source.yaml` with its inputs (`spec`), sign-in (`auth`) and HTTP defaults, and one
file per stream (campaigns, ad groups, daily performance, ...) in `streams/`. Each stream's requests fetch records,
its `transform` steps shape them with SQL (DuckDB), and its exports are written by the chosen output. The format is
described in [the source format design](docs/design/source-format.md); the examples are in
[`examples/sources/`](examples/sources/).

- **`adapt`** ([adapt-core](adapt-core/README.md)) runs sources: built-in auth, partitions, pagination, async
  report jobs, incremental state, retries and rate limits, SQL transform steps; output as Singer messages, JSONL, CSV,
  TSV or Parquet files, DuckDB or DuckLake tables, or any [dlt](https://dlthub.com/docs) destination.
- **`adapt validate`** checks sources without running them; every finding has its file, line
  and YAML path. JSON Schemas for editor autocomplete are in [`docs/schemas/`](docs/schemas/).
- **Connectors** add APIs that need a vendor SDK, and read files and databases:

| Connector | Package | Reads |
|---|---|---|
| `google_ads` | [adapt-google-ads](connectors/ads/google_ads/README.md) | Google Ads (SDK, GAQL query builder) |
| `microsoft_ads` | [adapt-microsoft-ads](connectors/ads/microsoft_ads/README.md) | Microsoft Advertising (SDK, async reports) |
| `facebook_ads` | [adapt-facebook-ads](connectors/ads/facebook_ads/README.md) | Facebook Marketing API (SDK) |
| `files` | [adapt-files](connectors/readers/files/README.md) | local CSV, TSV, JSON, JSONL and Parquet files |
| `s3` | [adapt-s3](connectors/readers/s3/README.md) | Amazon S3 and S3-compatible object storage |
| `gcs` | [adapt-gcs](connectors/readers/gcs/README.md) | Google Cloud Storage |
| `postgres` | [adapt-postgres](connectors/readers/postgres/README.md) | PostgreSQL (read-only queries) |

See [connectors/README.md](connectors/README.md) for the connector layout and how to write one.

## Installation

```bash
git clone https://github.com/karthick-jaganathan/ADaPT-ETL.git
cd ADaPT-ETL
make install                # adapt-core: the `adapt` command (run, validate, connectors)
make install-connectors        # optional: all seven connectors (or e.g. make install-google-ads)
adapt connectors               # lists the installed connectors
```

`MODE=dev` installs in editable mode. See [INSTALLATION.md](INSTALLATION.md) for every option.

## Quick start

```bash
adapt validate examples/sources                       # exit status 1 when there are errors
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --output jsonl:out/
adapt run examples/sources/ads/google_ads --set customer_ids=... --secrets ~/.adapt/google-secrets.yaml
adapt run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --output duckdb:acme.duckdb
```

`--config` takes one client's settings (`config` values and the `streams` to run, never secrets), so one source
folder serves every client. Secrets come from `--secrets FILE` or `ADAPT_SECRET_<NAME>` environment variables, and are
redacted from every log line. Records go to stdout as Singer messages by default; `--output` writes files
(`jsonl:DIR`, `csv:DIR`, `tsv:DIR`, `parquet:DIR`), loads DuckDB or DuckLake (`duckdb:PATH`, `ducklake:CATALOG`), or,
with `pip install "adapt-core[dlt]"`, a dlt destination (`dlt:bigquery:marketing`).

See the [adapt-core documentation](https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/) — the [command line](https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/cli/), [outputs](https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/outputs/) and [logging and the run summary](https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/logging/) — for every option.

## Architecture

ADaPT is layered so that **what to extract** (a customer's YAML source), **how to reach an API** (a connector) and
**when and for whom to run** (orchestration) stay independent:

- **Sources** — declarative extraction (`spec`, `auth`, requests, SQL transforms, exports); no code.
- **adapt-core** — the `adapt` CLI, validation, the run engine, the DuckDB transform and the outputs.
- **Connectors** — vendor access (ad-API SDKs + file/object/db readers), each registered under `adapt.connectors`.
- **Orchestration** — Dagster pipelines that run `adapt` per user and network (one shared pipeline across many ad
  networks) locally, in Docker, or as Kubernetes Jobs writing to an object-storage warehouse.

The layers, the `adapt run` flow and the orchestration flow — with sequence diagrams — are in
[docs/architecture.md](docs/architecture.md). Orchestration details are in the
[orchestration documentation](https://karthick-jaganathan.github.io/ADaPT-ETL/orchestration/).

## Repository layout

```
adapt-core/          adapt-core: the `adapt` CLI, the runtime and adapt validate
connectors/<name>/        one distribution per connector, with its own tests and README
examples/sources/        example source folders, validated in CI
docs/                  documentation site, the source format design and the JSON Schemas
orchestration/         Dagster orchestration of `adapt run` (subprocess, Docker or Kubernetes)
tests/                 the core test suite
```

## Docker

```bash
docker compose build                                  # adapt-core and the connectors
docker compose run --rm adapt-etl adapt validate examples/sources
docker compose run --rm adapt-etl adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data
```

The orchestration image `adapt-pipeline:local` (adapt-core with the ad and reader connectors, one container per pipeline
node) is built by `bash orchestration/docker/build.sh`; see [orchestration/docker](orchestration/docker/README.md) and,
for the Dagster orchestration itself, [orchestration/README.md](orchestration/README.md).

## Development

```bash
make install MODE=dev && make install-connectors MODE=dev
pip install pytest jsonschema
make test                   # python -m pytest tests connectors -q
make validate               # adapt validate --strict configs
```

See [docs/contributing.md](docs/contributing.md).

## License

Apache License 2.0 - see [LICENSE](LICENSE).
