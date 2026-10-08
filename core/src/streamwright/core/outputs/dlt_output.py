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
Loading a run into a dlt destination (https://dlthub.com/docs): `streamwright run SOURCE --output dlt:DESTINATION[:DATASET]`.

Records are staged in local JSONL files while the source runs. When the run succeeds, dlt loads every export into a
table of the same name, with column types from its step (DECIMAL(p,s) columns are dlt decimals, whose values are
loaded exactly; a column that a table already has keeps its type):
- exports with a `primary_key` are merged on it (on the filesystem destination they are Delta tables, which dlt can
  merge; this needs dlt[deltalake]);
- exports of full-refresh streams without one replace their table (an incomplete stream's keep the last complete one);
- exports of incremental streams without one are appended (the days read again, lookback and today, are loaded
  again).

The run's state is saved with the load, and every run takes it from the destination: each run uses a new dlt working
folder, so runs into different warehouses never share state, and a failed load leaves nothing behind (the next run
reads again from the last completed load). A failed run loads nothing.

Needs dlt and the destination's extra, e.g. pip install "streamwright[dlt]" "dlt[duckdb]". Destination
credentials come from dlt's configuration: environment variables (DESTINATION__<NAME>__CREDENTIALS...) or
.dlt/secrets.toml.
"""

import decimal
import importlib.util
import json
import logging
import os
import re
import shutil
import tempfile

from streamwright.core.engine import sql
from streamwright.core.outputs.output import log_summary
from streamwright.core.outputs.staging import canonical, decimals_as_text
from streamwright.core.runtime.templates import to_json, to_text


__all__ = ["DltOutput", "column_hints", "STATE_KEY"]

LOG = logging.getLogger("streamwright.output")
STATE_KEY = "streamwright"  # the run's state, in the dlt source state
_STATE_TABLE = "_dlt_pipeline_state"  # where dlt keeps its pipelines' state (with their sources'), in the dataset
_TYPES = {"string": "text", "integer": "bigint", "number": "double", "boolean": "bool", "object": "json",
          "array": "json"}
_FORMATS = {"date": "date", "date-time": "timestamp"}
_DECIMAL = re.compile(r"^DECIMAL\((\d+),(\d+)\)$")


def _import_dlt():
    os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")  # no usage telemetry to dltHub unless asked
    try:
        import dlt
    except ImportError:
        raise ValueError('--output dlt:... needs dlt: pip install "streamwright[dlt]" and the '
                         'destination\'s extra, e.g. "dlt[duckdb]"')
    return dlt


def _decimal_types(columns):
    """{name: (precision, scale)} of the DECIMAL(p,s) columns among (name, DuckDB type) columns."""
    types = {}
    for column, kind in columns:
        match = _DECIMAL.match(canonical(kind).replace(" ", ""))
        if match:
            types[column] = (int(match.group(1)), int(match.group(2)))
    return types


def column_hints(schema, decimals=None):
    """
    dlt column hints from a stream's JSON Schema (columns without a type, such as JSON, get none); `decimals`:
    {name: (precision, scale)} of its DECIMAL columns, which are dlt decimals (exact) instead of doubles.
    """
    hints = {}
    decimals = decimals or {}
    for name, definition in (schema.get("properties") or {}).items():
        if name in decimals:
            precision, scale = decimals[name]
            hints[name] = {"data_type": "decimal", "precision": precision, "scale": scale, "nullable": True}
            continue
        types = definition.get("type") or []
        types = [kind for kind in (types if isinstance(types, list) else [types]) if kind != "null"]
        data_type = _FORMATS.get(definition.get("format")) or (_TYPES.get(types[0]) if types else None)
        if data_type:
            hints[name] = {"data_type": data_type, "nullable": True}
    return hints


class DltOutput(object):
    """One dlt resource (table) per stream; loaded on close() if the run succeeded."""

    def __init__(self, destination, dataset, source):
        self.dlt = _import_dlt()
        self.source_name = source["name"]
        self.dataset = dataset or self.source_name
        self.destination = destination
        # unkeyed exports that hold a window of their data are appended, the others replaced
        self.incremental = sql.incremental_exports(source)
        # a new working folder for every run (removed by close()): the destination is the only place state lives
        self.directory = tempfile.mkdtemp(prefix="streamwright-dlt-")
        try:
            self.pipeline = self.dlt.pipeline(pipeline_name="streamwright_%s__%s" % (self.source_name, self.dataset),
                                              destination=destination, dataset_name=self.dataset,
                                              pipelines_dir=os.path.join(self.directory, "pipelines"))
        except Exception as exc:  # e.g. an unknown destination or its missing extra
            shutil.rmtree(self.directory, ignore_errors=True)
            raise ValueError("--output dlt:%s: %s" % (destination, exc))
        self.filesystem = self.pipeline.destination.destination_type.endswith(".filesystem")
        self.streams = {}  # name -> (path, handle, schema, key properties)
        self.counts = {}  # name -> records written
        self.decimals = {}  # name -> {column: (precision, scale)} of its DECIMAL columns (write_columns)
        self.partial = set()
        self.state = None
        self.status = None  # "loaded" once close() has loaded the run, "failed" if it loaded nothing
        self.state_saved = False
        self.names = None  # _names(), taken after the load

    def initial_state(self):
        """The state saved with the last completed load in the destination."""
        self.pipeline.sync_destination()
        return ((self.pipeline.state.get("sources") or {}).get(self.source_name) or {}).get(STATE_KEY)

    def disposition(self, name, keys):
        if keys:
            return "merge"
        return "append" if name in self.incremental else "replace"

    def write_columns(self, name, columns):
        """An export's (name, DuckDB type) columns: its DECIMAL(p,s) columns are dlt decimals, loaded exactly."""
        self.decimals[name] = _decimal_types(columns)

    def write_schema(self, name, schema, key_properties):
        keys = list(key_properties or [])
        if keys and self.filesystem and importlib.util.find_spec("deltalake") is None:
            raise ValueError("stream %r: the filesystem destination merges on primary_key only in Delta tables: "
                             "pip install \"dlt[deltalake]\"" % name)
        if self.disposition(name, keys) == "append":
            LOG.warning("stream %r has no primary_key, so dlt appends it: the days read again (lookback and today) "
                        "are loaded again", name)
        path = os.path.join(self.directory, "%d.jsonl" % len(self.streams))
        self.streams[name] = (path, open(path, "w", encoding="utf-8"), schema, keys)
        self.counts[name] = 0

    def write_record(self, name, record):
        # DECIMAL values are staged as their exact text, read back as decimal.Decimal values (_resources)
        staged = decimals_as_text(record, self.decimals.get(name) or ())
        self.streams[name][1].write(to_json(staged, default=to_text, sort_keys=True) + "\n")
        self.counts[name] += 1

    def write_state(self, state):
        self.state = state

    def mark_partial(self, name):
        self.partial.add(name)

    def _kept(self, name, keys):
        """Whether the table keeps its rows: an unkeyed full-refresh export of a stream that lacks partitions."""
        return name in self.partial and self.disposition(name, keys) == "replace"

    def _resources(self):
        dlt, state = self.dlt, self.state

        def rows(path, save_state, decimals):
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    for column in decimals:
                        if isinstance(record.get(column), str):
                            record[column] = decimal.Decimal(record[column])
                    yield record
            if save_state and state is not None:  # set during extraction, so dlt saves it with the load
                dlt.current.source_state()[STATE_KEY] = state

        resources = []
        for name, (path, _, schema, keys) in self.streams.items():
            if self._kept(name, keys):
                LOG.warning("stream %r skipped partitions, so its table keeps the rows of the last complete run "
                            "(it has no primary_key to merge on)", name)
                continue
            options = {"table_format": "delta"} if keys and self.filesystem else {}
            decimals = self.decimals.get(name) or {}
            resources.append(dlt.resource(rows(path, not resources, decimals), name=name, primary_key=keys or None,
                                          write_disposition=self.disposition(name, keys),
                                          columns=column_hints(schema, decimals), **options))
        return resources

    def _names(self):
        """(name of, state table): how dlt names the dataset and tables (snake_case by default) and its state table."""
        try:
            schema = self.pipeline.default_schema
            return schema.naming.normalize_table_identifier, schema.state_table_name
        except Exception:  # (no schema before the first load)
            return (lambda name: name), _STATE_TABLE

    def summary(self):
        """
        One entry per export (see streamwright.core.outputs.output): export, records, table (DATASET.TABLE, named by dlt's
        naming convention), disposition (merge, replace, append, or skipped: the table kept its rows), primary_key,
        and state (DATASET._dlt_pipeline_state) once the load saved it. No entries after a failed run or load:
        nothing was loaded.
        """
        if self.status == "failed":
            return []
        name_of, state_table = self.names or self._names()
        dataset = name_of(self.dataset)
        entries = []
        for name, (_, _, _, keys) in self.streams.items():
            entry = {"export": name, "records": self.counts.get(name, 0), "table": "%s.%s" % (dataset, name_of(name)),
                     "disposition": "skipped" if self._kept(name, keys) else self.disposition(name, keys),
                     "primary_key": list(keys)}
            if self.state_saved:
                entry["state"] = "%s.%s" % (dataset, state_table)
            entries.append(entry)
        return entries

    def close(self, failed=False):
        self.status = "failed"
        try:
            for _, handle, _, _ in self.streams.values():
                handle.close()
            if failed:
                return
            resources = self._resources() if self.streams else []
            if resources:
                def streamwright_source():
                    return resources
                info = self.pipeline.run(self.dlt.source(streamwright_source, name=self.source_name)())
                self.state_saved = self.state is not None
                self.names = self._names()  # (before the pipeline's working folder goes)
                LOG.debug("dlt: loaded %d stream(s) into %s, dataset %r (%d load package(s))", len(resources),
                          self.destination, self.dataset, len(info.load_packages))
            self.status = "loaded"
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)
        log_summary(self.summary())
