# Delta Lake Connector for StreamWright

`streamwright-deltalake` is an official StreamWright connector for querying and scanning **Delta Lake** tables (local or cloud storage) via DuckDB's native `delta` extension.

## Requirements

- Python >= 3.10
- `streamwright>=0.1.0`

## Installation

```bash
pip install streamwright-deltalake
```

## Features

- **Direct Delta Scans**: Fast, vectorised table reads using `delta_scan(...)`.
- **Cloud Storage Support**: Queries Delta tables on AWS S3 (`s3://...`) and Google Cloud Storage (`gs://...`).
- **Time Travel**: Read snapshot versions via `version` or timestamp.
- **SQL Parameter Binding**: Bind values securely to pushdown queries.

## Authentication & Configuration

```yaml
auth:
  provider: deltalake
  # Optional cloud storage credentials
  aws_access_key_id: "{{ secrets.aws_access_key_id }}"
  aws_secret_access_key: "{{ secrets.aws_secret_access_key }}"
  aws_region: "us-east-1"
```

## Supported Methods

| Method | Description | Arguments |
|---|---|---|
| `scan` | Scan Delta table with column selection and filters | `table_path`, `columns`, `where`, `limit` |
| `query` | Run custom SELECT query using `delta_scan(...)` | `query`, `params` |
