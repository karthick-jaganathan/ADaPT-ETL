---
layout: default
title: Command line
parent: adapt-core
nav_order: 2
permalink: /adapt-core/cli/
---

# The `adapt` command line

How to run and validate a source with `adapt run` and `adapt validate`: options, output files and their names, source folders with client settings, and run behaviour.

## Run a source

`adapt run` runs one source and writes its records.

```bash
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files > files_demo.singer.jsonl
export ADAPT_SECRET_DEVELOPER_TOKEN=... ADAPT_SECRET_CLIENT_ID=... ADAPT_SECRET_CLIENT_SECRET=... ADAPT_SECRET_REFRESH_TOKEN=...
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 > google_ads.singer.jsonl
```

`SOURCE` is a source folder (or its `source.yaml`), or a single-file source.

### Options

**Inputs and state**

| Option | Meaning |
|---|---|
| `--config FILE` | a client's settings for the source (YAML/JSON): `config` values for `spec.config`, and the `streams` or exports to run (see below) |
| `--set NAME=VALUE` | one `spec.config` value (repeatable; lists as `a,b,c`); overrides `--config` |
| `--secrets FILE` | YAML/JSON values for `spec.secrets` (warns if other users can read it); overrides `ADAPT_SECRET_<NAME>` environment variables |
| `--state FILE` | state from a previous run (JSON, or a Singer STATE message) |
| `--timezone TZ` | the client's IANA time zone (e.g. `America/New_York`) for `today` and incremental windows, so a daily run reads the client's local day; default: the machine's local date |

**What runs**

| Option | Meaning |
|---|---|
| `--stream NAME` | run only this stream, or the stream of this export (repeatable); replaces the `--config` file's `streams`; the streams its `from_stream` partitions come from run too, and every stream that runs writes all its exports |
| `--allow-connector NAME` | allow only these connectors (repeatable); by default any installed connector can be used |
{: .nowrap-first }

**Where records go**

| Option | Meaning |
|---|---|
| `--output` | where records go: `singer` (default) or a target from the table below |
| `--file-name TEMPLATE` | with `--output jsonl:DIR`, `csv:DIR`, `tsv:DIR` or `parquet:DIR`: each export's file, a path inside `DIR` made from {% raw %}`{{ export }}`{% endraw %}, {% raw %}`{{ source }}`{% endraw %}, {% raw %}`{{ today }}`{% endraw %}, {% raw %}`{{ timestamp }}`{% endraw %} and {% raw %}`{{ config.NAME }}`{% endraw %} (see below) |
| `--summary FILE` | also write the run summary as JSON to `FILE`, whatever the outcome (see [Run summary]({{ site.baseurl }}/adapt-core/logging/#run-summary)) |
{: .nowrap-first }

| `--output` value | Result |
|---|---|
| `singer` (default) | Singer SCHEMA / RECORD / STATE messages on stdout |
| `jsonl:DIR`, `csv:DIR`, `tsv:DIR` or `parquet:DIR` | one file per export plus `state.json`, written atomically (see [Output files](#output-files)) |
| `duckdb:PATH[:SCHEMA]` or `ducklake:CATALOG[:SCHEMA]` | load into DuckDB or DuckLake |
| `dlt:DESTINATION[:DATASET]` | load into a dlt destination |

See [Outputs]({{ site.baseurl }}/adapt-core/outputs/) for both kinds of load.

**Logging**

| Option | Meaning |
|---|---|
| `--log-level LEVEL` | the level of adapt's loggers: `DEBUG`, `INFO` (default), `WARNING`, `ERROR` or `CRITICAL` |
| `--log NAME=LEVEL` | the level of any logger by its name (repeatable), e.g. `adapt.network=INFO` for a line per API call; `root` is the root logger |
| `--log-format FORMAT` | `text` (default) or `json`: one JSON object per line |
| `--log-config FILE` | a Python logging configuration (YAML or JSON) for handlers and formatters of your own; not with `--log-format` |
| `--log-max-chars N` | cut log messages longer than `N` characters (default 20000; `0`: never) |

### Inputs and secrets

- Inputs are converted to the types declared in `spec`:
  - `list` accepts `a,b,c`;
  - `date` accepts `YYYY-MM-DD`, `today` or `-30d`.
- `ADAPT_SECRET_<NAME>` variables are read only for the secrets the source declares:
  - names match case-insensitively;
  - variables meant for other sources are ignored;
  - surrounding whitespace is removed from secret values.

### Logs

- Logs go to stderr (see [Logging, progress and run summary]({{ site.baseurl }}/adapt-core/logging/#logging-progress-and-run-summary)).
- Secret values are replaced with `***` in every line — including OAuth access tokens, their URL-encoded forms and
  tracebacks.

### Exit status

| Status | Meaning |
|---|---|
| `0` | success |
| `1` | the run failed |
| `2` | invalid source, inputs, stream names, `--file-name`, logging options or `--summary` path, or a connector that is missing, not allowed or does not support a request (nothing is fetched) |
| `130` | interrupted |

When a run fails or is interrupted, `--output` directories get no files.

### Output files

`--output jsonl:DIR`, `csv:DIR`, `tsv:DIR` and `parquet:DIR` write one file per export, plus `state.json` with the
bookmarks of incremental streams.

- Each file is written as a hidden temporary file in `DIR`.
- It is renamed into place when the run succeeds.
- A run that fails writes no files.

| Format | Each export's file |
|---|---|
| `jsonl` | one JSON object per line, one per record |
| `csv` | a header row of the export's columns, then one row per record, comma-separated (the csv module's `excel` dialect) |
| `tsv` | the same values as `csv`, tab-separated (the `excel-tab` dialect) |
| `parquet` | one row per record, with the export step's column types (below), compressed with zstd |

#### `csv` and `tsv`

- A null is an empty field.
- A list is comma-joined (`a,b`); a mapping is JSON.
- A field that holds the separator, a quote, a carriage return or a newline is quoted, with its quotes doubled.
- Rows end with CRLF.
- Read `tsv` files with a CSV reader (e.g. Python's `csv.reader(f, dialect="excel-tab")`), not by splitting lines on
  tabs.

#### `parquet`

- Records are staged in a private temporary folder while the run goes, so memory stays flat for large exports.
- DuckDB writes the files when the run succeeds.
- Columns are typed from the export step:

| Column type in the export step | Parquet column |
|---|---|
| `BIGINT` and the other integer types, `DOUBLE`, `FLOAT`, `DECIMAL(p,s)`, `BOOLEAN`, `DATE`, `TIME`, `TIMESTAMP`, `TIMESTAMP WITH TIME ZONE` | the same type |
| `TIMESTAMP_S`, `TIMESTAMP_MS`, `TIMESTAMP_NS` | `TIMESTAMP` |
| `HUGEINT`, `UHUGEINT` | `DECIMAL(38,0)`, exact; a value of more than 38 digits fails the run |
| lists, arrays, structs, maps, unions, `JSON` | `VARCHAR`: the value as JSON text |
| `INTERVAL` | `DOUBLE`: seconds |
| `UUID`, `ENUM`, `BLOB`, `BIT` and other types | `VARCHAR` |

A step `SELECT record FROM raw_campaigns` gives one `VARCHAR` column, `record`, with each record as JSON text.

### File names

By default the files are named `<export>.<date>.<time>.<unique>.jsonl` (or `.csv`, `.tsv`, `.parquet`).

`--file-name TEMPLATE` names the export files instead: a relative path inside `DIR`, extension included, made from:

| Reference | Value |
|---|---|
| {% raw %}`{{ export }}`{% endraw %} | the export |
| {% raw %}`{{ source }}`{% endraw %} | the source's name |
| {% raw %}`{{ today }}`{% endraw %} | YYYY-MM-DD |
| {% raw %}`{{ timestamp }}`{% endraw %} | the run's start in UTC, YYYYMMDDTHHMMSSZ |
| {% raw %}`{{ config.NAME }}`{% endraw %} | config values, never secrets |

{% raw %}
```bash
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files --output jsonl:out \
  --file-name "{{ source }}/{{ export }}_{{ today }}.jsonl"     # out/files_demo/orders_2026-10-04.jsonl, ...
adapt run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data \
  --allow-connector files --output parquet:out \
  --file-name "{{ source }}/{{ export }}.parquet"               # out/files_demo/orders.parquet, ...
```
{% endraw %}

**Paths**

- Folders are made as needed.
- Absolute paths and `..` are not allowed.
- Every export needs its own path (use {% raw %}`{{ export }}`{% endraw %}).
- A file with the same name is replaced.

**`state.json`**

- `state.json` keeps its name, so a path cannot be `state.json` or start with a folder of that name (in any case).
- A folder `DIR/state.json` stops any run.

**Config values**

- For a source with a `client` config input, {% raw %}`--file-name "{{ config.client }}/{{ export }}_{{ today }}.jsonl"`{% endraw %} keeps
  each client's files in a folder of their own.
- A config input the template uses must have a value.

**Checks**

- Other references, paths outside `DIR` (also through symbolic links) and two exports with one path exit with status
  2 before any request.
- Links are checked again before the files are renamed into place. If one leads outside `DIR` by then, the run fails
  and writes no file.

## Validate sources

`adapt validate [PATH ...]` checks sources without running them.

- A source folder is checked as one source.
- Directories are searched recursively.
- It also runs the checks of the installed connectors and query builders.
- Without a PATH, `$ADAPT_CONFIGS` is checked.

```bash
adapt validate examples                                   # every example source, recursively
adapt validate --strict examples/sources/ads/google_ads   # warnings fail too
```

| Option | Meaning |
|---|---|
| `--kind source` | the kind to assume for files that do not declare `kind` |
| `--allow-connector NAME` | only allow these connectors in `auth.provider` and `sdk` (repeatable) |
| `--strict` | exit with status 1 on warnings too |
| `--format text\|json\|github` | output format; `github` prints GitHub Actions annotations |
| `--export-schema DIR` | write `source.schema.json` and `stream.schema.json` to `DIR`, then exit |
| `--log-level`, `--log`, `--log-format`, `--log-config`, `--log-max-chars` | the same logging options as `adapt run` (see [Logging]({{ site.baseurl }}/adapt-core/logging/)) |

| Exit status | Meaning |
|---|---|
| `0` | no errors |
| `1` | errors (or warnings with `--strict`) |
| `2` | usage error |

## Source folders and client settings

One **source folder** is shared by every client — it describes the *data*, not any one client. A client's own settings
live in a separate **`--config` file** (which never holds secrets), so the same source serves everyone.

```text
sources/google_ads/            the source — shared by every client
├── source.yaml                kind, name, spec, auth, http
└── streams/                   one file per stream, named after it
    ├── campaigns.yaml
    └── campaign_performance.yaml
clients/acme/google_ads.yaml   Acme's settings — config values, no secrets
```

The config file supplies the `spec.config` values and, optionally, which streams to run:

```yaml
config:
  customer_ids: ["1112223333", "4445556666"]
streams: [campaigns, campaign_performance]   # optional — by default every stream runs
```

```bash
adapt run sources/google_ads --config clients/acme/google_ads.yaml --secrets acme-secrets.yaml
adapt run sources/google_ads --config clients/acme/google_ads.yaml --stream campaign_performance   # just one stream
```

Good to know:

- A folder is **one source**: one sign-in, one state file, and a stream can be the `from_stream` parent of a stream in
  another file. Streams run in file-name order, parents first.
- A stream file holds a stream's keys (without `name`). `adapt validate sources/` checks the whole folder, reporting
  each finding in the file it belongs to.
- Running a single stream file directly prints the correct folder command instead.

## Behaviour

How `adapt run` handles auth, retries, rate limits, pagination, incremental state, parent streams and errors.

### Auth

- `oauth2_refresh_token`: the token is fetched once, and refreshed on expiry or after a 401.
- `api_key`: in a header or the query.
- `bearer`.
- `basic`.

### Retries

These are retried:

- status codes in `retry.codes` (default 429 and 5xx);
- connection errors, timeouts and broken responses.

How:

- up to `max_attempts` (default 3);
- with exponential or constant backoff;
- honouring `Retry-After`, capped by `max_delay`.

Other request errors are not retried.

### Rate limits

- `rate_limit: {requests, per}` is a sliding window.
- Requests wait instead of failing.
- The limit in `http` is shared by all of the source's streams.
- A stream's own `rate_limit` replaces it for that stream.

### Pagination

A request's `paginator` decides when paging stops:

| Paginator | Stops |
|---|---|
| `offset`, `page_number` | at a short or empty page |
| `cursor` | when the token or next URL is missing |

- Next URLs may be relative to the previous request.
- A page that repeats the previous one, or a token or URL that repeats, stops the run instead of looping.

### Incremental

**Windows**

- `window` and `lookback` are whole days; `window` is at least `1d`.
- A stream with `incremental` needs a request that uses {% raw %}`{{ window.start }}`{% endraw %} or {% raw %}`{{ window.end }}`{% endraw %}.
- Windows run from `start` to today.

**Bookmarks**

- A bookmark is saved per stream and partition: after every window in `page` mode, after the exports in `run` mode.
- It is the last complete day: at most yesterday, since today is still changing.
- It never moves back.

**Resuming**

- The next run resumes the day after the bookmark.
- It re-reads `lookback` days, but not before `start`.
- A bookmark older than `start` wins, so no days are skipped.

### Parents

A parent stream's export gives its child streams their partitions.

- `{name, from_stream, field}` makes one partition per distinct value of a column of the parent's export.
- `{from_stream, fields: [account_id, campaign_id]}` makes one per distinct combination of several columns, named
  after them (`partition.account_id`, `partition.campaign_id`).
- This keeps hierarchies together: campaigns, then their ad groups, then their keywords.
- The parent has exactly one export.
- Rows missing a value give no partition.
- The parent runs first, like any stream: it writes its export and keeps its own state.
- Its children get partitions from the rows it writes in that run. If it skipped partitions, its children are
  incomplete too.

### Errors

`on_partition_error: skip` continues with the next partition when a request fails, or a step in `page` mode.

In `run` mode:

- the partition's rows are left out of the steps;
- a step that fails stops the run, naming the stream and the step.
