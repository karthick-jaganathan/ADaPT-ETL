# StreamWright files connector

The `files` connector for [streamwright](../../../core/README.md): `sdk: files` requests read local csv, tsv, json,
jsonl and parquet files with [DuckDB](https://duckdb.org/), each row one record, on a DuckDB connection of the
connector's own (never the run's transform sandbox). Example: the source folder
[examples/sources/readers/files_demo/](../../../examples/sources/readers/files_demo/), which reads the small files committed in its `data/`.

Object storage: see the s3 and gcs connectors (this connector reads local files only).

## Install

```bash
make install-files           # from the repository root; or: pip install ./connectors/readers/files (installs duckdb)
streamwright connectors             # lists files (it has no SDK loggers)
streamwright run examples/sources/readers/files_demo --set data_root=examples/sources/readers/files_demo/data --allow-connector files \
    --output jsonl:out
```

## Auth

```yaml
auth:
  provider: files
  roots: ["{{ config.data_root }}"]
```

| Key | Meaning |
|---|---|
| `roots` | required: the local folders files can be read from, a list (references are rendered first; a single reference must give a list). Relative folders are relative to the working directory. No secrets, no URLs (`s3://...` is refused: use the s3 or gcs connector). |

`roots` is the allow-list: deployments decide it (through `config` or the source), and nothing outside it can be read.

## Requests

```yaml
requests:
  - name: raw_orders
    sdk: files
    service: file                    # the default and only service
    method: read                     # the only method
    arguments:
      path: "orders/*.csv"           # a file, a glob, or a list of them; references allowed
      format: csv                    # csv | tsv | json | jsonl | parquet | auto (default: by extension)
      options: {header: true}        # per format, below; literal values
      on_missing: skip               # skip (default: a warning) | error
```

- `path`: relative to the first root, or absolute inside any root. Globs: `*`, `?`, `[ab]`, and `**` for any
  folders; matches are read in name order. References are rendered first: `{{ config.* }}`, `{{ partition.* }}`,
  `{{ window.start }}` / `{{ window.end }}` (e.g. one file per day), and a whole reference to a list - e.g. a
  `batch_size` partition of file names, `path: "{{ partition.files }}"` - reads each file of the list. Secrets and
  URLs are refused.
- `format: auto` takes each file's format from its extension: `.csv`, `.tsv`/`.tab`, `.json`, `.jsonl`/`.ndjson`,
  `.parquet`/`.pq`, also followed by `.gz`/`.zst` (e.g. `a.jsonl.gz`).
- `on_missing`: what happens when nothing matches a path (a day without its file): `skip` logs a warning and reads
  nothing, `error` fails the request (the partition, with `on_partition_error: skip`).

| Format | Options |
|---|---|
| csv, tsv | `header`, `delimiter`, `quote`, `escape`, `columns` (name: DuckDB type), `compression` (auto, none, gzip, zstd), `null_padding`, `ignore_errors`, `skip` (lines), `nullstr` (a text or a list), `all_varchar`, `dateformat`, `timestampformat`, `filename` |
| json | `format` (auto, array, newline_delimited), `compression`, `ignore_errors`, `filename` |
| jsonl | `compression`, `ignore_errors`, `filename` |
| parquet | `filename` |

`filename: true` adds each file's path, relative to its root, to its records as `filename` (e.g. to tell the files of
a glob apart).

### Picking files with a regex: `match`, `recursive`

```yaml
    arguments:
      path: orders                                 # a folder (not a glob); references allowed
      match: '^orders_\d{4}-\d{2}-\d{2}\.csv$'     # a Python regex, literal (no references)
      recursive: false                             # default false: true looks in sub-folders too
```

- With `match`, `path` is ONE folder inside a root (a text, not a list, and not a glob: a glob in `path` together
  with `match` is an error - the regex picks the files). The files in it whose path RELATIVE to it fully matches
  `match` (Python's `re.fullmatch`: `orders_2026` does not select `orders_2026-10-01.csv`) are read like a file list,
  sorted by that relative path.
- `recursive: true` also looks in its sub-folders; the relative path then has them, separated by `/`, so the regex
  sees them: `(.*/)?orders_\d{4}-\d{2}-\d{2}\.csv` selects daily files at any depth, `2026/[^/]+\.csv` the files of
  `2026/` only. Symbolic links to folders are not followed. `recursive` is `true` or `false` and goes with `match`.
- Each selected file is checked to be inside a root once symbolic links are followed (a link leaving the roots is
  refused, before any file is read). A folder that does not exist, or one where nothing matches, follows
  `on_missing`; a `path` that names a file is a `READ_ERROR`.
- `streamwright validate` refuses an invalid regex, an empty one, a reference in `match`, a non-boolean `recursive`, and a
  glob or a list `path` with `match`.

## Records

Each row of a file is one record, a JSON object, in pages of at most 1,000 records (memory stays bounded however
large the file is); steps read them with `record->>'column'` and cast:

- csv/tsv: the file's text (empty cells are null), unless `columns` types them;
- json/jsonl: each object exactly as written - a `.json` file holding an array gives one record per item, any other
  document is one record (use the request's `records: {explode: field}` for the list inside it);
- parquet: its values, with decimals exact, integers whole, dates and timestamps as text.

The request's `records.explode` applies to each record; leave `records.path` unset (each page is the record list).

## Security

- Every file is checked BEFORE any file is read: no `..`, no URL, and every file inside a root once symbolic links
  are followed (for a glob or `match`, every file it selects); otherwise the request fails with
  `files: path ... is outside the roots` (code `ACCESS_DENIED`).
- The reader's DuckDB connection can only read inside the roots too (DuckDB's `allowed_directories`, which also
  refuses symbolic links that leave them), with external access off and its configuration locked. It never turns
  external access on, loads no extension (no `httpfs`: it reads no URL) and sets no secret.
- Files are only read, never written. Allow the connector with `streamwright run --allow-connector files`.

## Errors

A file that cannot be read in its format, with its options, is a `READ_ERROR`; a path outside the roots is
`ACCESS_DENIED`; `on_missing: error` without a file is `NOT_FOUND`. None is retried (unless the stream's
`retry.codes` names them).

## Tests

`make -C connectors/readers/files test` runs offline (local files only).

## Logs

Each file read is a call (counted in the run summary's `requests`, with the stream's rate limit and retries) and logs
an INFO line on `streamwright.network`, with the file, its rows, bytes and time:

```text
INFO streamwright.network: stream 'orders', request 'raw_orders': files read /data/orders/east.csv: 3 row(s), 126 bytes, 0.00 s
```

(`streamwright run --log streamwright.network=INFO` shows them.)
