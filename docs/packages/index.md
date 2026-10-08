---
layout: default
title: Packages
nav_order: 10
description: "The StreamWright packages: streamwright and its connectors"
permalink: /packages/
has_children: true
has_toc: false
---

# StreamWright Packages

StreamWright is one core package, **streamwright**, and optional connectors. Every connector depends on streamwright and registers
itself under the `streamwright.connectors` entry point group, so `streamwright connectors` lists it once it is installed.

## streamwright

[`core/`](https://github.com/karthick-jaganathan/streamwright/tree/master/streamwright) ([documentation]({{ site.baseurl }}/core/)) - the `streamwright`
command (`streamwright run`, `streamwright validate`, `streamwright connectors`) and the runtime: built-in auth,
partitions, paginators, async jobs, incremental state, retries and rate limits, SQL transform steps on an embedded
DuckDB, and the outputs (Singer, JSONL, CSV, TSV, Parquet, DuckDB, DuckLake and, with the `dlt` extra, dlt).

Its modules (`streamwright.core.*`) include:

| Module | Purpose |
|---|---|
| `cli` | the `streamwright` command |
| `engine.runner` | `SourceRunner`: runs a loaded source |
| `validation.engine` | `streamwright validate`: YAML loading with locations, findings, the command and the JSON Schema export |
| `validation.source` | `SourceChecker`: the checks of `kind: source` |
| `validation.schema` | the source format's keys and the JSON Schemas |
| `config.loader` | finds and loads a source folder (`source.yaml` + `streams/`) or a single-file source |
| `config.reader` | `load_yaml` |
| `outputs.output`, `outputs.warehouse`, `outputs.dlt_output`, `outputs.exporter` | the outputs, and atomic file writes |
| `runtime.components` | the `Connector` and `QueryBuilder` base classes: component discovery and checks |

## Connectors

| Connector | Package | Reads |
|---|---|---|
| `google_ads` | streamwright-google-ads | Google Ads (SDK, GAQL query builder) |
| `microsoft_ads` | streamwright-microsoft-ads | Microsoft Advertising (SDK, async reports) |
| `facebook_ads` | streamwright-facebook-ads | Facebook Marketing API (SDK) |
| `files` | streamwright-files | local CSV, TSV, JSON, JSONL and Parquet files |
| `s3` | streamwright-s3 | Amazon S3 and S3-compatible object storage |
| `gcs` | streamwright-gcs | Google Cloud Storage |
| `postgres` | streamwright-postgres | PostgreSQL (read-only queries) |

Each connector's README (`connectors/<name>/README.md`) has its options, error codes and logs; `connectors/README.md` describes
the layout and how to write a connector.

## Installation

```bash
make install                  # streamwright
make install-connectors       # every connector; or e.g. make install-files
make install MODE=dev         # editable
```

See [Installation]({{ site.baseurl }}/installation) for details.
