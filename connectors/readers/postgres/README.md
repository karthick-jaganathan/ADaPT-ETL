# StreamWright postgres connector

The `postgres` connector for [streamwright](../../../core/README.md): `sdk: postgres` requests run read-only SELECT
queries on a PostgreSQL database through [DuckDB](https://duckdb.org/)'s postgres extension, each row one record, on
a DuckDB connection of the connector's own (never the run's transform sandbox). Example: the source folder
[examples/sources/readers/postgres_demo/](../../../examples/sources/readers/postgres_demo/).

## Install

```bash
make install-postgres        # from the repository root; or: pip install ./connectors/readers/postgres (installs duckdb)
streamwright connectors             # lists postgres (it has no SDK loggers)
STREAMWRIGHT_SECRET_PG_PASSWORD='...' streamwright run examples/sources/readers/postgres_demo --set pg_host=db.example.com \
  --allow-connector postgres --output jsonl:out
```

DuckDB installs its postgres extension on the first connect (a download; offline hosts: run `INSTALL postgres` in
DuckDB ahead of time, or set `extension_directory`).

## Auth

Two forms, one or the other (not both): the connection keys, or one DSN secret.

```yaml
spec:
  config:
    pg_host: {type: string, default: localhost}
  secrets:
    pg_password: {type: string}
auth:
  provider: postgres
  host: "{{ config.pg_host }}"
  port: 5432
  database: shop                     # or `dbname`
  user: reader
  password: "{{ secrets.pg_password }}"
  sslmode: require
  options: {connect_timeout: "10", application_name: streamwright}
  statement_timeout: 5min
```

```yaml
spec:
  secrets:
    pg_dsn: {type: string}
auth:
  provider: postgres
  dsn: "{{ secrets.pg_dsn }}"
  statement_timeout: 5min
```

Use the connection keys when the connection is configuration (a host, database and role per environment, set with
`--set` or a config file) and only the password is secret; use `dsn` when the whole connection string is managed as
one secret (e.g. a URL a vault hands out), or needs libpq features the keys do not cover.

| Key | Meaning |
|---|---|
| `host` | required (connection keys): the server's host name or address; literal text or a reference (config, not a secret). |
| `port` | optional, default `5432`: a whole number (or a reference). |
| `database` | required (connection keys): the database name; `dbname` is an alias (not both). |
| `user` | required (connection keys): the role; use one that can only read. |
| `password` | the role's password, when the server asks for one (the connection keys' ONLY credential): ONE secret reference, e.g. `"{{ secrets.pg_password }}"`. `streamwright validate` refuses any other reference (and warns of a literal), and `streamwright run` refuses to connect with a password that is not a secret of the run. It is redacted (`***`) in every log line and error, as is the connection string that holds it. |
| `sslmode` | optional: `disable`, `allow`, `prefer` (libpq's default), `require`, `verify-ca` or `verify-full`. |
| `options` | optional: extra libpq connection parameters, a mapping of name to text, e.g. `{connect_timeout: "10", application_name: streamwright, target_session_attrs: read-only, sslrootcert: /etc/ssl/pg.crt}`; values may be references but not secrets. Names are libpq's (lowercase); not `password`, `passfile`, `sslpassword` or other credentials, not the keys above (`host`, `port`, `dbname`, `user`, `sslmode`), not `dsn`, `replication` or `sslkeylogfile`. Its `options` parameter (Postgres server options, e.g. `-c search_path=shop`) is kept, and `statement_timeout` added to it. |
| `dsn` | instead of the connection keys: a libpq DSN - `postgresql://user:password@host:5432/db?sslmode=require` or `host=... dbname=... user=... password=...` - as ONE secret reference. `streamwright validate` refuses any other reference, and `streamwright run` refuses to connect with a DSN that is not a secret of the run, so a DSN is never written in the source or its config. The DSN, and any password inside it, is redacted (`***`) in every log line and error. |
| `statement_timeout` | optional: seconds (`90`), or `500ms`, `30s`, `5min`, `1h`: Postgres' `statement_timeout` for every statement the connector runs (added to libpq's `options`; a DSN that sets `options` itself must add `-c statement_timeout=...` there instead). |

The connection keys become one libpq connection string - `host='...' port='5432' dbname='...' user='...'
password='...' sslmode='...' ...` - every value single-quoted, its backslashes and quotes escaped, so a value such as
`x' dbname=other` stays that one (odd) value and can never add or replace a parameter. It is attached exactly like a
DSN: through a temporary DuckDB secret (see Security), never the attach path.

Use a role that can only read; the connector enforces read-only access anyway (see Security).

## Requests

```yaml
requests:
  - name: raw_orders
    sdk: postgres
    service: database                # the default and only service
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
    method: table
    arguments:
      schema: public                 # optional: default the database's default schema (public)
      table: customers
      columns: [id, name, status]    # optional: default all
      where:                         # optional: joined with AND
        - {column: status, op: "=", value: "{{ config.status }}"}
        - {column: id, op: IN, value: "{{ partition.ids }}", type: BIGINT}
```

- `query`: ONE SELECT (`WITH`, `FROM`-first and `VALUES` too) in DuckDB's SQL over the attached database: name
  tables `schema.table` (or `table` in the default schema, `public`); filters and columns are pushed down to Postgres.
  A trailing `;` is fine; a second statement is refused.
- `params`: the query's values, by name - `$since` in the query, `since:` in `params`. They are BOUND (DuckDB prepared
  parameters), never pasted into the query text, so a value like `'; DROP TABLE x; --` stays a value. References are
  rendered first and keep their types: `{{ window.start }}` / `{{ window.end }}` are dates (`window.end` is the
  window's last day), `{{ config.* }}`, `{{ partition.* }}`. Every `$name` must be in `params` and every param used.
  The query text itself cannot hold references (`{{ ... }}`) or secrets.
- A `batch_size` partition is a list: `params: {ids: "{{ partition.ids }}"}` with `WHERE id = ANY($ids::BIGINT[])`
  reads a batch in one query (ids taken from JSON records are numbers or text: cast the list to the column's type).
- `table`: the connector writes the SELECT: names are quoted (case-sensitive, any text up to 63 bytes) and `where` values
  bound. `op`: `=` (default), `!=`, `<`, `<=`, `>`, `>=`, `IN`, `NOT IN` (a list `value`), `IS NULL`, `IS NOT NULL`
  (no `value`); `type` casts the value (or each item of a list), e.g. `DATE`, `BIGINT`, `DECIMAL(12,2)`. `schema`,
  `table` and `value` may be references; `columns` are literal.

## Records

Each row is one record, a JSON object (`to_json`), in pages of at most 1,000 records (memory stays bounded however
many rows the query returns); steps read them with `record->>'column'` and cast. Integers are whole numbers, decimals
exact JSON numbers, dates and timestamps text (timestamps with a time zone in UTC: `2026-10-03 08:00:00+00`), NULL
null. A Postgres `numeric` without a precision (or with more than 38 digits) is exact text, e.g.
`"1234567890123456.78"`, never a rounded double: cast it in a step (`(record->>'amount')::DECIMAL(38,2)`). Steps
read JSON numbers as doubles (`record->>'total'` keeps about 17 digits): select a column whose values need more as
text (`total::VARCHAR AS total`) and cast it in the step (`(record->>'total')::DECIMAL(18,2)`), exactly.

## Security

- The DSN, or the connection keys' password, is a secret only (above) and redacted everywhere, with the connection
  string that holds it. The connector keeps the DSN or connection string in a temporary DuckDB secret (`duckdb_secrets()`
  shows it as `uri=redacted`) and attaches through that: the attached database's path, which DuckDB's system views
  show, is empty. (Postgres' own connection errors may name the host and user, which are config, never the password.)
- Read-only, three times over: every query is checked BEFORE it runs - one statement, starting with SELECT/WITH/FROM/
  VALUES, no write keyword (INSERT, UPDATE, DELETE, MERGE, COPY, CREATE, DROP, ALTER, GRANT, REVOKE, TRUNCATE, CALL,
  EXECUTE, PREPARE, ATTACH, INSTALL, PRAGMA, VACUUM, ...; a column named like one must be double-quoted, e.g.
  `"update"`), and by DuckDB's own parser a single SELECT with no table functions but `unnest`, `range` and
  `generate_series`, no function that runs text of its own (`postgres_execute`, `postgres_query`, `query`), reads
  files or settings, and no file names as tables. It reads the database's own tables only: DuckDB's system views and
  schemas are refused (`duckdb_*`, `pragma_*`, `sqlite_*`, `pg_tables` and the other `pg_catalog` views, the schemas
  `system`, `temp`, `memory`, `pg_catalog`, `information_schema`), and so are SHOW, DESCRIBE and SUMMARIZE. The
  database is attached `READ_ONLY` (DuckDB refuses writes, and
  Postgres runs the reads in READ ONLY transactions); and the connection, once attached, has external access off and
  its configuration locked (no files, no other databases, no extensions). A write fails even when the role could
  write: `QUERY_REFUSED` before it runs, or `READ_ONLY`.
- Values are bound parameters, never query text. Allow the connector with `streamwright run --allow-connector postgres`.

## Errors

A refused query is `QUERY_REFUSED`; a write the database refuses `READ_ONLY`; a query Postgres cancels after
`statement_timeout` is `STATEMENT_TIMEOUT`; a lost or refused connection is `CONNECTION_ERROR` (retried, as is a
connect that cannot reach the server; a connect refused otherwise, e.g. its password, is a `CONNECT_ERROR` that is
not); any other query error is `QUERY_ERROR`. Only the connection errors are retried (unless the stream's
`retry.codes` names others); a query that fails after its first page is not retried, since its rows were already
read.

## Logs

Each query is a call (counted in the run summary's `requests`, with the stream's rate limit and retries) and logs an
INFO line on `streamwright.network`, with its rows and time (never the DSN or connection string):

```text
INFO streamwright.network: stream 'orders', request 'raw_orders', window 2026-10-03..2026-10-03: postgres query: 1 row(s), 0.01 s
```

(`streamwright run --log streamwright.network=INFO` shows them.)

## Tests

`make test` (in this folder) runs offline: reads go through the same connect/query/page path against a local DuckDB
file attached READ_ONLY in place of Postgres. Set `STREAMWRIGHT_TEST_PG_DSN` to a throwaway Postgres (the test creates and
drops a table of its own) to also read a real one, and `STREAMWRIGHT_TEST_PG_HOST`, `STREAMWRIGHT_TEST_PG_USER` and
`STREAMWRIGHT_TEST_PG_DATABASE` (optionally `STREAMWRIGHT_TEST_PG_PORT`, `STREAMWRIGHT_TEST_PG_PASSWORD`, `STREAMWRIGHT_TEST_PG_SSLMODE`) to
connect to one with the connection keys (it only reads).
