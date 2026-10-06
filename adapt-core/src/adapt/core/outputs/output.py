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
Writers for a run's SCHEMA / RECORD / STATE output: Singer messages, files or warehouse loaders.

Every output has summary(): what it wrote, one entry (a dict) per export, in the order the exports were first
written. Call it after close(). Keys:

- export: the export's name; records: the number of records the run wrote to it.
- path and bytes (jsonl, csv, tsv and parquet files): the export's file (the output directory joined with its name)
  and its size in bytes, once it is written.
- table, disposition and primary_key (duckdb, ducklake and dlt): the table, as SCHEMA.TABLE (dlt: DATASET.TABLE, as
  dlt names it), how it was loaded - merge (on primary_key), replace, append, or skipped (an unkeyed export of a
  full-refresh stream that skipped partitions: its table keeps the rows of the last complete run) - and its key.
- state: where the run's state went, when it was saved: DIR/state.json, SCHEMA._adapt_state,
  DATASET._dlt_pipeline_state, or stdout for Singer STATE messages.

After close(failed=True), or a close() that failed, the outputs that write when a run ends have no entries: nothing
was written. Singer messages go out while the run goes, so SingerOutput describes them in any case.

Outputs log on the logger `adapt.output`: one INFO line per entry when they close successfully (log_summary), their
warnings, and staging details at DEBUG.

Records keep the exact values of DECIMAL columns (decimal.Decimal): JSON (Singer messages, jsonl files) writes them
as exact numbers, csv and tsv files as the same text (templates.decimal_text: 2.6, 100, 12345678901234567890.123456),
and DuckDB, DuckLake and Parquet store them in DECIMAL columns (dlt too, in the columns it makes).
"""

import csv
import datetime
import decimal
import json
import logging
import os
import re
import stat
import sys
import tempfile

from adapt.core.config.inputs import InputError
from adapt.core.outputs.staging import Staging, quote, sql_string, staged_table, storage_type, typed_columns
from adapt.core.runtime.templates import TemplateError, expressions, render, to_json, to_text
from adapt.core.outputs.exporter import _AtomicFile
from adapt.core.validation.schema import split_outside_quotes


__all__ = ["SingerOutput", "FileOutput", "ParquetOutput", "open_output", "export_files", "log_summary",
           "parquet_type", "FILE_FORMATS", "FILE_NAME_OUTPUTS", "FILE_NAME_SCOPES"]

LOG = logging.getLogger("adapt.output")
# --output FORMAT:DIR: one file per export, plus state.json
FILE_FORMATS = ("jsonl", "csv", "tsv", "parquet")
FILE_NAME_OUTPUTS = "--file-name works only with --output jsonl:DIR, csv:DIR, tsv:DIR or parquet:DIR"
# what --file-name can use: {{ export }}, {{ source }}, {{ today }}, {{ timestamp }} and {{ config.NAME }}
FILE_NAME_SCOPES = ("export", "source", "today", "timestamp")
STATE_FILE = "state.json"
# the summary() keys that log records carry as fields (for structured logs), with the event "output"
_LOG_FIELDS = ("export", "records", "path", "bytes", "table")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _counted(count, word):
    """1 record, 1,234 records."""
    return "{:,} {}{}".format(count, word, "" if count == 1 else "s")


def _size(count):
    """A number of bytes for people: 512 bytes, 41.2 KB, 3.0 MB (1 KB is 1,024 bytes)."""
    if count < 1024:
        return _counted(count, "byte")
    size = float(count)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if round(size, 1) < 1024:
            return "%.1f %s" % (size, unit)
    return "%.1f TB" % (size / 1024)


def _summary_line(entry, where):
    """(format, arguments) of the log line of a summary() entry; `where`: where Singer messages went."""
    records = entry["records"]
    if "table" in entry:
        if entry.get("disposition") == "skipped":
            return ("did not load %s: %s (its stream skipped partitions: the table keeps the rows of the last "
                    "complete run)", (entry["table"], _counted(records, "row")))
        how = entry.get("disposition")
        if how == "merge":
            how = "merge on %s" % ", ".join(entry.get("primary_key") or ())
        return "loaded %s: %s (%s)", (entry["table"], _counted(records, "row"), how)
    if "path" in entry:
        return "wrote %s: %s, %s", (entry["path"], _counted(records, "record"), _size(entry.get("bytes") or 0))
    return "wrote %r to %s: %s", (entry["export"], where, _counted(records, "record"))


def log_summary(entries, where="stdout"):
    """
    Logs one INFO line per summary() entry on the logger adapt.output, with the event "output" and the entry's
    export, records, path, bytes and table as attributes of the log record (fields of structured logs):

        wrote out/campaigns.2026-10-04.112233123456.k2j4.jsonl: 29 records, 41.2 KB
        loaded acme_google.campaigns: 29 rows (merge on customer_id, campaign_id)
        wrote 'campaigns' to stdout: 29 records
    """
    for entry in entries:
        line, arguments = _summary_line(entry, where)
        fields = dict((key, entry[key]) for key in _LOG_FIELDS if key in entry)
        LOG.info(line, *arguments, extra=dict(fields, event="output"))


class SingerOutput(object):
    """Newline-delimited Singer messages, readable by Singer targets."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.where = "stdout" if self.stream is sys.stdout else str(getattr(self.stream, "name", "stream"))
        self.records = {}  # export -> records written, in the order of their schemas
        self.state_written = False

    def _write(self, message):
        self.stream.write(to_json(message, default=to_text, sort_keys=True) + "\n")

    def write_schema(self, name, schema, key_properties):
        self._write({"type": "SCHEMA", "stream": name, "schema": schema, "key_properties": key_properties})
        self.records.setdefault(name, 0)

    def write_record(self, name, record):
        self._write({"type": "RECORD", "stream": name, "record": record, "time_extracted": _now()})
        self.records[name] = self.records.get(name, 0) + 1

    def write_state(self, state):
        self._write({"type": "STATE", "value": state})
        self.state_written = True

    def summary(self):
        """
        One entry per export (see the module's documentation): export, records, and state (stdout) once a STATE
        message was written. The messages go out while the run goes, so a failed run's are described too.
        """
        entries = []
        for name, count in self.records.items():
            entry = {"export": name, "records": count}
            if self.state_written:
                entry["state"] = self.where
            entries.append(entry)
        return entries

    def close(self, failed=False):
        self.stream.flush()
        if not failed:
            log_summary(self.summary(), self.where)


class FileOutput(object):
    """
    One file per export plus state.json, written atomically (into a hidden temporary file, renamed into place when
    the run succeeds): `<export>.<timestamp>.<unique>.<jsonl|csv|tsv>` in the directory, or the path --file-name gives
    the export (`file_names`: export -> path inside the directory, whose folders are made when the run succeeds).
    A directory whose state.json is a folder is refused, and closing writes no file if a --file-name path would
    leave the directory through a symbolic link.

    jsonl files hold one JSON object per line. csv and tsv files (the csv module's excel and excel-tab dialects:
    comma- or tab-separated, rows ending with CRLF) start with a header row of the export's columns; a null is an
    empty field, a DECIMAL its exact value (templates.decimal_text), a list is comma-joined (templates.to_text) and
    a mapping is JSON. A field that holds the separator, a quote, a carriage return or a newline is quoted, its quotes
    doubled.
    """

    def __init__(self, directory, file_format, file_names=None):
        state = os.path.join(directory, STATE_FILE)
        if os.path.isdir(state):  # (before any request: the run would fail when it ends)
            raise ValueError("%s is a folder: the run writes its state to that path" % state)
        self.directory = directory
        self.format = file_format
        self.file_names = file_names
        self.files = {}  # export -> dict(temporary, path, records; handle and writer while it is written; bytes)
        self.state = None
        self.status = None  # "written" once close() has put the files in place, "failed" if it wrote none
        os.makedirs(directory, exist_ok=True)

    def _reserve(self, name):
        """(temporary, path): an export's hidden temporary file, made in the directory, and the path it becomes."""
        if self.file_names is None:
            target = _AtomicFile(name, self.directory, "." + self.format)
            temporary, path = target.tmp_path, target.path
        else:
            if name not in self.file_names:
                raise ValueError("--file-name: export %r has no file name" % name)
            path = os.path.join(self.directory, self.file_names[name])
            descriptor, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".part",
                                                     dir=self.directory)
            os.close(descriptor)
        LOG.debug("export %r: its file is %s until the run succeeds, then %s", name, temporary, path,
                  extra={"export": name, "path": path})
        return temporary, path

    def write_schema(self, name, schema, key_properties):
        temporary, path = self._reserve(name)
        handle = open(temporary, "w", encoding="utf-8", newline="")
        writer = None
        if self.format in ("csv", "tsv"):
            writer = csv.DictWriter(handle, fieldnames=list(schema.get("properties") or {}), extrasaction="ignore",
                                    dialect="excel-tab" if self.format == "tsv" else "excel")
            writer.writeheader()
        self.files[name] = {"temporary": temporary, "path": path, "handle": handle, "writer": writer, "records": 0}

    def write_record(self, name, record):
        item = self.files[name]
        if self.format == "jsonl":
            item["handle"].write(to_json(record, default=to_text, sort_keys=True) + "\n")
        else:
            item["writer"].writerow(dict((k, to_text(v) if isinstance(v, (dict, list, decimal.Decimal)) else v)
                                         for k, v in record.items()))
        item["records"] += 1

    def write_state(self, state):
        self.state = state

    def summary(self):
        """
        One entry per export (see the module's documentation): export, records, path, and bytes and state
        (DIR/state.json, when the run saved one) once the files are written. No entries after a failed run: no file
        was written.
        """
        if self.status == "failed":
            return []
        entries = []
        for name, item in self.files.items():
            entry = {"export": name, "records": item["records"], "path": item["path"]}
            if "bytes" in item:
                entry["bytes"] = item["bytes"]
            if self.status == "written" and self.state is not None:
                entry["state"] = os.path.join(self.directory, STATE_FILE)
            entries.append(entry)
        return entries

    def _close_files(self):
        for item in self.files.values():
            item["handle"].close()

    def _write_files(self):
        """Fills the temporary files before they are renamed into place (jsonl, csv and tsv ones hold the records)."""

    def _cleanup(self):
        """Removes what the output kept while the run went, whether or not it wrote its files."""

    def close(self, failed=False):
        self.status = "failed"
        written = False
        try:
            self._close_files()
            # a link made during the run cannot take a --file-name outside the directory: then no file is written
            outside = [] if failed or self.file_names is None else [
                item["path"] for item in self.files.values() if not _inside(self.directory, item["path"])]
            if outside:
                raise ValueError("--file-name: %s %s outside %s through a symbolic link: no file was written" % (
                    ", ".join(repr(os.path.relpath(path, self.directory)) for path in outside),
                    "leads" if len(outside) == 1 else "lead", self.directory))
            if failed:
                return
            self._write_files()
            for item in self.files.values():
                if os.path.dirname(item["path"]):
                    os.makedirs(os.path.dirname(item["path"]), exist_ok=True)
                item["bytes"] = os.path.getsize(item["temporary"])
                os.replace(item["temporary"], item["path"])
            if self.state is not None:
                with _AtomicFile("state", self.directory, ".json") as target:
                    with open(target.tmp_path, "w", encoding="utf-8") as stream:
                        json.dump(self.state, stream, indent=2, sort_keys=True)
                os.replace(target.path, os.path.join(self.directory, STATE_FILE))
            written = True
        finally:
            try:
                for item in [] if written else self.files.values():
                    if os.path.exists(item["temporary"]):
                        os.remove(item["temporary"])
            finally:
                self._cleanup()
        self.status = "written"
        log_summary(self.summary())


def parquet_type(kind):
    """
    The Parquet column type of a DuckDB column type: the type DuckDB tables store it as (staging.storage_type), except
    that HUGEINT and UHUGEINT are DECIMAL(38,0), exact where DuckDB would write a DOUBLE (values of more than 38 digits
    fail the run), and JSON (nested lists, structs, maps and JSON values) is its text, VARCHAR.
    """
    kind = storage_type(kind)
    if kind in ("HUGEINT", "UHUGEINT"):
        return "DECIMAL(38,0)"
    return "VARCHAR" if kind == "JSON" else kind


def _parquet_column(column, kind):
    """The select item of a column (of a storage type, see typed_columns) in a Parquet file."""
    target = parquet_type(kind)
    return quote(column) if target == kind else "CAST(%s AS %s) AS %s" % (quote(column), target, quote(column))


class ParquetOutput(FileOutput):
    """
    One Parquet file per export (zstd compression) plus state.json, named and written atomically like FileOutput's
    files, with the export's DuckDB column types (write_columns; without them, its schema's) as parquet_type maps them:
    DECIMAL(p,s), DATE, TIMESTAMP WITH TIME ZONE, BIGINT, BOOLEAN, DOUBLE, VARCHAR... Records are staged in a private
    temporary folder while the run goes (staging.Staging), so memory stays flat; when the run succeeds, a DuckDB of
    its own (not the run's locked-down sandbox) types them and writes each export's file into its hidden temporary
    file, which is then renamed into place. A failed run writes no files; closing removes the staged records.
    """

    def __init__(self, directory, file_names=None):
        super(ParquetOutput, self).__init__(directory, "parquet", file_names)
        self.staging = Staging("adapt-parquet-")

    def write_columns(self, name, columns):
        self.staging.write_columns(name, columns)

    def write_schema(self, name, schema, key_properties):
        temporary, path = self._reserve(name)
        self.files[name] = {"temporary": temporary, "path": path, "records": 0}
        self.staging.write_schema(name, schema, key_properties)

    def write_record(self, name, record):
        self.staging.write_record(name, record)
        self.files[name]["records"] += 1

    def _close_files(self):
        self.staging.close()

    def _write_files(self):
        if not self.files:
            return
        import duckdb
        # it reads the staged files and writes the Parquet ones in the records' order, spilling to the staging folder
        connection = duckdb.connect(":memory:", config={
            "temp_directory": os.path.join(self.staging.directory, "duckdb"), "preserve_insertion_order": True})
        try:
            connection.execute("SET TimeZone = 'UTC'")
            for name, item in self.files.items():
                export, columns = self.staging.exports[name], self.staging.columns_of(name)
                rows = "SELECT %s FROM %s" % (", ".join(typed_columns(columns, export["whole_record"])),
                                              staged_table(export["path"]))
                select = ", ".join(_parquet_column(column, kind) for column, kind in columns)
                LOG.debug("export %r: DuckDB writes %s from %s, columns %s", name, item["temporary"], export["path"],
                          ", ".join("%s %s" % (column, parquet_type(kind)) for column, kind in columns),
                          extra={"export": name, "path": item["path"]})
                mode = stat.S_IMODE(os.stat(item["temporary"]).st_mode)
                try:
                    # straight into the hidden file, which is the temporary one (DuckDB removes it if it fails)
                    connection.execute("COPY (SELECT %s FROM (%s)) TO %s (FORMAT parquet, COMPRESSION zstd, "
                                       "USE_TMP_FILE false)" % (select, rows, sql_string(item["temporary"])))
                except duckdb.Error as exc:
                    message = " ".join(line.strip() for line in str(exc).strip().split("\n\n")[0].splitlines()
                                       if line.strip())
                    if "DECIMAL(38,0)" in message and any(kind in ("HUGEINT", "UHUGEINT") for _, kind in columns):
                        message += " (HUGEINT and UHUGEINT columns are DECIMAL(38,0) in Parquet files: 38 digits)"
                    raise ValueError("export %r: cannot write its Parquet file: %s" % (name, message))
                os.chmod(item["temporary"], mode)  # the permissions of the other files, should DuckDB make a new one
        finally:
            connection.close()

    def _cleanup(self):
        self.staging.remove()


def _inside(directory, path):
    """Whether the file `path` (in `directory`) is inside it once symbolic links are followed."""
    root = os.path.normcase(os.path.realpath(directory))
    target = os.path.normcase(os.path.realpath(path))
    try:
        return target != root and os.path.commonpath([root, target]) == root
    except ValueError:  # (on another drive)
        return False


def _file_problem(path):
    """Why a rendered --file-name is not the path of a file inside the output directory, or None."""
    if not path.strip():
        return "is empty"
    if "\x00" in path:
        return "holds a NUL character"
    if os.path.isabs(path) or os.path.splitdrive(path)[0] or path.startswith(("/", "\\")):
        return "is not a relative path"
    parts = path.replace(os.sep, "/").replace(os.altsep or "/", "/").split("/")
    if ".." in parts:
        return "goes outside the output directory (..)"
    if parts[-1] in ("", "."):
        return "names a folder, not a file"
    normalized = os.path.normpath(path)
    first = normalized.replace(os.sep, "/").replace(os.altsep or "/", "/").split("/")[0]
    if first.lower() == STATE_FILE:  # (some file systems ignore case)
        return "is the state file's name" if first == normalized else \
            "needs the folder %r, the state file's name" % first
    return None


def export_files(template, exports, scopes, directory=None):
    """
    --file-name: the file of each export (export -> path inside the output directory), `template` rendered with
    {{ export }} and `scopes` (source, today, timestamp and config: values, never secrets). Raises ValueError, with
    every problem, for references it cannot render and for paths that are not inside the directory, that start with
    a folder named like the state file, that two exports share, that are (or need) folders that are files there, or
    that lead outside it through symbolic links.
    """
    config = scopes.get("config") or {}
    problems = []
    for expression in expressions(template):
        scope, _, attribute = split_outside_quotes(expression, "|")[0].strip().partition(".")
        if scope == "config" and attribute:
            if attribute not in config:
                problems.append("{{ %s }}: %r is not a config input (declared: %s)" % (
                    expression.strip(), attribute, ", ".join(sorted(config)) or "none"))
            elif config[attribute] is None:
                problems.append("{{ %s }}: config %r has no value (give it with --set or --config)" % (
                    expression.strip(), attribute))
        elif scope not in FILE_NAME_SCOPES or attribute:
            problems.append("{{ %s }}: a file name can use {{ export }}, {{ source }}, {{ today }}, {{ timestamp }} "
                            "and {{ config.NAME }}" % expression.strip())
    if problems:
        raise ValueError("--file-name: %s" % "; ".join(problems))
    paths, owners = {}, {}  # export -> path; normalized path (ignoring case) -> export
    for export in exports:
        try:
            value = render(template, dict(scopes, export=export))
        except (TemplateError, InputError, TypeError, ValueError) as exc:
            raise ValueError("--file-name: %s" % exc)
        path = value if isinstance(value, str) else to_text(value)
        problem = _file_problem(path)
        if problem:
            problems.append("%r (export %r) %s" % (path, export, problem))
            continue
        paths[export] = os.path.normpath(path)
        key = os.path.normcase(paths[export]).lower()  # some file systems ignore case
        if key in owners:
            problems.append("exports %r and %r get the same file %r: use {{ export }}" % (
                owners[key], export, paths[export]))
        owners.setdefault(key, export)
    for export, path in paths.items():
        folder = os.path.dirname(path)
        while folder:
            if os.path.normcase(folder).lower() in owners:
                problems.append("%r is the file of export %r and a folder of export %r's file %r" % (
                    folder, owners[os.path.normcase(folder).lower()], export, path))
            elif directory is not None and os.path.isfile(os.path.join(directory, folder)):
                problems.append("%r (export %r) needs the folder %r, a file in %s" % (path, export, folder, directory))
            folder = os.path.dirname(folder)
        if directory is not None and os.path.isdir(os.path.join(directory, path)):
            problems.append("%r (export %r) is a folder in %s" % (path, export, directory))
        elif directory is not None and not _inside(directory, os.path.join(directory, path)):
            problems.append("%r (export %r) leads outside %s through a symbolic link" % (path, export, directory))
    if problems:
        raise ValueError("--file-name: %s" % "; ".join(problems))
    return paths


def open_output(spec, source=None, file_names=None):
    """'singer' (default), 'jsonl:DIR', 'csv:DIR', 'tsv:DIR', 'parquet:DIR', 'dlt:DESTINATION[:DATASET]',
    'duckdb:PATH[:SCHEMA]' or 'ducklake:CATALOG[:SCHEMA]' (the dataset / schema defaults to the source name; a
    DuckLake CATALOG can be 'postgres:DSN', a libpq connection string: ducklake:postgres:DSN[:SCHEMA]).
    file_names (--file-name, see export_files): each export's file, for the file outputs (FILE_FORMATS) only."""
    kind, _, directory = (spec or "singer").partition(":")
    if file_names is not None and kind not in FILE_FORMATS:
        raise ValueError(FILE_NAME_OUTPUTS)
    if spec in (None, "", "singer"):
        return SingerOutput()
    source = source or {"name": "source", "streams": []}
    if kind == "dlt":
        destination, _, dataset = directory.partition(":")
        if not destination:
            raise ValueError("--output dlt needs a destination, e.g. dlt:duckdb or dlt:bigquery:DATASET")
        from adapt.core.outputs.dlt_output import DltOutput
        return DltOutput(destination, dataset or None, source)
    if kind in ("duckdb", "ducklake"):
        path, _, schema = directory.partition(":")
        if kind == "ducklake" and path == "postgres":
            # postgres:DSN[:SCHEMA]: a libpq connection string (spaces and all); a last `:NAME` is the schema
            path, colon, schema = directory.rpartition(":")
            if not (colon and path != "postgres" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", schema)):
                path, schema = directory, ""
        if not path:
            raise ValueError("--output %s needs a path, e.g. %s:warehouse.duckdb or %s:catalog.ducklake:SCHEMA" % (
                kind, kind, kind))
        from adapt.core.outputs.warehouse import DuckDBOutput, DuckLakeOutput
        cls = DuckDBOutput if kind == "duckdb" else DuckLakeOutput
        return cls(path, schema or source["name"], source)
    if kind not in FILE_FORMATS or not directory:
        raise ValueError("--output must be singer, jsonl:DIR, csv:DIR, tsv:DIR, parquet:DIR, "
                         "dlt:DESTINATION[:DATASET], duckdb:PATH[:SCHEMA] or ducklake:CATALOG[:SCHEMA], got %r" % spec)
    if kind == "parquet":
        return ParquetOutput(directory, file_names)
    return FileOutput(directory, kind, file_names)
