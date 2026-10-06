---
layout: default
title: Packages
nav_order: 10
description: "The ADaPT packages: adapt-core and its connectors"
permalink: /packages/
has_children: true
has_toc: false
---

# ADaPT Packages

ADaPT is one core package, **adapt-core**, and optional connectors. Every connector depends on adapt-core and registers
itself under the `adapt.connectors` entry point group, so `adapt connectors` lists it once it is installed.

## adapt-core

[`adapt-core/`](https://github.com/karthick-jaganathan/ADaPT-ETL/tree/master/adapt-core) ([documentation]({{ site.baseurl }}/adapt-core/)) - the `adapt`
command (`adapt run`, `adapt validate`, `adapt connectors`) and the runtime: built-in auth,
partitions, paginators, async jobs, incremental state, retries and rate limits, SQL transform steps on an embedded
DuckDB, and the outputs (Singer, JSONL, CSV, TSV, Parquet, DuckDB, DuckLake and, with the `dlt` extra, dlt).

Its modules (`adapt.core.*`) include:

| Module | Purpose |
|---|---|
| `cli` | the `adapt` command |
| `engine.runner` | `SourceRunner`: runs a loaded source |
| `validation.engine` | `adapt validate`: YAML loading with locations, findings, the command and the JSON Schema export |
| `validation.source` | `SourceChecker`: the checks of `kind: source` |
| `validation.schema` | the source format's keys and the JSON Schemas |
| `config.loader` | finds and loads a source folder (`source.yaml` + `streams/`) or a single-file source |
| `config.reader` | `load_yaml` |
| `outputs.output`, `outputs.warehouse`, `outputs.dlt_output`, `outputs.exporter` | the outputs, and atomic file writes |
| `runtime.components` | the `Connector` and `QueryBuilder` base classes: component discovery and checks |

## Connectors

| Connector | Package | Reads |
|---|---|---|
| `google_ads` | adapt-google-ads | Google Ads (SDK, GAQL query builder) |
| `microsoft_ads` | adapt-microsoft-ads | Microsoft Advertising (SDK, async reports) |
| `facebook_ads` | adapt-facebook-ads | Facebook Marketing API (SDK) |
| `files` | adapt-files | local CSV, TSV, JSON, JSONL and Parquet files |
| `s3` | adapt-s3 | Amazon S3 and S3-compatible object storage |
| `gcs` | adapt-gcs | Google Cloud Storage |
| `postgres` | adapt-postgres | PostgreSQL (read-only queries) |

Each connector's README (`connectors/<name>/README.md`) has its options, error codes and logs; `connectors/README.md` describes
the layout and how to write a connector.

## Installation

```bash
make install                  # adapt-core
make install-connectors       # every connector; or e.g. make install-files
make install MODE=dev         # editable
```

See [Installation]({{ site.baseurl }}/installation) for details.
