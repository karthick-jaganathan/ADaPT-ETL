# StreamWright azure_blob connector

The `azure_blob` connector for [streamwright](../../../core/README.md): `sdk: azure_blob` requests read csv, tsv, json, jsonl
and parquet objects from Azure Blob Storage containers through [DuckDB](https://duckdb.org/)'s azure extension,
each row one record, on a DuckDB connection of the connector's own (never the run's transform sandbox).
Example: the source folder [examples/sources/readers/azure_blob_demo/](../../../examples/sources/readers/azure_blob_demo/).
Local files are read by the `files` connector; Amazon S3 by `s3`; Google Cloud Storage by `gcs`.

## Install

```bash
make install-azure-blob          # from repository root; or: pip install ./connectors/readers/azure_blob
streamwright connectors          # lists azure_blob (it has no SDK loggers)
streamwright validate examples/sources/readers/azure_blob_demo  # static checks: no network, no container
STREAMWRIGHT_SECRET_AZURE_CONNECTION_STRING=DefaultEndpointsProtocol=https;... \
  streamwright run examples/sources/readers/azure_blob_demo --set container_root=azure://my-container/exports/ --allow-connector azure_blob --output jsonl:out
```

DuckDB installs its azure extension on the first connect (a download; offline hosts: run `INSTALL azure` in DuckDB
ahead of time, or set `extension_directory`).

## Auth

```yaml
spec:
  secrets:
    azure_connection_string: {type: string}
auth:
  provider: azure_blob
  roots: ["azure://acme-data/exports/"]
  connection_string: "{{ secrets.azure_connection_string }}"
```

Or using an account key:

```yaml
spec:
  secrets:
    azure_account_key: {type: string}
auth:
  provider: azure_blob
  roots: ["azure://acme-data/exports/"]
  account_name: "acmedata"
  account_key: "{{ secrets.azure_account_key }}"
```

Or using Service Principal authentication:

```yaml
spec:
  secrets:
    azure_client_secret: {type: string}
auth:
  provider: azure_blob
  roots: ["azure://acme-data/exports/"]
  account_name: "acmedata"
  tenant_id: "00000000-0000-0000-0000-000000000000"
  client_id: "11111111-1111-1111-1111-111111111111"
  client_secret: "{{ secrets.azure_client_secret }}"
```

| Key | Meaning |
|---|---|
| `roots` | required: the URL prefixes objects can be read from, `azure://container/prefix/` (a list; references allowed, e.g. `["{{ config.container_root }}"]`; no secrets, no glob, no `?`). |
| `connection_string` | a standard Azure storage connection string, one `{{ secrets.* }}` reference. |
| `account_name`, `account_key` | storage account name and account key (`account_key` must be a `{{ secrets.* }}` reference). |
| `tenant_id`, `client_id`, `client_secret` | Service Principal credentials (`client_secret` must be a `{{ secrets.* }}` reference). |
| `endpoint` | optional custom blob endpoint (e.g. for Azurite emulator or private endpoints). |
| `use_ssl` | optional boolean (default true; set false for local HTTP emulators like Azurite). |

connect() loads the azure extension and sets the credentials as ONE temporary DuckDB secret (`TYPE azure`) scoped to the roots, as bound
parameters. All credential values are registered as secrets of the run: `***` in every log line, error, `--summary` and message
the connector gives.

## Requests

```yaml
requests:
  - name: raw_orders
    sdk: azure_blob
    service: object                  # the default and only service
    method: read
    arguments:
      path: "orders/*.csv"           # a key relative to the first root, or azure://container/key; a glob; a list
      format: csv                    # csv, tsv, json, jsonl, parquet or auto (by extension; the default)
      options: {header: true, filename: true}
      on_missing: skip               # skip (a warning; the default) or error, when nothing matches
  - name: raw_events
    sdk: azure_blob
    method: read
    arguments:
      path: events/                  # with `match`: the folder to look in
      match: 'day=\d{4}-\d{2}-\d{2}/part-\d+\.parquet'
      recursive: true                # default false: only the folder's own objects
```

The arguments match the `s3` and `gcs` connectors: `path` (an `azure://` URL or a
key relative to the first root; a glob with `*`, `[ab]`, `**` - never `?`; a list), `format`, `options`,
`on_missing`, and `match` (a literal Python regex fully matching each object's key relative to the `path` folder,
in key order) with `recursive` (default false). Each row is one record, a JSON object, in pages of at most 1,000
records; each object read is one call and logs one `streamwright.network` line: `azure_blob read azure://...: N row(s), S s`.

## Security

- **No `?` anywhere - the query-parameter injection.** Azure URLs take no query parameters: DuckDB would read them as
  connection parameters. The connector refuses any `?` in a root, a path (literal or rendered) and every key a listing returns, and every
  `%` (so no percent-encoded `?`, `.`, `/` or `\`); `?` is not a glob character here (only `*` and `[`). Every
  listing and every read re-checks its URL.
- **Containment.** A URL is read only if it is inside a root: `azure://`, the same container (exactly as written) and a key
  under the root's prefix. Refused before anything is read: `..`, `.` and empty folders, `%`, `#`, backslashes, control characters,
  `user@` and a port in the container, other schemes (`s3://`, `gs://`, `https://`), absolute local paths; a listed key that fails any check fails the request.
- **DuckDB checks too**: `allowed_directories` are the roots, external access is off and configuration is locked;
  the secret is scoped to the roots.
- **Credentials** come from secrets only, are redacted everywhere and travel to DuckDB as bound parameters.
- Only `object.read` exists: objects are never written.

## Tests

```bash
make -C connectors/readers/azure_blob test     # offline: a recording DuckDB stand-in serves azure:// URLs from local files
```
