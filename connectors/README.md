# ADaPT connectors

Connectors add APIs that need a vendor SDK to [adapt-core](../adapt-core/README.md): an auth provider that builds
the SDK client, and the read-only calls that a stream's `requests` items (`{name, sdk: <connector>, service, method,
arguments}`) can make. Each folder is its own distribution, installed only where it is needed, since each one pulls in
a vendor SDK. Four reader connectors read files, object storage and databases the same way, with DuckDB instead of a
vendor SDK: `files` (local files), `s3` and `gcs` (object storage) and `postgres` (PostgreSQL).

| Folder | Package | Connector | SDK | SDK loggers |
|---|---|---|---|---|
| [google_ads](ads/google_ads/README.md) | `adapt-google-ads` | `google_ads` | google-ads | `google.ads.googleads.client` |
| [microsoft_ads](ads/microsoft_ads/README.md) | `adapt-microsoft-ads` | `microsoft_ads` | bingads | `suds.client`, `suds.transport` |
| [facebook_ads](ads/facebook_ads/README.md) | `adapt-facebook-ads` | `facebook_ads` | facebook_business | `urllib3.connectionpool` |
| [files](readers/files/README.md) | `adapt-files` | `files` | duckdb | - |
| [s3](readers/s3/README.md) | `adapt-s3` | `s3` | duckdb (its httpfs extension) | - |
| [gcs](readers/gcs/README.md) | `adapt-gcs` | `gcs` | duckdb (its httpfs extension) | - |
| [postgres](readers/postgres/README.md) | `adapt-postgres` | `postgres` | duckdb (its postgres extension) | - |

```bash
make install-connectors         # all of them, from the repository root; or: pip install ./connectors/ads/google_ads
make install-files           # one of them: install-files, install-s3, install-gcs, install-postgres, ...
adapt connectors                # the installed connectors, with their SDK loggers
```

```text
$ adapt connectors
advertising:
  facebook_ads — Meta / Facebook Ads (facebook-business SDK) (SDK loggers: urllib3.connectionpool)
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

An SDK's loggers show its own request and response logs, redacted like adapt's lines; adapt never turns them on. Name
them with `--log` (each connector's README has an example):

```bash
adapt run examples/sources/ads/google_ads --set customer_ids=1112223333 --log google.ads.googleads.client=DEBUG
```

## Files, object storage and databases

- `files` reads local csv, tsv, json, jsonl and parquet files only (no URLs, no `httpfs`, no credentials, external
  access always off): `auth: {provider: files, roots: [...]}` lists the local folders that can be read, and requests
  are `sdk: files`, `method: read`, `arguments: {path, format, options, on_missing, match, recursive}`. `path` is a
  file, a glob or a list; or, with `match` (a Python regex fully matched against each file's path relative to it), a
  folder, searched in its sub-folders too with `recursive: true`. Example:
  [examples/sources/readers/files_demo](../examples/sources/readers/files_demo/), which runs offline.
- `s3` and `gcs` read the same formats from object storage through DuckDB's httpfs, with the same arguments on
  `service: object`: `auth: {provider: s3, roots: ["s3://bucket/prefix/"], key_id, secret, ...}` (optional
  `session_token`, `region`, `endpoint`, `url_style`, `use_ssl`), or `{provider: gcs, roots: ["gs://bucket/prefix/"],
  key_id, secret}` (an HMAC key). Credentials are `{{ secrets.* }}` references only and redacted; any `?` or `%` in a
  URL is refused, and the scheme, bucket and prefix of a root must match exactly. Examples:
  [examples/sources/readers/s3_demo](../examples/sources/readers/s3_demo/), [examples/sources/readers/gcs_demo](../examples/sources/readers/gcs_demo/).
- `postgres` runs read-only SELECT queries on a PostgreSQL database, attached `READ_ONLY` through DuckDB's postgres
  extension and a temporary DuckDB secret holding the DSN: `auth: {provider: postgres, dsn: "{{ secrets.pg_dsn }}"}`,
  and requests are `sdk: postgres` with `method: query` (`{query, params}`: DuckDB SQL over the attached database,
  with bound `$name` values; DuckDB's system and catalog views are refused) or `method: table`. Example:
  [examples/sources/readers/postgres_demo](../examples/sources/readers/postgres_demo/).

There is no `https` reader. Allow them like the others: `adapt run --allow-connector files` (or `s3`, `gcs`,
`postgres`). They log one line per file, object or query on `adapt.network`, and have no SDK loggers.

## Layout

```text
connectors/<name>/
├── pyproject.toml              distribution adapt-<name>; entry point <name> in the adapt.connectors group
│                               (and its query builders, if any, in adapt.query_builders)
├── README.md                   auth keys, allowed calls, records, errors and logs
├── Makefile, LICENSE, setup.py
├── src/adapt/connectors/<name>/   import adapt.connectors.<name> (adapt and adapt.connectors are namespace packages)
│   ├── __init__.py
│   └── connector.py               the Connector subclass
└── tests/                      offline tests: the real SDK against fakes
    ├── conftest.py             the `api` fixture (adapt.core.runtime.testing.FakeApi)
    └── test_<name>.py
```

Example sources are the folders in [examples/sources/](../examples/sources/), where CI validates them; the
[design doc](../docs/design/source-format.md) shows the same files.

## Adding a connector

1. Copy a connector folder, then rename the folder, the package (`src/adapt/connectors/<name>`), the distribution
   (`adapt-<name>`) and the entry point:

   ```toml
   [project.entry-points."adapt.connectors"]
   <name> = "adapt.connectors.<name>.connector:<Name>Connector"
   ```

2. Implement the connector contract ([Writing a connector](https://karthick-jaganathan.github.io/ADaPT-ETL/adapt-core/connectors-and-readers/#writing-a-connector)): allow only
   read-only calls, wrap every API call in `context.call(...)`, map SDK errors in `error()`, pass the tokens the
   connector obtains at run time to `context.secret(value)` (they are `***` in every log line and error), set
   `network_loggers` to the names of the loggers the SDK writes its requests and responses to, and set `category`
   (how `adapt connectors` groups it, e.g. `"databases"`) and a one-line `summary`, both shown by `adapt connectors`.
3. Test offline with `adapt.core.runtime.testing` (`FakeApi`, `MemoryOutput`, `FakeClock`, and `page_stream` for a stream
   with one request, step and export), starting each test module with `pytest.importorskip("<sdk module>")` so the
   core CI job, which has no SDKs, skips it.
4. Add the connector to `CONNECTORS` in the root Makefile and to the `connectors` job in `.github/workflows/ci.yml`, and add an
   example source folder to `examples/sources/` and the design doc.

Tests: `make test` runs everything; `python -m pytest connectors/<name>/tests` runs one connector's tests.
