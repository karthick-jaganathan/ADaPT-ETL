#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# * https://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
# **************************************************************************/

"""
DuckDB and DuckLake warehouse outputs for `adapt run`.

DuckLake (`--output ducklake:CATALOG[:SCHEMA]`) reads these environment variables (all optional):

- ADAPT_DUCKLAKE_DATA_PATH: where DuckLake writes its data (Parquet) files, a local folder or `s3://bucket/prefix/`
  (DuckLake's default: next to the catalog).
- Object storage, set up (httpfs loaded, a temporary DuckDB `s3` secret) when the data path is `s3://...` or any
  ADAPT_DUCKLAKE_S3_* variable is set; values are bound parameters, never in a query's text, and credentials are
  never logged or shown in a message:
  - ADAPT_DUCKLAKE_S3_KEY_ID, ADAPT_DUCKLAKE_S3_SECRET (both or neither), ADAPT_DUCKLAKE_S3_SESSION_TOKEN: credentials;
  - ADAPT_DUCKLAKE_S3_REGION: e.g. us-east-1;
  - ADAPT_DUCKLAKE_S3_ENDPOINT: host[:port] of an S3-compatible store, no scheme (e.g. localhost:4566 for LocalStack);
  - ADAPT_DUCKLAKE_S3_URL_STYLE: path or vhost;
  - ADAPT_DUCKLAKE_S3_USE_SSL: true or false.
- ADAPT_DUCKLAKE_DATA_INLINING_ROW_LIMIT: DuckLake's data inlining (inserts of fewer rows are kept in the catalog
  instead of a data file); 0 turns it off. Defaults to 0 when the data path is on object storage (every load writes
  data files to the bucket), to DuckLake's default otherwise.
- A Postgres catalog: CATALOG `postgres:DSN`, a libpq connection string such as
  `postgres:dbname=lake host=localhost user=adapt` (DuckDB's postgres extension is loaded; Postgres catalogs take
  concurrent writers). The DSN cannot hold a password: ADAPT_DUCKLAKE_CATALOG_PASSWORD gives it, as a temporary
  DuckDB postgres secret (a bound parameter, never logged).
- ADAPT_DUCKLAKE_CATALOG_SCHEMA: the schema of the catalog's own tables (DuckLake's METADATA_SCHEMA), e.g. to keep a
  Postgres catalog in a schema of its own.
"""

import datetime
import json
import logging
import os
import re

from adapt.core.engine import sql
from adapt.core.outputs.output import log_summary
from adapt.core.outputs.staging import Staging, canonical as _canonical, json_path as _json_path, quote as _quote, \
    sql_string as _sql_string, staged_table, typed_columns
from adapt.core.runtime.templates import to_text


__all__ = ["DuckDBOutput", "DuckLakeOutput"]

LOG = logging.getLogger("adapt.output")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATE_TABLE = "_adapt_state"

POSTGRES_PREFIX = "postgres:"
DATA_PATH_ENV = "ADAPT_DUCKLAKE_DATA_PATH"
INLINING_ENV = "ADAPT_DUCKLAKE_DATA_INLINING_ROW_LIMIT"
CATALOG_PASSWORD_ENV = "ADAPT_DUCKLAKE_CATALOG_PASSWORD"
CATALOG_SCHEMA_ENV = "ADAPT_DUCKLAKE_CATALOG_SCHEMA"
S3_SECRET_NAME = "adapt_ducklake_s3"
# environment variable -> the DuckDB s3 secret's parameter
S3_CREDENTIALS = {"ADAPT_DUCKLAKE_S3_KEY_ID": "KEY_ID", "ADAPT_DUCKLAKE_S3_SECRET": "SECRET",
                  "ADAPT_DUCKLAKE_S3_SESSION_TOKEN": "SESSION_TOKEN"}
S3_SETTINGS = {"ADAPT_DUCKLAKE_S3_REGION": "REGION", "ADAPT_DUCKLAKE_S3_ENDPOINT": "ENDPOINT",
               "ADAPT_DUCKLAKE_S3_URL_STYLE": "URL_STYLE", "ADAPT_DUCKLAKE_S3_USE_SSL": "USE_SSL"}
_URL_STYLES = ("path", "vhost")
_ENDPOINT = re.compile(r"^[A-Za-z0-9._\-]+(:[0-9]+)?$")
_REGION = re.compile(r"^[A-Za-z0-9_\-]+$")
_BUCKET = re.compile(r"^s3://([A-Za-z0-9][A-Za-z0-9._\-]*)(/|$)")
_DSN_PASSWORD = re.compile(r"(^|[\s?&])password\s*=", re.IGNORECASE)
_MISSING_CATALOG = "creating a new DuckLake is explicitly disabled"


def _checked(name, what):
    if not (isinstance(name, str) and _NAME.match(name)):
        raise ValueError("%s %r is not valid (use letters, digits and _, not starting with a digit)" % (what, name))
    return name


def _key_value(alias, key, whole_record):
    if whole_record:
        return "json_extract_string(%s.%s, %s)" % (alias, _quote("record"), _sql_string(_json_path(key)))
    return "%s.%s" % (alias, _quote(key))


def _table_name(schema, name):
    return "%s.%s" % (_quote(schema), _quote(name))


def _load_extension(connection, name):
    """Loads a DuckDB extension, installing it first (once, from DuckDB's repository) when it is missing."""
    installed = connection.execute("SELECT bool_or(installed) FROM duckdb_extensions() "
                                   "WHERE extension_name = ? OR list_contains(aliases, ?)", [name, name]).fetchone()
    if not (installed and installed[0]):
        connection.execute("INSTALL %s" % name)
    connection.execute("LOAD %s" % name)


def _load_ducklake(connection):
    """Loads DuckDB's ducklake extension, installing it first (once, from DuckDB's repository) when it is missing."""
    _load_extension(connection, "ducklake")


def _is_postgres(catalog):
    return catalog.startswith(POSTGRES_PREFIX)


def _flag(name, value):
    text = value.strip().lower()
    if text not in ("true", "false"):
        raise ValueError("%s: expected true or false" % name)
    return text == "true"


def ducklake_settings(environ, catalog):
    """
    The DuckLake connection settings of `environ` (see the module's doc) for `catalog`, checked: a dict of data_path,
    s3 (None, or the s3 secret's parameter -> value), s3_scope, inlining (None or a row limit), catalog_password and
    catalog_schema. ValueError (naming the variable, never a credential's value) when a setting is wrong.
    """
    def value(name):
        text = environ.get(name)
        return text if text is not None and text.strip() != "" else None

    data_path = value(DATA_PATH_ENV)
    data_path = os.path.expanduser(data_path) if data_path is not None else None
    on_s3 = data_path is not None and data_path.lower().startswith("s3://")
    names = list(S3_CREDENTIALS) + list(S3_SETTINGS)
    s3 = None
    if on_s3 or any(value(name) is not None for name in names):
        s3 = {}
        for name, parameter in list(S3_CREDENTIALS.items()) + list(S3_SETTINGS.items()):
            text = value(name)
            if text is None:
                continue
            if name in S3_SETTINGS:
                text = text.strip()
            if parameter == "USE_SSL":
                s3[parameter] = _flag(name, text)
                continue
            if parameter == "URL_STYLE" and text not in _URL_STYLES:
                raise ValueError("%s: expected one of: %s" % (name, ", ".join(_URL_STYLES)))
            if parameter == "ENDPOINT" and not _ENDPOINT.match(text):
                raise ValueError("%s: expected a host and an optional port, e.g. localhost:4566 or "
                                 "storage.example.com (no scheme: %s picks https or http)" % (
                                     name, "ADAPT_DUCKLAKE_S3_USE_SSL"))
            if parameter == "REGION" and not _REGION.match(text):
                raise ValueError("%s: expected a region, e.g. us-east-1" % name)
            s3[parameter] = text
        if ("KEY_ID" in s3) != ("SECRET" in s3):
            raise ValueError("ADAPT_DUCKLAKE_S3_KEY_ID and ADAPT_DUCKLAKE_S3_SECRET go together: set both or neither")
        if "SESSION_TOKEN" in s3 and "KEY_ID" not in s3:
            raise ValueError("ADAPT_DUCKLAKE_S3_SESSION_TOKEN needs ADAPT_DUCKLAKE_S3_KEY_ID and "
                             "ADAPT_DUCKLAKE_S3_SECRET")
    scope = None
    if on_s3:
        bucket = _BUCKET.match(data_path)
        if not bucket:
            raise ValueError("%s: expected s3://BUCKET/PREFIX/" % DATA_PATH_ENV)
        scope = "s3://%s/" % bucket.group(1)
    inlining = value(INLINING_ENV)
    if inlining is not None:
        if not inlining.strip().isdigit():
            raise ValueError("%s: expected a whole number >= 0 (0 turns data inlining off)" % INLINING_ENV)
        inlining = int(inlining.strip())
    elif on_s3:
        inlining = 0
    postgres = _is_postgres(catalog)
    if postgres:
        if not catalog[len(POSTGRES_PREFIX):].strip():
            raise ValueError("a postgres DuckLake catalog needs a connection string, e.g. "
                             "ducklake:postgres:dbname=lake host=localhost user=adapt")
        if _DSN_PASSWORD.search(catalog[len(POSTGRES_PREFIX):]):
            raise ValueError("the postgres DuckLake catalog's connection string holds a password: give it in %s "
                             "instead" % CATALOG_PASSWORD_ENV)
    password = environ.get(CATALOG_PASSWORD_ENV) or None
    if password is not None and not postgres:
        raise ValueError("%s is for a postgres catalog (ducklake:postgres:DSN)" % CATALOG_PASSWORD_ENV)
    schema = value(CATALOG_SCHEMA_ENV)
    if schema is not None:
        _checked(schema.strip(), CATALOG_SCHEMA_ENV)
        schema = schema.strip()
    return {"data_path": data_path, "s3": s3, "s3_scope": scope, "inlining": inlining, "catalog_password": password,
            "catalog_schema": schema}


def s3_secret_query(s3, scope=None):
    """(query, parameters) that create the temporary DuckDB s3 secret; every value is a bound parameter."""
    parts, parameters = ["TYPE s3"], []
    for parameter in list(S3_CREDENTIALS.values()) + list(S3_SETTINGS.values()):
        if parameter in s3:
            parts.append("%s ?" % parameter)
            parameters.append(s3[parameter])
    if scope:
        parts.append("SCOPE ?")
        parameters.append(scope)
    return "CREATE OR REPLACE TEMPORARY SECRET %s (%s)" % (S3_SECRET_NAME, ", ".join(parts)), parameters


def postgres_secret_query(password):
    """
    (query, parameters) that create DuckDB's default postgres secret, which the postgres extension adds to every
    connection string (the DuckLake catalog's too): the password stays out of the ATTACH's text.
    """
    return "CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD ?)", [password]


def attach_query(catalog, settings, read_only=False):
    """The ATTACH of the DuckLake catalog (its options are not secrets: they are in the query's text)."""
    options = []
    if settings["data_path"]:
        options.append("DATA_PATH %s" % _sql_string(settings["data_path"]))
    if settings["catalog_schema"]:
        options.append("METADATA_SCHEMA %s" % _sql_string(settings["catalog_schema"]))
    if settings["inlining"] is not None:
        options.append("DATA_INLINING_ROW_LIMIT %d" % settings["inlining"])
    if read_only:
        options.append("READ_ONLY")
    attach_options = " (%s)" % ", ".join(options) if options else ""
    return "ATTACH %s AS %s%s" % (_sql_string("ducklake:" + catalog), _quote("adapt_ducklake"), attach_options)


def _masked(text, values):
    for value in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


class _WarehouseOutput(object):
    """Common staging and load logic for DuckDB and DuckLake."""

    kind = "warehouse"

    def __init__(self, path, schema, source):
        _checked(schema, "schema")
        self.path = os.path.expanduser(path)
        self.schema = schema
        self.source_name = source["name"]
        # unkeyed exports that hold a window of their data are appended, the others replaced
        self.incremental = sql.incremental_exports(source)
        self.staging = Staging("adapt-%s-" % self.kind)
        self.streams = self.staging.exports  # name -> dict(path, handle, schema, keys, columns, count, whole_record)
        self.partial = set()
        self.state = None
        self.status = None  # "loaded" once close() has loaded the run, "failed" if it loaded nothing

    def write_columns(self, name, columns):
        self.staging.write_columns(name, columns)

    def write_schema(self, name, schema, key_properties):
        self.staging.write_schema(name, schema, key_properties)

    def write_record(self, name, record):
        self.staging.write_record(name, record)

    def write_state(self, state):
        self.state = state

    def mark_partial(self, name):
        self.partial.add(name)

    def disposition(self, name, keys):
        if keys:
            return "merge"
        return "append" if name in self.incremental else "replace"

    def _kept(self, name, keys):
        """Whether the table keeps its rows: an unkeyed full-refresh export of a stream that lacks partitions."""
        return name in self.partial and self.disposition(name, keys) == "replace"

    def summary(self):
        """
        One entry per export (see adapt.core.outputs.output): export, records, table (SCHEMA.EXPORT), disposition (merge,
        replace, append, or skipped: the table kept its rows), primary_key, and state (SCHEMA._adapt_state) once the
        load saved it. No entries after a failed run or load: nothing was loaded.
        """
        if self.status == "failed":
            return []
        entries = []
        for name, stream in self.streams.items():
            entry = {"export": name, "records": stream["count"], "table": "%s.%s" % (self.schema, name),
                     "disposition": "skipped" if self._kept(name, stream["keys"]) else self.disposition(
                         name, stream["keys"]), "primary_key": list(stream["keys"])}
            if self.status == "loaded" and self.state is not None:
                entry["state"] = "%s.%s" % (self.schema, _STATE_TABLE)
            entries.append(entry)
        return entries

    def close(self, failed=False):
        self.status = "failed"
        try:
            self.staging.close()
            if failed:
                return
            self._load()
            self.status = "loaded"
        finally:
            self.staging.remove()
        log_summary(self.summary())

    def _connect(self, read_only=False):
        raise NotImplementedError

    def _destination(self):
        return self.path

    def _load(self):
        if not self.streams and self.state is None:
            return
        connection = self._connect()
        try:
            connection.execute("SET TimeZone = 'UTC'")
            connection.execute("BEGIN")
            try:
                connection.execute("CREATE SCHEMA IF NOT EXISTS %s" % _quote(self.schema))
                loaded = 0
                for index, (name, stream) in enumerate(self.streams.items()):
                    if self._load_table(connection, index, name, stream):
                        loaded += 1
                if self.state is not None:
                    self._save_state(connection)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            LOG.debug("%s: loaded %d table(s) into %s, schema %r", self.kind, loaded, self._destination(),
                      self.schema)
        finally:
            connection.close()

    def _table_columns(self, connection, name):
        try:
            rows = connection.execute("DESCRIBE %s" % _table_name(self.schema, name)).fetchall()
        except Exception:
            return None
        return dict((row[0], _canonical(row[1])) for row in rows)

    def _ensure_table(self, connection, name, columns):
        existing = self._table_columns(connection, name)
        target = dict((column, _canonical(kind)) for column, kind in columns)
        if existing is None:
            connection.execute("CREATE TABLE IF NOT EXISTS %s (%s)" % (
                _table_name(self.schema, name), ", ".join("%s %s" % (_quote(column), kind)
                                                          for column, kind in columns)))
            return
        for column, kind in target.items():
            if column in existing and existing[column] != kind:
                raise ValueError("%s.%s column %r has type %s, but this run writes %s" % (
                    self.schema, name, column, existing[column], kind))
        for column, kind in columns:
            if column not in existing:
                connection.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_table_name(self.schema, name),
                                                                        _quote(column), kind))

    def _stage(self, connection, index, stream, columns):
        wrapper = "_adapt_wrapper_%d" % index
        rows = "_adapt_rows_%d" % index
        connection.execute("DROP TABLE IF EXISTS %s" % _quote(wrapper))
        connection.execute("DROP TABLE IF EXISTS %s" % _quote(rows))
        if stream["count"] == 0:
            connection.execute("CREATE TEMP TABLE %s (%s)" % (
                _quote(rows), ", ".join(["_adapt_ordinal BIGINT"] + ["%s %s" % (_quote(column), kind)
                                                                     for column, kind in columns])))
            return rows
        connection.execute("CREATE TEMP TABLE %s AS SELECT ordinal, record FROM %s" % (
            _quote(wrapper), staged_table(stream["path"])))
        select = typed_columns(columns, stream["whole_record"])
        statement = "CREATE TEMP TABLE %s AS SELECT %s FROM %s" % (_quote(rows), ", ".join(select), _quote(wrapper))
        connection.execute(statement)
        return rows

    def _dedupe(self, connection, index, rows, keys, whole_record=False):
        deduped = "_adapt_dedup_%d" % index
        connection.execute("DROP TABLE IF EXISTS %s" % _quote(deduped))
        by = ", ".join(_key_value("rows", key, whole_record) for key in keys)
        connection.execute("CREATE TEMP TABLE %s AS SELECT * EXCLUDE (_adapt_row_number) FROM ("
                           "SELECT *, row_number() OVER (PARTITION BY %s ORDER BY _adapt_ordinal DESC) "
                           "AS _adapt_row_number FROM %s AS rows) WHERE _adapt_row_number = 1" % (
                               _quote(deduped), by, _quote(rows)))
        return deduped

    def _load_table(self, connection, index, name, stream):
        keys = stream["keys"]
        columns = self.staging.columns_of(name)
        disposition = self.disposition(name, keys)
        if self._kept(name, keys):
            LOG.warning("stream %r skipped partitions, so its table keeps the rows of the last complete run "
                        "(it has no primary_key to merge on)", name)
            return False
        self._ensure_table(connection, name, columns)
        rows = self._stage(connection, index, stream, columns)
        source = self._dedupe(connection, index, rows, keys, stream["whole_record"]) if keys else rows
        table = _table_name(self.schema, name)
        names = ", ".join(_quote(column) for column, _ in columns)
        if disposition == "merge":
            condition = " AND ".join("%s IS NOT DISTINCT FROM %s" % (
                _key_value("target", key, stream["whole_record"]), _key_value("source", key, stream["whole_record"]))
                for key in keys)
            connection.execute("DELETE FROM %s AS target USING %s AS source WHERE %s" % (
                table, _quote(source), condition))
        elif disposition == "replace":
            connection.execute("DELETE FROM %s" % table)
        else:
            LOG.warning("stream %r has no primary_key, so %s appends it: the days read again (lookback and today) "
                        "are loaded again", name, self.kind)
        connection.execute("INSERT INTO %s BY NAME SELECT %s FROM %s" % (table, names, _quote(source)))
        return True

    def _save_state(self, connection):
        table = _table_name(self.schema, _STATE_TABLE)
        connection.execute("CREATE TABLE IF NOT EXISTS %s (source VARCHAR, state JSON, "
                           "loaded_at TIMESTAMP WITH TIME ZONE)" % table)
        connection.execute("DELETE FROM %s WHERE source = $source" % table, {"source": self.source_name})
        connection.execute("INSERT INTO %s (source, state, loaded_at) VALUES ($source, $state::JSON, $loaded_at)" %
                           table, {"source": self.source_name, "state": json.dumps(self.state, default=to_text),
                                   "loaded_at": datetime.datetime.now(datetime.timezone.utc)})

    def _state_connection(self):
        """A read-only connection to the destination, or None when it does not exist yet."""
        if not os.path.exists(self.path):
            return None
        return self._connect(read_only=True)

    def initial_state(self):
        connection = self._state_connection()
        if connection is None:
            return None
        try:
            connection.execute("SET TimeZone = 'UTC'")
            try:
                row = connection.execute("SELECT state FROM %s WHERE source = $source" % _table_name(
                    self.schema, _STATE_TABLE), {"source": self.source_name}).fetchone()
            except Exception:
                return None
            if not row:
                return None
            return json.loads(row[0]) if isinstance(row[0], str) else row[0]
        finally:
            connection.close()


class DuckDBOutput(_WarehouseOutput):
    """Warehouse output that loads into a DuckDB database file."""

    kind = "duckdb"

    def __init__(self, path, schema, source):
        database = os.path.basename(os.path.expanduser(path))
        if os.path.splitext(database)[0].lower() == schema.lower():
            raise ValueError("schema %r has the name of the database file %s: DuckDB cannot tell them apart; "
                             "give the file another name or pass a schema (duckdb:%s:SCHEMA)" % (
                                 schema, database, path))
        super(DuckDBOutput, self).__init__(path, schema, source)

    def _connect(self, read_only=False):
        import duckdb
        return duckdb.connect(self.path, read_only=read_only)


class DuckLakeOutput(_WarehouseOutput):
    """
    Warehouse output that loads into a DuckLake catalog: a local file, or a Postgres database (`postgres:DSN`), with
    its data files in a local folder or on S3 (see the module's doc for the environment variables).
    """

    kind = "ducklake"

    def __init__(self, path, schema, source):
        postgres = _is_postgres(path)
        self.settings = ducklake_settings(os.environ, path)
        super(DuckLakeOutput, self).__init__(path, schema, source)
        if postgres:
            self.path = path  # a connection string, not a file: no ~ to expand
        self.postgres = postgres

    def _secrets(self):
        s3 = self.settings["s3"] or {}
        return [str(s3[parameter]) for parameter in ("KEY_ID", "SECRET", "SESSION_TOKEN") if s3.get(parameter)] + [
            value for value in [self.settings["catalog_password"]] if value]

    def _mask(self, exc):
        """`exc`, or (when its message shows a credential) a RuntimeError with the credentials masked."""
        text = str(exc)
        masked = _masked(text, self._secrets())
        return exc if masked == text else RuntimeError(masked)

    def _connect(self, read_only=False):
        import duckdb
        connection = duckdb.connect(":memory:")
        try:
            _load_ducklake(connection)
            s3 = self.settings["s3"]
            if s3 is not None:
                _load_extension(connection, "httpfs")
                if s3:
                    connection.execute(*s3_secret_query(s3, self.settings["s3_scope"]))
            if self.postgres:
                _load_extension(connection, "postgres")
                if self.settings["catalog_password"]:
                    connection.execute(*postgres_secret_query(self.settings["catalog_password"]))
            LOG.debug("ducklake: catalog %s, data path %s, s3 %s, data inlining row limit %s", self._destination(),
                      self.settings["data_path"] or "(DuckLake's)", "(not set up)" if s3 is None else ", ".join(
                          "%s=%s" % (key.lower(), "***" if key in S3_CREDENTIALS.values() else value)
                          for key, value in sorted(s3.items())) or "(no secret)",
                      "(DuckLake's)" if self.settings["inlining"] is None else self.settings["inlining"])
            connection.execute(attach_query(self.path, self.settings, read_only))
            connection.execute("USE %s" % _quote("adapt_ducklake"))
            return connection
        except Exception as exc:
            connection.close()
            masked = self._mask(exc)
            if masked is exc:
                raise
            raise masked from None

    def _load(self):
        try:
            super(DuckLakeOutput, self)._load()
        except Exception as exc:
            masked = self._mask(exc)
            if masked is exc:
                raise
            raise masked from None

    def _state_connection(self):
        if not self.postgres:
            return super(DuckLakeOutput, self)._state_connection()
        try:
            return self._connect(read_only=True)
        except Exception as exc:
            if _MISSING_CATALOG in str(exc):  # no catalog in that database (schema) yet: the first load creates it
                return None
            raise
