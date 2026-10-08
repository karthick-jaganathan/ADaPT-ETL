---
layout: default
title: streamwright
nav_order: 6
has_children: true
permalink: /core/
---

# streamwright — the `streamwright` CLI and runtime for `kind: source`

streamwright is the `streamwright` command and the runtime behind it for `kind: source`.

- `streamwright run` runs a source.
- `streamwright validate` checks it.
- `streamwright connectors` lists the installed connectors.

A source is a folder (`source.yaml` with `spec`, `auth` and `http`, and one file per stream in `streams/`) or a single
YAML file, as described in [the source format design]({{ site.baseurl }}/design/source-format/). `streamwright validate`
checks it before every run. The [architecture guide]({{ site.baseurl }}/architecture/) shows how it fits with the
connectors and [orchestration]({{ site.baseurl }}/orchestration/).

## What it does

HTTP sources run end to end, with built-in:

- auth, partitions and pagination
- async report jobs
- incremental state
- retries and rate limits
- SQL transform steps and exports

Vendor access comes from [connectors]({{ site.baseurl }}/core/connectors-and-readers/):

- the ad-API SDKs (Google, Microsoft, Facebook)
- the readers (files, S3, GCS, PostgreSQL)

## Install

```bash
make install            # from the repository root; or: cd core && make install MODE=dev
streamwright --help
```

See [Installation]({{ site.baseurl }}/installation/) for the connectors and the other packages.

## Quickstart

From the repository root:

```bash
# local files, offline (pip install ./connectors/readers/files)
streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files --output jsonl:out
# an SDK connector (pip install ./connectors/ads/google_ads); secrets from STREAMWRIGHT_SECRET_<NAME> or --secrets FILE
streamwright run examples/sources/ads/google_ads --set customer_ids=1112223333 --secrets ~/.streamwright/google-secrets.yaml \
  --output duckdb:warehouse.duckdb
# check every example source, with the installed connectors' checks
streamwright validate examples
```

- Records go to stdout as Singer messages, unless `--output` names files or a warehouse.
- Logs go to stderr. Secret values are replaced by `***` in every line.

| Exit status | Meaning |
|---|---|
| `0` | success |
| `1` | the run failed |
| `2` | invalid source, inputs or options (nothing is fetched) |
| `130` | interrupted |

## A source at a glance

A source folder is shared by every client. Each client's settings (never secrets) live in a file of their own:

```text
examples/sources/ads/google_ads/     the source
├── source.yaml                      kind, name, spec (config and secrets), auth, http
└── streams/                         one file per stream, named after it
    ├── campaigns.yaml               requests, SQL transform steps, exports
    └── campaign_performance.yaml
clients/acme/google_ads.yaml         Acme's settings: config values and the streams to run
```

```yaml
config:                                      # values for spec.config
  customer_ids: ["1112223333", "4445556666"]
streams: [campaigns, campaign_performance]   # optional: by default every stream runs
```

```bash
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --secrets acme-google-secrets.yaml
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --stream campaign_performance
```

Each stream:

1. reads its `requests` (HTTP, or a connector's `sdk` calls);
2. shapes the records with sandboxed DuckDB SQL `steps`;
3. writes the steps its `export` names.

See [Streams]({{ site.baseurl }}/core/streams/) and the [source format design]({{ site.baseurl }}/design/source-format/).

## Capabilities

| Page | What it covers |
|---|---|
| [Command line]({{ site.baseurl }}/core/cli/) | every option of `streamwright run` and `streamwright validate`; the [output files]({{ site.baseurl }}/core/cli/#output-files) and their [names]({{ site.baseurl }}/core/cli/#file-names); [source folders and client settings]({{ site.baseurl }}/core/cli/#source-folders-and-client-settings); [run behaviour]({{ site.baseurl }}/core/cli/#behaviour) (auth, retries, rate limits, pagination, incremental state, parents, errors) |
| [Streams]({{ site.baseurl }}/core/streams/) | requests, sandboxed DuckDB SQL steps and exports; the `page` and `run` modes |
| [Outputs]({{ site.baseurl }}/core/outputs/) | loading into DuckDB or DuckLake (also on object storage with a Postgres catalog), or any [dlt](https://dlthub.com/docs) destination |
| [Logging]({{ site.baseurl }}/core/logging/) | loggers and levels; [SDK logs]({{ site.baseurl }}/core/logging/#sdk-logs); logging configuration files; progress lines; the JSON [run summary]({{ site.baseurl }}/core/logging/#run-summary) |
| [Connectors & readers]({{ site.baseurl }}/core/connectors-and-readers/) | SDK connectors and query builders; async jobs; [writing a connector]({{ site.baseurl }}/core/connectors-and-readers/#writing-a-connector); reading local files, S3, GCS and PostgreSQL |
| [Python API]({{ site.baseurl }}/core/python-api/) | `validate_source`, `load_source` and `SourceRunner`: validate and run a source without the CLI |

## Related documentation

- [Architecture]({{ site.baseurl }}/architecture/) — how streamwright, the connectors and orchestration fit together,
  with sequence diagrams for the `streamwright run` and orchestration flows.
- [Source format design]({{ site.baseurl }}/design/source-format/) — the full `source.yaml` + `streams/` reference.
- [Connectors](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/README.md) — the installed
  connectors (ad APIs, file/object/db readers) and how to write one.
- [Orchestration]({{ site.baseurl }}/orchestration/) — running `streamwright` as Dagster pipelines per user and network.
- [Examples](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources) — the example sources:
  [ads](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/ads) and
  [readers](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/readers).
- [Source code](https://github.com/karthick-jaganathan/streamwright/tree/master/streamwright) — the `streamwright` package.
