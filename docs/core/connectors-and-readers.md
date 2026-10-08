---
layout: default
title: Connectors & Readers
parent: streamwright
nav_order: 6
permalink: /core/connectors-and-readers/
---

# Connectors and readers

SDK-backed connectors and query builders, async jobs, writing a connector, and reading local files, object storage (S3, GCS) and PostgreSQL.

## Connectors

APIs that need a vendor SDK (gRPC, SOAP, paged SDK objects) run through connectors.

- `auth: {provider: <connector>, ...}` builds the SDK client.
- A request `{name, sdk: <connector>, service, method, arguments}` in a stream's `requests` calls it.
- Files, objects and databases are read through connectors the same way ([below](#reading-from-files-and-databases)).
- Query builders, the other kind of component, write query languages.

### Installed connectors

`streamwright connectors` lists the installed connectors, one per line with their SDK loggers
([SDK logs]({{ site.baseurl }}/core/logging/#sdk-logs)). It does not list query builders, which `streamwright run` and
`streamwright validate` still use and check:

```text
$ streamwright connectors
advertising:
  meta_ads — Meta Ads (facebook-business SDK) (SDK loggers: urllib3.connectionpool)
  google_ads — Google Ads (GAQL via the google-ads SDK) (SDK loggers: google.ads.googleads.client)
  microsoft_ads — Microsoft Advertising (Bing Ads SDK) (SDK loggers: suds.client, suds.transport)
databases:
  postgres — PostgreSQL tables, read-only
files:
  files — Local files: CSV, JSON, JSONL, Parquet, TSV
object storage:
  gcs — Google Cloud Storage objects
  s3 — Amazon S3 objects (DuckDB httpfs)
```

| Connector | Package | Calls |
|---|---|---|
| `google_ads` | [streamwright-google-ads](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/ads/google_ads/README.md) | `GoogleAdsService.search_stream` / `search` (GAQL), `CustomerService.list_accessible_customers` |
| `microsoft_ads` | [streamwright-microsoft-ads](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/ads/microsoft_ads/README.md) | read operations of the v13 SOAP services; reports as async jobs |
| `meta_ads` | [streamwright-meta-ads](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/ads/meta_ads/README.md) | `api_get` and `get_*` edges of Marketing API objects |
| `files` | [streamwright-files](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/readers/files/README.md) | `file.read`: local csv, tsv, json, jsonl and parquet files |
| `s3` | [streamwright-s3](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/readers/s3/README.md) | `object.read`: the same formats from `s3://` buckets (or an S3-compatible store), through DuckDB's httpfs |
| `gcs` | [streamwright-gcs](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/readers/gcs/README.md) | `object.read`: the same formats from `gs://` buckets (an HMAC key), through DuckDB's httpfs |
| `postgres` | [streamwright-postgres](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/readers/postgres/README.md) | `database.query` (one read-only SELECT) and `database.table` on a PostgreSQL database |

### Read-only calls

A connector allows only read-only calls; anything else stops the run before it starts (exit 2).

### Rate limits and retries

- Connector calls use the stream's `rate_limit` and `retry` (or the source's `http` ones).
- Connectors retry their APIs' throttling and temporary errors on their own.
- `retry.codes` adds provider error codes (e.g. `[117]` for Microsoft, `[RESOURCE_EXHAUSTED]` for Google).
- A streamed response that fails after records were read is not retried.

### Auth and headers

- Sources with a connector `auth.provider` cannot have `http` requests: their auth is the connector's.
- `headers` on an sdk request sets per-request headers the connector takes (its `request_headers`), e.g.
  {% raw %}`headers: {CustomerAccountId: "{{ partition.account_id }}"}`{% endraw %} for Microsoft Ads.
- An async job's `poll` uses its `submit` headers.
- Headers a connector does not take stop the run before it starts.

### Query builders

A query builder writes query text from a mapping, so inputs are never pasted into queries.

- In an `http` or `sdk` request, a mapping with a single key that is an installed query builder's name is replaced by
  the query text the builder writes from it, e.g. `query: {gaql: {select, from, where, order_by, limit}}` with
  [streamwright-google-ads](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/ads/google_ads/README.md#gaql-queries).
- References are rendered with their types first.
- The builder checks, quotes and escapes every value.
- Only calls written in the source are built: values from inputs or responses stay data.
- `--allow-connector` lists query builders too (e.g. `--allow-connector google_ads --allow-connector gaql`).

### Checks

- `streamwright run`, and `streamwright validate`, run the checks of the installed connectors and query builders before anything is
  fetched (e.g. an unknown GAQL operator, with its file and line).
- Plain `streamwright validate` checks the YAML and every reference without them.

### Async jobs

An `async_job` request submits a job, polls it until it is done, then downloads the result (or reads it with `results`).

{% raw %}
```yaml
requests:
  - name: raw_campaign_performance
    async_job:
      submit: {sdk: microsoft_ads, service: ReportingService, method: SubmitGenerateReport, arguments: {...}}
      poll:
        method: PollGenerateReport                   # same service as submit
        arguments: {ReportRequestId: "{{ submit.result }}"}
        every: 15s
        timeout: 30m
        done_when: {path: Status, equals: Success}
        fail_when: {path: Status, equals: Error}
      download: {url: "{{ poll.ReportDownloadUrl }}", format: csv, compression: zip}   # or results: {sdk request}
```
{% endraw %}

**`submit` and `poll`**

- Their responses are scopes for the job's later parts.
- A response that is not a mapping (e.g. a job ID) is `submit.result` / `poll.result`.
- `poll` runs every `every` until `done_when` matches.
- `fail_when` or `timeout` fails the request like an API error.

**`download`**

- Fetches the file without the API's credentials (report URLs are pre-signed, and their signatures are masked in logs
  and errors).
- Streams it to a temporary file.
- `csv` (empty cells are null) and `jsonl` files give one record per row.
- A `json` file is a response, read with the request's `records.path`.
- An empty or missing URL means no data.

**`results`**

- An sdk request instead of a download, read like any response.

### Writing a connector

A connector, or a query builder, is a Python class registered as an entry point:

- A connector subclasses `streamwright.core.runtime.components.Connector` and goes in the `streamwright.connectors` group.
- A query builder subclasses `streamwright.core.runtime.components.QueryBuilder` and goes in the `streamwright.query_builders` group.

```toml
[project.entry-points."streamwright.connectors"]
my_api = "my_package.connector:MyApiConnector"

[project.entry-points."streamwright.query_builders"]
my_query = "my_package.query:MyQueryBuilder"
```

#### A connector

Set `name`, `auth_required`, `auth_optional` and `request_headers` (the names a request's `headers` may use;
`request["headers"]` is present only when the source sets it). Implement:

| Method | Does |
|---|---|
| `check_request()` | reject what the connector does not support, before a run |
| `connect(auth, context)` | build the SDK client |
| `request(client, request, context)` | yield plain-data responses |
| `error(exc)` | map SDK exceptions to `ConnectorError(message, code, retryable, retry_after)` |

A connector's `check_request()` sees a builder's call as an `streamwright.core.engine.queries.BuiltQuery`.

#### A query builder

Set `name` and implement:

| Method | Returns |
|---|---|
| `check(spec)` | problems with the unrendered mapping, as `(path inside it, message)` pairs |
| `build(spec)` | the query text for the rendered mapping; raise `streamwright.core.engine.queries.QueryError` for a value it cannot write safely |

#### Calls, tokens and logs

- Wrap every API call in `context.call(...)` for rate limits and retries. streamwright counts the calls in the run's metrics
  and logs a line per response on `streamwright.network`.
- Pass the tokens the connector obtains at run time (access tokens, refreshed tokens, signatures) to
  `context.secret(value)`: from then on they are `***` in every log line and error.
- Set `network_loggers` to the names of the loggers the SDK writes its requests and responses to, e.g.
  `network_loggers = ("google.ads.googleads.client",)`.
  - `streamwright connectors` lists them for `streamwright run --log NAME=DEBUG`.
  - streamwright never turns them on by itself.
- An SDK that sends its requests with the requests library can also pass each response to
  `streamwright.core.runtime.logs.http_details(response, context.redact)` (a response hook, as meta_ads does) for the
  headers and bodies on `streamwright.network` at `DEBUG`.

#### Examples and tests

- The connectors in [connectors/](https://github.com/karthick-jaganathan/streamwright/blob/master/connectors/README.md)
  are examples, one self-contained folder each.
- Test yours offline with `streamwright.core.runtime.testing` (`FakeApi`, `MemoryOutput`, `FakeClock`).

## Reading from files and databases

Local files, object storage and PostgreSQL databases are read through connectors, like any SDK-backed API.

- The `auth` block names the connector and what it may read.
- Each request is an `sdk` request. There is no other request kind and no other option for them.
- Allow the connector like any other (`--allow-connector files`, `--allow-connector s3`, `--allow-connector gcs`,
  `--allow-connector postgres`).
- There is no `https` reader.
- All read with DuckDB, on a connection of the connector's own (never the transform sandbox).
- Each row is one record, a JSON object, in pages of at most 1,000 records.
- Steps read them with `record->>'column'` and cast.

### Files

The `files` connector reads local files only:

- no URLs, no `httpfs`, no credentials;
- its DuckDB connection never has external access.

```bash
make install-files           # from the repository root; or: pip install ./connectors/readers/files
streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --allow-connector files \
    --output jsonl:out
```

{% raw %}
```yaml
# source.yaml
auth:
  provider: files
  roots: ["{{ config.data_root }}"]    # the local folders files can be read from; no URLs, no secrets

# streams/orders.yaml
requests:
  - name: raw_orders
    sdk: files
    service: file                      # the default and only service
    method: read                       # the only method
    arguments:
      path: "orders/*.csv"             # a file, a glob or a list of them; references allowed
      format: csv                      # csv | tsv | json | jsonl | parquet | auto (the default: by extension)
      options: {header: true, filename: true}
      on_missing: skip                 # skip (the default: a warning) | error, when nothing matches the path
```
{% endraw %}

**Roots**

- `roots` is the boundary.
- Every path, relative to the first root or absolute inside any root, is checked to be inside a root before any file
  is read (once symbolic links are followed).
- The connector's DuckDB connection can read nothing else.
- Deployments set the roots, through `config` or the source.

**Paths**

- `path` can use {% raw %}`{{ config.* }}`{% endraw %}, {% raw %}`{{ partition.* }}`{% endraw %} and {% raw %}`{{ window.start }}`{% endraw %} / {% raw %}`{{ window.end }}`{% endraw %} (e.g. one file
  per day: {% raw %}`customers/{{ window.start }}.jsonl`{% endraw %}).
- A whole reference to a list (a `batch_size` partition of file names) reads each file of the list.
- Partitions, windows, `batch_size` and `records.explode` work as for any request.

**Picking files by regex: `match`**

- `match` picks files by a regex instead of a glob. `path` is then one folder inside a root.
- The files whose path relative to it fully matches `match` (a literal Python regex, `re.fullmatch`) are read, sorted
  by that relative path.
- `recursive: true` looks in sub-folders too (default `false`), whose names the relative path then has, separated by
  `/`.
- Each file it selects is checked to be inside a root before any file is read:

  ```yaml
      arguments:
        path: orders                                   # a folder, not a glob
        match: '(.*/)?orders_\d{4}-\d{2}-\d{2}\.csv'   # daily files at any depth
        recursive: true
  ```

**Values**

- CSV and TSV values are text unless `options.columns` types them.
- JSON values are as written.
- Parquet values keep their types (decimals exact, dates and timestamps as text).
- `filename: true` adds each file's path, relative to its root, as `filename`.

### Object storage: S3 and Google Cloud Storage

Both connectors read through DuckDB's `httpfs` extension:

- `s3` reads Amazon S3 (or an S3-compatible store such as MinIO);
- `gcs` reads Google Cloud Storage (its S3-compatible access, with an HMAC key).

Their requests are those of `files`, on `service: object`, `match` and `recursive` included:

```bash
make install-s3              # or install-gcs; from the repository root, or: pip install ./connectors/readers/s3
STREAMWRIGHT_SECRET_S3_KEY_ID=... STREAMWRIGHT_SECRET_S3_SECRET=... streamwright run examples/sources/readers/s3_demo \
    --set bucket_root=s3://my-bucket/exports/ --allow-connector s3 --output jsonl:out
```

{% raw %}
```yaml
# source.yaml
auth:
  provider: s3                         # or gcs, with gs:// roots, key_id and secret only
  roots: ["{{ config.bucket_root }}"]  # the URL prefixes objects can be read from: s3://bucket/prefix/
  key_id: "{{ secrets.s3_key_id }}"    # credentials: one secret reference each
  secret: "{{ secrets.s3_secret }}"
  region: "{{ config.region }}"        # optional settings: region, endpoint, url_style, use_ssl

# streams/events.yaml
requests:
  - name: raw_events
    sdk: s3
    service: object                    # the default and only service
    method: read
    arguments:
      path: events/                    # with match: the folder to look in
      match: 'day=\d{4}-\d{2}-\d{2}/part-\d+\.parquet'
      recursive: true
      format: parquet
```
{% endraw %}

**`auth`**

| Connector | Takes |
|---|---|
| `s3` | `roots`, `key_id`, `secret` and an optional `session_token` (each one {% raw %}`{{ secrets.* }}`{% endraw %} reference), and the settings `region`, `endpoint` (`host[:port]`), `url_style` (`vhost` or `path`) and `use_ssl` (literal values or references) |
| `gcs` | `roots` (`gs://` only), `key_id` and `secret` |

**Credentials**

- A literal credential is refused (by `streamwright validate`, and by `streamwright run` for a value that is not a secret of the
  run).
- The credentials become one temporary DuckDB secret scoped to the roots, set with bound parameters.
- They are `***` in every log line, error and `--summary`.

**No `?` and no `%` in a URL**

- httpfs reads a URL's query parameters as connection settings, so `...x.csv?s3_endpoint=` would send a signed request
  to another host.
- A root, a path or a listed key with either is refused.
- So `?` is not a glob character here (only `*`, `[ab]` and `**`).

**Containment is exact**

- The root's scheme, exactly its bucket and a key under its prefix (`s3://acme/` does not hold `s3://acme-evil/...`).
- Checked before anything is read, and by DuckDB's `allowed_directories` too.

### PostgreSQL

```bash
make install-postgres        # from the repository root; or: pip install ./connectors/readers/postgres
STREAMWRIGHT_SECRET_PG_PASSWORD='...' streamwright run examples/sources/readers/postgres_demo \
  --set pg_host=db.example.com --allow-connector postgres --output jsonl:out
```

Two forms, one or the other (not both):

- **Connection keys** — when the connection is per-environment configuration (host, database, role) and only the
  password is secret. This is what the demo uses.
- **A `dsn` secret** — when the whole connection string is one managed secret (e.g. a URL from a vault), or needs
  libpq features the keys don't cover.

{% raw %}
```yaml
# source.yaml — connection keys (the password is the only secret)
spec:
  config:
    pg_host: {type: string, default: localhost}
  secrets:
    pg_password: {type: string}
auth:
  provider: postgres
  host: "{{ config.pg_host }}"         # connection keys are config: literal text or references
  port: 5432                           # optional (default 5432)
  database: shop                       # or its alias `dbname`
  user: reader
  password: "{{ secrets.pg_password }}"  # the only credential; a secret only, redacted in logs
  sslmode: require                     # optional: disable | allow | prefer | require | verify-ca | verify-full
  options: {connect_timeout: "10", application_name: streamwright}  # optional: extra libpq parameters
  statement_timeout: 5min              # optional: Postgres' statement_timeout

# source.yaml — or one DSN secret, in their place (not alongside the keys)
auth:
  provider: postgres
  dsn: "{{ secrets.pg_dsn }}"          # a libpq URL or keyword DSN, from secrets only; redacted with its password
  statement_timeout: 5min              # optional: Postgres' statement_timeout

# streams/orders.yaml
requests:
  - name: raw_orders
    sdk: postgres
    service: database                  # the default and only service
    method: query
    arguments:
      query: |
        SELECT id, customer_id, total::VARCHAR AS total, updated_at
        FROM public.orders
        WHERE updated_at >= $since AND updated_at < $until + INTERVAL 1 DAY
      params:
        since: "{{ window.start }}"
        until: "{{ window.end }}"
  - name: raw_customers
    sdk: postgres
    method: table                      # the connector writes the SELECT: names quoted, values bound
    arguments: {schema: public, table: customers, columns: [id, name, status],
                where: [{column: status, op: "=", value: active}]}
```
{% endraw %}

**Queries**

- The query is DuckDB SQL over the attached database, not text sent to Postgres as written.
- Name tables `schema.table`; DuckDB pushes filters and columns down to Postgres.
- Its values are named parameters (`$since`) bound from `params`, never pasted into the query.
- The query text itself cannot hold references or secrets.

**Windows and batches**

- Incremental streams bind the window (`window.end` is the window's last day).
- A `batch_size` partition binds a list: {% raw %}`params: {ids: "{{ partition.ids }}"}`{% endraw %} with
  `WHERE order_id = ANY($ids::BIGINT[])` reads a batch in one query.

**Read-only**

- One SELECT per request (`WITH`, `FROM`-first and `VALUES` too).
- Write keywords, a second statement and functions that run text of their own are refused before the query runs.
- The database is attached `READ_ONLY`, and the connection reaches nothing else.
- A write fails even when the role could write.

**Isolation**

- The database is attached through a temporary DuckDB secret that holds the DSN, so DuckDB's views do not show it.
- Queries read the database's own tables only: DuckDB's system and catalog views (`duckdb_*`, `pragma_*`, `system.*`,
  `pg_catalog`, `information_schema`) and `SHOW`, `DESCRIBE` and `SUMMARIZE` are refused.

**Numbers**

- A `numeric` without a precision comes through as exact text (DuckDB's `pg_numeric_as_varchar`).
- Steps read JSON numbers as doubles (about 17 digits).
- Select big integers and exact decimals as text (`total::VARCHAR AS total`) and cast them in the step
  (`(record->>'total')::DECIMAL(18,2)`).

### Examples

- [examples/sources/readers/files_demo](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/readers/files_demo) (runs offline, on the files in its `data/`)
- [examples/sources/readers/s3_demo](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/readers/s3_demo)
- [examples/sources/readers/gcs_demo](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/readers/gcs_demo)
- [examples/sources/readers/postgres_demo](https://github.com/karthick-jaganathan/streamwright/tree/master/examples/sources/readers/postgres_demo)

The connectors' READMEs have every option, the error codes and the logs.
