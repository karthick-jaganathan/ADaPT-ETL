# MySQL Connector for StreamWright

`streamwright-mysql` is an official StreamWright connector for running high-performance, read-only `SELECT` queries on a MySQL database, attached through DuckDB's official `mysql` extension.

## Requirements

- Python >= 3.10
- `streamwright>=0.1.0`

## Installation

```bash
pip install streamwright-mysql
```

## Authentication & Configuration

The connector accepts either a DSN or a structured configuration block:

### DSN
```yaml
auth:
  provider: mysql
  dsn: "{{ secrets.mysql_dsn }}" # e.g. mysql://user:password@host:3306/dbname
```

### Structured
```yaml
auth:
  provider: mysql
  host: "{{ config.host }}"
  port: 3306
  database: "{{ config.database }}"
  user: "{{ config.user }}"
  password: "{{ secrets.mysql_password }}"
  sslmode: "preferred"
```

## Features

- **Read-Only Enforced**: Attached as `READ_ONLY` within DuckDB with AST safety checks blocking all write statements (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, etc.).
- **Safe Parameter Binding**: Values bind via `$param_name` prepared parameters.
- **Table Reader**: Method `table` directly extracts table data with column projections and pushdown `where` filters.
- **Streaming JSON Records**: Efficient batching with memory bounds.
