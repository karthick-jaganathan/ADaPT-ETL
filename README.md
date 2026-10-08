# StreamWright

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

- **`streamwright`** ([streamwright](core/README.md)) runs sources: built-in auth, partitions, pagination, async
  report jobs, incremental state, retries and rate limits, SQL transform steps; output as Singer messages, JSONL, CSV,
  TSV or Parquet files, DuckDB or DuckLake tables, or any [dlt](https://dlthub.com/docs) destination.
- **`streamwright validate`** checks sources without running them; every finding has its file, line
  and YAML path. JSON Schemas for editor autocomplete are in [`docs/schemas/`](docs/schemas/).
- **Connectors** add APIs that need a vendor SDK, and read files and databases:

| Connector | Package | Reads |
|---|---|---|
| `google_ads` | [streamwright-google-ads](connectors/ads/google_ads/README.md) | Google Ads (SDK, GAQL query builder) |
| `microsoft_ads` | [streamwright-microsoft-ads](connectors/ads/microsoft_ads/README.md) | Microsoft Advertising (SDK, async reports) |
| `facebook_ads` | [streamwright-facebook-ads](connectors/ads/facebook_ads/README.md) | Facebook Marketing API (SDK) |
| `files` | [streamwright-files](connectors/readers/files/README.md) | local CSV, TSV, JSON, JSONL and Parquet files |
| `s3` | [streamwright-s3](connectors/readers/s3/README.md) | Amazon S3 and S3-compatible object storage |
| `gcs` | [streamwright-gcs](connectors/readers/gcs/README.md) | Google Cloud Storage |
| `postgres` | [streamwright-postgres](connectors/readers/postgres/README.md) | PostgreSQL (read-only queries) |

See [connectors/README.md](connectors/README.md) for the connector layout and how to write one.

## Installation

```bash
git clone https://github.com/karthick-jaganathan/streamwright.git
cd streamwright
make install                # streamwright: the `streamwright` command (run, validate, connectors)
make install-connectors        # optional: all seven connectors (or e.g. make install-google-ads)
streamwright connectors               # lists the installed connectors
```

`MODE=dev` installs in editable mode. See [INSTALLATION.md](INSTALLATION.md) for every option.

## Quick start

```bash
streamwright validate examples/sources                       # exit status 1 when there are errors
streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --output jsonl:out/
streamwright run examples/sources/ads/google_ads --set customer_ids=... --secrets ~/.streamwright/google-secrets.yaml
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --output duckdb:acme.duckdb
```

`--config` takes one client's settings (`config` values and the `streams` to run, never secrets), so one source
folder serves every client. Secrets come from `--secrets FILE` or `STREAMWRIGHT_SECRET_<NAME>` environment variables, and are
redacted from every log line. Records go to stdout as Singer messages by default; `--output` writes files
(`jsonl:DIR`, `csv:DIR`, `tsv:DIR`, `parquet:DIR`), loads DuckDB or DuckLake (`duckdb:PATH`, `ducklake:CATALOG`), or,
with `pip install "streamwright[dlt]"`, a dlt destination (`dlt:bigquery:marketing`).

See the [streamwright documentation](https://karthick-jaganathan.github.io/streamwright/core/) — the [command line](https://karthick-jaganathan.github.io/streamwright/core/cli/), [outputs](https://karthick-jaganathan.github.io/streamwright/core/outputs/) and [logging and the run summary](https://karthick-jaganathan.github.io/streamwright/core/logging/) — for every option.

## Architecture

StreamWright is layered so that **what to extract** (a customer's YAML source), **how to reach an API** (a connector) and
**when and for whom to run** (orchestration) stay independent:

- **Sources** — declarative extraction (`spec`, `auth`, requests, SQL transforms, exports); no code.
- **streamwright** — the `streamwright` CLI, validation, the run engine, the DuckDB transform and the outputs.
- **Connectors** — vendor access (ad-API SDKs + file/object/db readers), each registered under `streamwright.connectors`.
- **Orchestration** — Dagster pipelines that run `streamwright` per user and network (one shared pipeline across many ad
  networks) locally, in Docker, or as Kubernetes Jobs writing to an object-storage warehouse.

The layers, the `streamwright run` flow and the orchestration flow — with sequence diagrams — are in
[docs/architecture.md](docs/architecture.md). Orchestration details are in the
[orchestration documentation](https://karthick-jaganathan.github.io/streamwright/orchestration/).

## Repository layout

```
core/          streamwright: the `streamwright` CLI, the runtime and streamwright validate
connectors/<name>/        one distribution per connector, with its own tests and README
examples/sources/        example source folders, validated in CI
docs/                  documentation site, the source format design and the JSON Schemas
orchestration/         Dagster orchestration of `streamwright run` (subprocess, Docker or Kubernetes)
tests/                 the core test suite
```

## Docker

```bash
docker compose build                                  # streamwright and the connectors
docker compose run --rm streamwright streamwright validate examples/sources
docker compose run --rm streamwright streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data
```

The orchestration image `streamwright-pipeline:local` (streamwright with the ad and reader connectors, one container per pipeline
node) is built by `bash orchestration/docker/build.sh`; see [orchestration/docker](orchestration/docker/README.md) and,
for the Dagster orchestration itself, [orchestration/README.md](orchestration/README.md).

## Development

```bash
make install MODE=dev && make install-connectors MODE=dev
pip install pytest jsonschema
make test                   # python -m pytest tests connectors -q
make validate               # streamwright validate --strict configs
```

See [docs/contributing.md](docs/contributing.md).

## License

Apache License 2.0 - see [LICENSE](LICENSE).
