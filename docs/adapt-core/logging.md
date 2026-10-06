---
layout: default
title: Logging
parent: adapt-core
nav_order: 5
permalink: /adapt-core/logging/
---

# Logging, progress and run summary

What `adapt run` and `adapt validate` log, how to configure the loggers (SDK loggers and logging configuration files included), the progress lines and the JSON run summary.

Logs go to stderr; stdout is for Singer messages.

## Loggers

adapt uses Python's standard logging: named loggers and the standard levels (`DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`).

| Logger | `INFO` | `DEBUG` |
|---|---|---|
| `adapt.source` | the run's progress (below), summaries and warnings | internal details |
| `adapt.network` | one line per HTTP request and per response of a connector SDK call (below); also connectors' connections and the waits for rate limits. Retries are `WARNING` lines | the HTTP headers, with credentials masked, and bodies |
| `adapt.output` | what each output wrote (below) | staging details |
| SDK loggers, by their own names ([below](#sdk-logs)) | the SDK's own lines | the SDK's requests and responses |

An `adapt.network` line at `INFO` comes after its stream, request, partition and window, and holds:

- for an HTTP request: method, URL, status, bytes, time and attempt;
- for a response of a connector SDK call: connector, service and method, page, records, time and attempt.

### Default levels

| Logger | Default level |
|---|---|
| the `adapt` loggers | `INFO` |
| `adapt.network` | `WARNING` (retries and errors only) |
| every other logger (the root logger) | `WARNING` |

## Logging options

`adapt run` and `adapt validate` take the same logging options.

### `--log-level LEVEL`

- Sets the level of the `adapt` loggers.
- `adapt.network` stays at `WARNING` (or `LEVEL`, when it is higher) unless `--log` names it.

### `--log NAME=LEVEL`

- Sets the level of any logger by its name, e.g. `--log adapt.network=INFO`. Repeatable.
- `--log root=DEBUG` sets the root logger: every logger without a level of its own, other libraries' included.
- adapt never turns SDK loggers on by itself.

### `--log-format FORMAT`

| Format | Lines |
|---|---|
| `text` (default) | `[2026-10-04 18:55:43,123] INFO adapt.source: message` |
| `json` | one JSON object per line (fields below) |

A `json` line has:

- `time` (ISO 8601, UTC), `level`, `logger` and `message`;
- then the fields of adapt's lines that have them: `event` (e.g. `stream_start`, `read`, `http_request`, `sdk_call`,
  `retry`, `output`, `run_end`), `stream`, `partition`, `window`, `request`, `export`, `records`, `pages`,
  `duration_ms`, `status`, `attempt`, `bytes`, `path`, `table` and others;
- `exception` for a traceback.

### `--log-config FILE`

- A Python logging configuration (`logging.config.dictConfig`'s schema, `version: 1`) in YAML or JSON.
- For handlers and formatters of your own: files, syslog, a log platform's handler (below).
- It replaces adapt's handler, so it cannot be used with `--log-format`.
- `--log-level` and `--log` apply on top.
- Existing loggers stay enabled unless it sets `disable_existing_loggers: true` (adapt's loggers and the ones `--log`
  names always log).
- Handlers that already exist stay open; the ones the file makes are closed when the command ends.

### `--log-max-chars N`

- Log messages longer than `N` characters (bodies, SDK payloads) are cut and end with `... [truncated N chars]`.
- Default 20000; `0`: never.

### Examples

```bash
# one line per API call
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 --log adapt.network=INFO
# also each request's and response's headers and body
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 --log adapt.network=DEBUG
# JSON lines, for log platforms that read stderr
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 --log-format json
```

## Redaction

Every line of every logger is redacted, through every handler (a `--log-config` file's too). These become `***`:

- secret values;
- the tokens the run obtains (OAuth access, refresh and ID tokens, and the tokens connectors get);
- the values of headers, URL parameters and form fields named like credentials: `Authorization`,
  `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `sig`, and names containing `token`, `key`, `secret`, `password`,
  `signature` or `credential` (also after JSON-escaped separators, `\u0026` and `\u003f`).

Also:

- The bodies of token responses are not logged.
- An error shows a response body's first 300 characters only after redacting all of it.

## SDK logs

Connectors name the loggers their SDK writes its requests and responses to, and `adapt connectors` lists them:

| Connector | SDK loggers | Lines |
|---|---|---|
| `google_ads` | `google.ads.googleads.client` | `INFO`: one line per call; `DEBUG`: each request and response |
| `microsoft_ads` | `suds.client`, `suds.transport` | `DEBUG`: the SOAP messages sent and received (`suds.client`), and their HTTP requests and replies (`suds.transport`) |
| `facebook_ads` | `urllib3.connectionpool` | `DEBUG`: each request's method, URL and status (`--log adapt.network=DEBUG` adds the headers and bodies) |

Name them with `--log`. For example, google-ads' logging snippet (`logging.basicConfig()`, then the logger
`google.ads.googleads.client` at `DEBUG`) becomes:

```bash
adapt run examples/sources/ads/google_ads --config clients/acme/google_ads.yaml \
  --log adapt.network=DEBUG --log google.ads.googleads.client=DEBUG
```

SDK lines are redacted like adapt's own: the secrets and the tokens the SDK gets are `***`.

## Logging configuration files

`--log-config FILE` sets up handlers and formatters. The formatter `adapt.core.runtime.logs.JsonFormatter` writes
adapt's JSON lines:

```yaml
# logging.yaml: JSON lines in adapt.jsonl, and warnings and errors as text on stderr
version: 1
formatters:
  json:
    (): adapt.core.runtime.logs.JsonFormatter
  text:
    format: "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
handlers:
  file:
    class: logging.FileHandler
    filename: adapt.jsonl
    formatter: json
  console:
    class: logging.StreamHandler
    stream: ext://sys.stderr
    formatter: text
    level: WARNING
root:
  level: WARNING
  handlers: [file, console]
```

```bash
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 --log-config logging.yaml --log adapt.network=INFO
```

Levels the file sets for the `adapt` loggers apply unless `--log-level` or `--log` sets them.

## Progress

`adapt.source` logs the run's progress at `INFO`:

| When | Line |
|---|---|
| a stream starts | `stream 'campaigns': starting (mode page, 2 partition(s), requests: raw_campaigns)` |
| a partition, or a window of an incremental stream, is read | `stream 'campaigns', partition {"account_id": "111"}: 2 page(s), 150 record(s), 0.8 s` |
| a request keeps paging: every 30 seconds or 100 pages | `stream 'campaigns', request 'raw_campaigns': 100 page(s), 10,000 record(s) so far` |
| a stream ends | `stream 'campaigns': 180 record(s) written (campaigns: 180), 3 request(s) (raw_campaigns: 3), 0 retries, 0 failed partition(s), 1.1 s` |
| the run ends | `run finished in 1.2 s: 1 stream(s), 180 record(s) written (campaigns: 180), 3 request(s), 0 retries, 0 failed partition(s), 1 output(s) written` |

A run that fails or is interrupted ends with `run failed after ...` or `run interrupted after ...`.

### Output lines

Before the run's end, each output logs on `adapt.output` what it wrote, one line per export:

```text
wrote out/campaigns.2026-10-04.093000868707.i56w6zma.parquet: 180 records, 1.4 KB
loaded acme.campaigns: 180 rows (merge on campaign_id)
did not load acme.events: 1 row (its stream skipped partitions: the table keeps the rows of the last complete run)
wrote 'campaigns' to stdout: 180 records
```

- Files give their path, records and size.
- DuckDB, DuckLake and dlt tables give their rows and how they were loaded (merge, replace or append).
- A failed run writes no files and loads no tables, so it has no such lines.

## Run summary

`--summary FILE` also writes the run summary as JSON to `FILE` (in an existing folder), atomically, whatever the
outcome: also when the run fails, is interrupted, or cannot start because of an invalid source or inputs.

```json
{
  "status": "ok",
  "started_at": "2026-10-04T09:30:00.770Z",
  "finished_at": "2026-10-04T09:30:01.997Z",
  "duration_s": 1.227,
  "source": "google_ads",
  "streams": [
    {"name": "campaigns", "mode": "page", "partitions": 2, "failed_partitions": 0, "windows": 0, "pages": 3,
     "requests": {"raw_campaigns": 3}, "retries": 0, "records_read": 180, "exports": {"campaigns": 180},
     "duration_s": 1.135}
  ],
  "outputs": [
    {"export": "campaigns", "records": 180, "path": "out/campaigns.2026-10-04.093000868707.i56w6zma.parquet",
     "bytes": 1436}
  ],
  "state": null
}
```

- **`status`**: `ok` or `failed`.
  - A failed or interrupted run also has `error`: its error messages, redacted and cut as log lines are, whatever the
    loggers' levels and handlers.
- **`streams`**: each stream that started, with:
  - its partitions (and the `failed_partitions` it skipped);
  - the windows and pages it read;
  - its `requests` per request (HTTP requests or SDK calls, retries included);
  - `retries`;
  - `records_read` (from the API);
  - `exports` (the records written to each export).
- **`outputs`**: what the output wrote, one entry per export:
  - `export` and `records`;
  - `path` and `bytes` for files;
  - `table`, `disposition` and `primary_key` for tables;
  - `state`: where the state was saved.
  - A failed run has none, except for Singer messages, which went out while it ran.
- **`state`**: the bookmarks the run wrote last (`null` without incremental streams).
  - File and table outputs save them only when the run succeeds.
