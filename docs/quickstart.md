---
layout: default
title: Quickstart
nav_order: 2
description: "Install, validate and run your first StreamWright source in five minutes"
permalink: /quickstart/
---

# Quickstart
{: .no_toc }

Install StreamWright, then validate and run an example source — offline, no credentials — in about five minutes.
{: .fs-5 .fw-300 }

<details open markdown="block">
  <summary>Contents</summary>
{: .text-delta }
1. TOC
{:toc}
</details>

---

## 1. Install

```bash
git clone https://github.com/karthick-jaganathan/streamwright.git
cd streamwright
make install                 # streamwright: the `streamwright` CLI
make install-connectors      # the reader + ad connectors (or e.g. `make install-files`)
streamwright connectors             # list what's installed
```

`make install MODE=dev` installs in editable mode. See [Installation]({{ site.baseurl }}/installation/) for Docker and
other options.

## 2. Run a source offline

`files_demo` reads CSV, JSON Lines and Parquet files committed in its own `data/` folder, so it runs with no network
and no credentials:

```bash
streamwright run examples/sources/readers/files_demo \
  --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files \
  --output jsonl:out/
```

You'll see one JSON Lines file per export under `out/`, plus a `state.json`. Records go to **stdout as Singer
messages** if you omit `--output`; logs always go to stderr with secret values redacted.

{: .note }
> `--allow-connector files` restricts the run to the `files` connector — nothing else installed can be loaded.

## 3. Validate before you run

`streamwright validate` checks sources without running them and reports the file, line and YAML path of every finding:

```bash
streamwright validate examples/sources                        # every example + the installed connectors' checks
streamwright validate --strict examples/sources/ads/google_ads
streamwright validate --format json examples/sources          # machine-readable
```

Exit status is `0` on success, `1` if the run failed, `2` for an invalid source or inputs.

## 4. Run against a real API

Sources are shared by every client; **secrets come only from `--secrets FILE` or `STREAMWRIGHT_SECRET_<NAME>`** and are never
written in the source. For Google Ads:

```bash
streamwright run examples/sources/ads/google_ads \
  --set customer_ids=1112223333 \
  --secrets ~/.streamwright/google-secrets.yaml \
  --output duckdb:warehouse.duckdb
```

The same source serves every client via a per-client config file:

```bash
streamwright run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml --output duckdb:acme.duckdb
```

See [Command line]({{ site.baseurl }}/core/cli/) for every option and [Outputs]({{ site.baseurl }}/core/outputs/)
for DuckDB, DuckLake and dlt.

## 5. Orchestrate it (optional)

To run a source for many users and networks on a schedule, use the Dagster-based
[orchestration]({{ site.baseurl }}/orchestration/): one shared pipeline runs across Google, Microsoft and Facebook,
locally, in Docker, or as Kubernetes Jobs.

## Where to next

- [Concepts]({{ site.baseurl }}/concepts/) — the vocabulary: sources, streams, connectors, pipelines, networks.
- [Examples]({{ site.baseurl }}/examples/) — task-oriented recipes for every example source.
- [streamwright]({{ site.baseurl }}/core/) — the full CLI and runtime reference.
- [Architecture]({{ site.baseurl }}/architecture/) — how the pieces fit, with diagrams.
