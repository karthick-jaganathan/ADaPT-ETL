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
Staged records, for the outputs that write when a run ends (DuckDB and DuckLake tables, Parquet files).

While a run goes, each export's records are appended to a JSONL file in a private temporary folder, so memory stays
flat however large an export is: one line per record, {"ordinal": N, "record": {...}}, N counting the export's
records from 1. When the run succeeds, DuckDB reads the files back (staged_table) and types each column
(typed_columns): the export's DuckDB column types (write_columns), as storage_type() maps them to types that tables
and files hold.

DECIMAL values stay exact: a DECIMAL column's value is staged as its exact text (templates.decimal_text), which DuckDB
casts to the column's type (it reads JSON numbers with fractions as doubles, which hold about 15 digits), and other
decimals (in nested values) as exact JSON numbers. Integers are JSON integers, which DuckDB reads exactly (HUGEINT
too).
"""

import decimal
import json
import logging
import os
import shutil
import tempfile

from streamwright.core.runtime.templates import decimal_text, to_json, to_text


__all__ = ["Staging", "storage_type", "columns_from_schema", "json_value", "staged_table", "typed_columns",
           "canonical", "quote", "sql_string", "json_path", "decimal_columns", "decimals_as_text"]

LOG = logging.getLogger("streamwright.output")  # (staging details, at DEBUG)
_INTEGER_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                  "UBIGINT", "UHUGEINT")
_FLOAT_TYPES = ("FLOAT", "REAL", "DOUBLE", "DOUBLE PRECISION")
_JSON_PREFIXES = ("STRUCT(", "MAP(", "UNION(")
_TIMESTAMP_TYPES = {"TIMESTAMP_S": "TIMESTAMP", "TIMESTAMP_MS": "TIMESTAMP", "TIMESTAMP_NS": "TIMESTAMP"}
_SCHEMA_TYPES = {"string": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN",
                 "object": "JSON", "array": "JSON"}
_SCHEMA_FORMATS = {"date": "DATE", "date-time": "TIMESTAMP WITH TIME ZONE", "time": "TIME"}


def quote(name):
    """A SQL identifier."""
    return '"%s"' % str(name).replace('"', '""')


def sql_string(value):
    """A SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def json_path(name):
    """The JSON path of a key of a JSON object."""
    return "$." + json.dumps(name)


def canonical(kind):
    """A DuckDB type name in upper case, with single spaces."""
    return " ".join(str(kind).upper().split())


def storage_type(kind):
    """
    The type a DuckDB result column is stored as: its own, except that nested values (lists, arrays, structs, maps,
    unions) are JSON, TIMESTAMP_S/MS/NS are TIMESTAMP, INTERVAL is seconds (DOUBLE, as records give it), and ENUM,
    BLOB, BIT, UUID and other types are text (VARCHAR).
    """
    kind = canonical(kind)
    base = kind.split("(", 1)[0]
    if kind in _TIMESTAMP_TYPES:
        return _TIMESTAMP_TYPES[kind]
    if kind.endswith("]") or kind == "JSON" or kind.startswith(_JSON_PREFIXES):
        return "JSON"
    if base in _INTEGER_TYPES or base in _FLOAT_TYPES or base in ("BOOLEAN", "DATE", "TIME"):
        return kind
    if base == "TIMESTAMP" or kind == "TIMESTAMP WITH TIME ZONE":
        return kind
    if base == "DECIMAL":
        return kind
    if base in ("ENUM", "VARCHAR"):
        return "VARCHAR"
    if base == "BLOB" or base == "BIT":
        return "VARCHAR"
    if base == "INTERVAL":
        return "DOUBLE"
    return "VARCHAR"


def _schema_type(definition):
    kind = definition.get("type") if isinstance(definition, dict) else None
    kinds = [item for item in (kind if isinstance(kind, list) else [kind]) if item and item != "null"]
    return _SCHEMA_FORMATS.get(definition.get("format")) or _SCHEMA_TYPES.get(kinds[0], "JSON") if kinds else "JSON"


def columns_from_schema(schema):
    """(name, type) columns from a JSON Schema's properties; without any, the whole record as one JSON column."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not properties:
        return [("record", "JSON")]
    return [(name, _schema_type(definition or {})) for name, definition in properties.items()]


def decimal_columns(columns):
    """The names of the DECIMAL(p,s) columns among (name, DuckDB type) columns (None: no columns)."""
    return [column for column, kind in columns or () if canonical(kind).startswith("DECIMAL")]


def decimals_as_text(record, columns):
    """The record, with the decimal.Decimal values of these columns as their exact text (decimal_text)."""
    if not columns:
        return record
    texts = dict((column, decimal_text(record[column])) for column in columns
                 if isinstance(record.get(column), decimal.Decimal) and record[column].is_finite())
    return dict(record, **texts) if texts else record


def json_value(column, kind):
    """The select item that reads a column of a staged record (`record`) as its storage type (null stays null)."""
    path = sql_string(json_path(column))
    kind = storage_type(kind)
    base = kind.split("(", 1)[0]
    if kind == "JSON":
        return "nullif(json_extract(record, %s), 'null'::JSON) AS %s" % (path, quote(column))
    # (DECIMAL values are staged as their exact text)
    if base in ("VARCHAR", "DATE", "TIME", "TIMESTAMP", "DECIMAL") or kind == "TIMESTAMP WITH TIME ZONE":
        return "json_extract_string(record, %s)::%s AS %s" % (path, kind, quote(column))
    return "json_extract(record, %s)::%s AS %s" % (path, kind, quote(column))


def staged_table(path):
    """The SQL table function that reads a staged file: one row (ordinal BIGINT, record JSON) per line."""
    return "read_json(%s, format='newline_delimited', columns={'ordinal':'BIGINT','record':'JSON'})" % (
        sql_string(path))


def typed_columns(columns, whole_record=False):
    """
    The select list that types the rows of a staged file (staged_table): `_streamwright_ordinal`, then each (name, type)
    column as its storage type; with `whole_record`, the record as one JSON column, `record`.
    """
    if whole_record:
        return ["ordinal AS _streamwright_ordinal", "record::JSON AS %s" % quote("record")]
    return ["ordinal AS _streamwright_ordinal"] + [json_value(column, kind) for column, kind in columns]


class Staging(object):
    """
    The records of a run's exports, staged in a private temporary folder (`directory`, removed by remove()) until the
    run ends. exports[name] = dict(path, handle, schema, keys, columns, count, whole_record, decimals): `columns` are
    the (name, storage type) columns write_columns gave (None without them), `count` the records written,
    `whole_record` whether the records are stored as one JSON column (no columns and a schema without properties),
    and `decimals` the DECIMAL columns, whose values are staged as their exact text.
    """

    def __init__(self, prefix):
        self.directory = tempfile.mkdtemp(prefix=prefix)
        self.exports = {}
        self.columns = {}

    def write_columns(self, name, columns):
        self.columns[name] = [(column, storage_type(kind)) for column, kind in columns]

    def write_schema(self, name, schema, key_properties):
        path = os.path.join(self.directory, "%d.jsonl" % len(self.exports))
        self.exports[name] = {
            "path": path, "handle": open(path, "w", encoding="utf-8"), "schema": schema,
            "keys": list(key_properties or []), "columns": self.columns.get(name), "count": 0,
            "whole_record": self.columns.get(name) is None and not schema.get("properties"),
            "decimals": decimal_columns(self.columns.get(name))}
        LOG.debug("export %r: staging its records in %s", name, path, extra={"export": name})

    def write_record(self, name, record):
        export = self.exports[name]
        export["count"] += 1
        line = to_json({"ordinal": export["count"], "record": decimals_as_text(record, export["decimals"])},
                       default=to_text, sort_keys=True)
        export["handle"].write(line + "\n")

    def columns_of(self, name):
        """An export's (name, storage type) columns: write_columns' or, without them, its schema's."""
        export = self.exports[name]
        return export["columns"] or columns_from_schema(export["schema"])

    def close(self):
        """Closes the staged files: they hold every record."""
        for export in self.exports.values():
            export["handle"].close()

    def remove(self):
        LOG.debug("removing the staged records in %s", self.directory)
        shutil.rmtree(self.directory, ignore_errors=True)
