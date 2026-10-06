# ADaPT gcs connector

The `gcs` connector for [adapt-core](../../../adapt-core/README.md): `sdk: gcs` requests read csv, tsv, json, jsonl
and parquet objects from Google Cloud Storage through [DuckDB](https://duckdb.org/)'s httpfs extension (GCS's
S3-compatible access, with an HMAC key), each row one record, on a DuckDB connection of the connector's own (never the
run's transform sandbox). Example: the source folder [examples/sources/readers/gcs_demo/](../../../examples/sources/readers/gcs_demo/).
Local files are read by the `files` connector; Amazon S3 by the `s3` connector.

## Install

```bash
make install-gcs             # from the repository root; or: pip install ./connectors/readers/gcs (installs duckdb)
adapt connectors             # lists gcs (it has no SDK loggers)
adapt validate examples/sources/readers/gcs_demo     # static checks: no network, no bucket
ADAPT_SECRET_GCS_KEY_ID=GOOG1E... ADAPT_SECRET_GCS_SECRET=... \
  adapt run examples/sources/readers/gcs_demo --set bucket_root=gs://my-bucket/exports/ --allow-connector gcs --output jsonl:out
```

DuckDB installs its httpfs extension on the first connect (a download; offline hosts: run `INSTALL httpfs` in DuckDB
ahead of time, or set `extension_directory`).

## Auth

```yaml
spec:
  secrets:
    gcs_key_id: {type: string}
    gcs_secret: {type: string}
auth:
  provider: gcs
  roots: ["gs://acme-data/exports/"]
  key_id: "{{ secrets.gcs_key_id }}"
  secret: "{{ secrets.gcs_secret }}"
```

| Key | Meaning |
|---|---|
| `roots` | required: the URL prefixes objects can be read from, `gs://bucket/prefix/` (a list; references allowed, e.g. `["{{ config.bucket_root }}"]`; no secrets, no glob, no `?`). |
| `key_id`, `secret` | required: an HMAC key (Cloud Storage > Settings > Interoperability), each ONE `{{ secrets.* }}` reference. `adapt validate` refuses any other reference, and `adapt run` refuses to connect with a value that is not a secret of the run, so credentials are never written in the source or its config. |

connect() loads httpfs and sets the key as ONE temporary DuckDB secret (`TYPE gcs`) scoped to the roots, as bound
parameters. Both values are registered as secrets of the run: `***` in every log line, error, `--summary` and message
the connector gives.

## Requests

```yaml
requests:
  - name: raw_orders
    sdk: gcs
    service: object                  # the default and only service
    method: read
    arguments:
      path: "orders/*.csv"           # a key relative to the first root, or gs://bucket/key; a glob; a list
      format: csv                    # csv, tsv, json, jsonl, parquet or auto (by extension; the default)
      options: {header: true, filename: true}
      on_missing: skip               # skip (a warning; the default) or error, when nothing matches
  - name: raw_events
    sdk: gcs
    method: read
    arguments:
      path: events/                  # with `match`: the folder to look in
      match: 'day=\d{4}-\d{2}-\d{2}/part-\d+\.parquet'
      recursive: true                # default false: only the folder's own objects
```

The arguments are those of the `s3` connector (see [its README](../s3/README.md#requests)): `path` (a `gs://` URL or a
key relative to the first root; a glob with `*`, `[ab]`, `**` - never `?`; a list), `format`, `options`,
`on_missing`, and `match` (a literal Python regex fully matching each object's key relative to the `path` folder,
in key order) with `recursive` (default false). Each row is one record, a JSON object, in pages of at most 1,000
records; each object read is one call and logs one `adapt.network` line: `gcs read gs://...: N row(s), S s`.

## Security

- **No `?` anywhere - the query-parameter injection.** DuckDB's httpfs reads a gs:// URL's query parameters as
  connection settings too (`?s3_endpoint=`, `?s3_access_key_id=`, `?s3_use_ssl=`, ...):
  `gs://acme/exports/x.csv?s3_endpoint=evil.example.com` would send a request signed with the HMAC key to another
  host. The connector refuses any `?` in a root, a path (literal or rendered) and every key a listing returns, and every
  `%` (so no percent-encoded `?`, `.`, `/` or `\`); `?` is not a glob character here (only `*` and `[`). Every
  listing and every read re-checks its URL.
- **Containment.** A URL is read only if it is inside a root: `gs://`, the same bucket (exactly as written) and a key
  under the root's prefix - `gs://acme/` does not hold `gs://acme-evil/...`. Refused before anything is read: `..`,
  `.` and empty folders, `%`, `#`, backslashes, control characters, `user@` and a port in the bucket, other schemes
  (`s3://`, `gcs://`, `https://`), absolute local paths; a listed key that fails any check fails the request.
- **DuckDB checks too**: `allowed_directories` are the roots, external access is off and the configuration locked;
  the secret is scoped to the roots.
- **Credentials** come from secrets only, are redacted everywhere and travel to DuckDB as bound parameters.
- Only `object.read` exists: objects are never written.

## Tests

```bash
make -C connectors/readers/gcs test     # offline: a recording DuckDB stand-in serves gs:// URLs from local files
ADAPT_TEST_GCS=gs://bucket/prefix/ ADAPT_TEST_GCS_KEY_ID=... ADAPT_TEST_GCS_SECRET=... make -C connectors/readers/gcs test
```

When DuckDB's httpfs is installed locally, two tests use it with refused URLs and a `127.0.0.1` listener only (never
reached by the connector); the real-bucket test runs only with `ADAPT_TEST_GCS`.
