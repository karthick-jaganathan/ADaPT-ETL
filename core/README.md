# StreamWright — Declarative Data Pipelines

**StreamWright turns a few YAML files into a working data pipeline.** You describe what to pull—from an API, a file store, or a database—and StreamWright handles the how: authentication, pagination, retries, shaping data with DuckDB SQL, and writing it to your destination.

Build reusable data pipelines without writing per-connector code. Define your sources declaratively, install connectors on demand, and run everything through a single CLI command.

## How It Works

The `streamwright` package provides both the CLI tooling and execution runtime for declarative data sources.

Each stream follows a three-stage pipeline:

1. **Fetch** — Retrieve records through declarative REST HTTP requests, storage readers (S3, GCS, and local files), database integrations, or vendor SDK connectors.
2. **Shape** — Filter, transform, and normalize records on the fly using embedded DuckDB SQL.
3. **Write** — Deliver data to JSONL, CSV, TSV, and Parquet files; DuckDB and DuckLake; Singer-compatible message streams; or supported `dlt` destinations.

## CLI Commands

- **`streamwright run`** — Execute a source and stream records to the configured destination.
- **`streamwright validate`** — Validate source definitions, authentication specifications, and schemas without executing network requests.
- **`streamwright connectors`** — Browse, inspect, and install connectors from the StreamWright Hub.

## Source Structure

A source is defined declaratively as a directory containing a `source.yaml` file and a `streams/` directory, or as a standalone YAML file where supported.

```text
my_source/
├── source.yaml
└── streams/
    ├── customers.yaml
    └── orders.yaml
```

- **`source.yaml`** — Defines input parameters (`spec`), authentication (`auth`), and base HTTP configuration (`http`).
- **`streams/*.yaml`** — Defines stream endpoints, pagination, rate limits, schemas, and stream-specific configuration.

## Installation

Install StreamWright using pip:

```bash
pip install streamwright
streamwright --help
```

This installs the CLI, execution runtime, and connector hub client. Install additional connectors on demand:

```bash
# Browse available connectors
streamwright connectors list

# Install the generic REST API connector
streamwright connectors install restapi

# Install file and storage readers
streamwright connectors install files

# Install Google Ads API support
streamwright connectors install google_ads

# Install multiple connectors at once
streamwright connectors install google_ads meta_ads microsoft_ads
```

The connector catalog is discovered from the live [StreamWright Hub](https://github.com/karthick-jaganathan/streamwright-hub) and cached locally in `~/.streamwright/connectors.json`.

## Quickstart

The repository includes runnable examples to help you get started.

```bash
# Install StreamWright and the required connector
pip install streamwright
streamwright connectors install files

# Clone the repository
git clone https://github.com/karthick-jaganathan/streamwright.git

# Validate the example sources
streamwright validate streamwright/examples

# Run the file-reader example
streamwright run streamwright/examples/sources/readers/files_demo \
  --set data_root=streamwright/examples/sources/readers/files_demo/data \
  --allow-connector files \
  --output jsonl:out
```

## Documentation

- **Full documentation:** https://streamwright.web.app/docs/
- **Architecture Guide:** https://streamwright.web.app/docs/architecture/
- **Connectors & Readers:** https://streamwright.web.app/docs/connectors/
