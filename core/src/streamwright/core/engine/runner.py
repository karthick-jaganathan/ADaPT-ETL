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
Runs a validated source: each selected stream reads its requests, runs its SQL steps (sql.py) and writes its exports
to the output. Streams are self-contained: a step reads only its own stream's requests and earlier steps. A stream
runs after the streams its `from_stream` partitions come from, which run too and write their exports.
"""

import collections
import copy
import datetime
import decimal
import itertools
import json
import logging
import math
import re
import tempfile
import time
from urllib.parse import urlsplit

import requests

from streamwright.core.net import downloads
from streamwright.core.runtime import components, logs
from streamwright.core.engine import queries, sql
from streamwright.core.net.http import Authenticator, HttpClient, HttpError, RateLimiter, Redactor, error_excerpt, paginate, merge_headers
from streamwright.core.net.encoding import encode_params
from streamwright.core.net.paginators import set_dotted_path
from streamwright.core.config.inputs import as_date, parse_duration
from streamwright.core.runtime.logs import RunMetrics
from streamwright.core.runtime.components import ConnectorContext, ConnectorError, ComponentLoadError
from streamwright.core.engine.queries import QueryError
from streamwright.core.runtime.templates import TemplateError, get_path, json_numbers, render, to_json, to_text


__all__ = ["SourceError", "SourceRunner", "partition_key"]

LOG = logging.getLogger("streamwright.source")
_DAY = datetime.timedelta(days=1)


class SourceError(Exception):
    pass


# errors that `on_partition_error: skip` can skip
_PARTITION_ERRORS = (HttpError, ConnectorError, QueryError, SourceError, TemplateError, downloads.DownloadError,
                     sql.SqlError)


def partition_key(partition):
    """A partition as text (its bookmarks' key in the state): JSON, its DECIMAL values exact numbers (to_json)."""
    return to_json(partition, sort_keys=True, default=str)


def _doubles(value):
    """A value with its decimal.Decimal values (in lists and mappings too) as floats."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dict):
        return dict((key, _doubles(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_doubles(item) for item in value]
    return value


def _legacy_partition_key(partition):
    """
    The key a partition's bookmarks had before DECIMAL values were exact, when they were floats: the partition's
    JSON with its decimals as floats (1.2345678901234567e+19 for 12345678901234567890.123456).
    """
    return json.dumps(_doubles(partition), sort_keys=True, default=str)


def _partition_value(value):
    """
    A partition value taken from a column (a `from_stream` parent's export, a step's rows): a DECIMAL value is the
    float it was before DECIMAL values were exact when that float is its exact value (2.60 -> 2.6, 100 -> 100.0), so
    its text in requests and steps (`partition->>'amount'`: '100.0'), the keys made of it and its bookmarks' key stay
    as they were; else it stays the exact decimal.Decimal (12345678901234567890.123456), which a float rounded.
    """
    if isinstance(value, decimal.Decimal):
        if value.is_finite():
            number = float(value)
            if math.isfinite(number) and decimal.Decimal(repr(number)) == value:
                return number
        return value
    if isinstance(value, dict):
        return dict((key, _partition_value(item)) for key, item in value.items())
    if isinstance(value, list):
        return [_partition_value(item) for item in value]
    return value


class _Distinct(object):
    """Distinct values (also lists and mappings) in the order they were first seen."""

    def __init__(self):
        self.values = []
        self._seen = set()

    def add(self, value):
        key = partition_key(value)
        if key not in self._seen:
            self._seen.add(key)
            self.values.append(value)


def _lookup(data, path):
    try:
        return get_path(data, path)
    except KeyError:
        return None


def _as_scope(value):
    """A submit / poll response as a scope: mappings as they are, anything else as `result`."""
    return value if isinstance(value, dict) else {"result": value}


def _same(value, expected):
    return value == expected or (value is not None and expected is not None and to_text(value) == to_text(expected))


def _matches(data, condition):
    value = _lookup(data, condition["path"])
    expected = condition["in"] if "in" in condition else [condition.get("equals")]
    return any(_same(value, item) for item in expected)


def _summary(data, redact):
    """A job status in an error message: its JSON, redacted before it is cut (error_excerpt)."""
    return error_excerpt(json.dumps(data, default=str, sort_keys=True), redact)


def _http_requests(request):
    """The `http` requests in a request body, including the parts of an async job."""
    job = request.get("async_job") or {}
    return [part["http"] for part in (request, job.get("submit") or {}, job.get("results") or {}) if "http" in part]


def _first(responses):
    iterator = iter(responses)
    try:
        return next(iterator, None)
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def _days(text, what, stream, minimum):
    duration = parse_duration(text)
    if duration.total_seconds() % 86400 or duration.days < minimum:
        raise SourceError("stream %r: `%s: %s` - use whole days (at least %dd)" % (stream, what, text, minimum))
    return duration.days


def _uses_window(value):
    if isinstance(value, str):
        return re.search(r"\{\{\s*window\.", value) is not None
    if isinstance(value, dict):
        return any(_uses_window(item) for item in value.values())
    if isinstance(value, list):
        return any(_uses_window(item) for item in value)
    return False


def _values(values):
    """A partition item's rendered `values` as a list (text is comma-separated values)."""
    if isinstance(values, str):
        return [value.strip() for value in values.split(",") if value.strip()]
    if not isinstance(values, list):
        return [] if values is None else [values]
    return values


def _batches(values, size):
    """
    `batch_size` partitions: the distinct values (a missing one gives no partition), in the order they were first
    seen, as lists of up to `size` values (the last one smaller); no values give none.
    """
    distinct = _Distinct()
    for value in values:
        if value is not None:
            distinct.add(value)
    return [distinct.values[start:start + size] for start in range(0, len(distinct.values), size)]


def _partition_names(stream):
    """The names in a stream's partitions (each of them has all of these)."""
    return [name for item in stream.get("partitions") or [] for name in sql.partition_fields(item)[1]]


def _named_errors(pages, what):
    """The pages of a request, with the errors a partition can skip named after it: "request 'x': ..."."""
    try:
        for page in pages:
            yield page
    except _PARTITION_ERRORS as exc:
        raise SourceError("%s: %s" % (what, exc)) from exc


def _where(stream, request, partition, window=None):
    """
    What a read is for, in log lines and the run's metrics: its stream, request, partition (DECIMAL values as JSON
    numbers, as logs write them) and window.
    """
    where = {"stream": stream["name"], "request": request["name"], "partition": json_numbers(partition)}
    if window is not None:
        where["window"] = {"start": window[0], "end": window[1]}
    return where


class SourceRunner(object):
    """
    Runs a source's streams into `output`. `metrics` (logs.RunMetrics; by default one of its own) gets the run's
    counts and timings and logs its progress.
    """

    def __init__(self, source, config, secrets, state=None, output=None, today=None, session=None,
                 clock=time.monotonic, sleep=time.sleep, redact=None, allowed_connectors=None, metrics=None):
        self.source = source
        self.config = config
        self.secrets = secrets
        self.output = output
        self.today = today or datetime.date.today()
        self.state = copy.deepcopy(state) if state else {}
        self.state.setdefault("bookmarks", {})
        self.redact = redact or Redactor()
        for value in secrets.values():
            self.redact.add(value)
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.metrics = metrics if metrics is not None else RunMetrics(clock=clock)
        if self.metrics.source is None:
            self.metrics.source = source.get("name")
        self.streams = {}
        for stream in source["streams"]:
            self.streams.setdefault(stream["name"], stream)
        self.stream_names = dict((name.lower(), name) for name in self.streams)
        self.export_owner = {}  # export -> its stream
        for stream in source["streams"]:
            for export in sql.exports_of(stream):
                self.export_owner.setdefault(export, stream["name"])
        self.plan = None  # sql.SourcePlan, made when the run starts
        self.allowed_connectors = allowed_connectors
        self.builder_names = set(components.available("query builder"))  # loaded when a request calls one
        self.builders = {}
        self.sandbox = None  # the run's DuckDB, opened when the first stream runs
        self.incomplete = {}  # stream -> why it lacks partitions: it skipped some, or a `from_stream` parent lost some
        self.collected = {}  # parent stream -> (fields) -> _Distinct rows of their values, for `from_stream`
        auth = source.get("auth") or {}
        self.provider = auth.get("provider")
        self.connector = self.connector_auth = self.connector_client = None
        self.authenticator = None
        if self.provider:
            try:
                self.connector = components.load(self.provider, allowed_connectors)
            except ComponentLoadError as exc:
                raise SourceError(str(exc))
            if getattr(self.connector, "transport", "sdk") == "http":
                auth_block = dict((key, value) for key, value in auth.items() if key != "provider")
                rendered = render(auth_block, dict(self.scopes(), secrets=secrets))
                self.authenticator = Authenticator(rendered, self.session, self.redact)
                self.connector_auth = None
            else:
                self.connector_auth = render(dict((key, value) for key, value in auth.items() if key != "provider"),
                                             dict(self.scopes(), secrets=secrets))
            if self.allowed_connectors is not None:
                extra = getattr(self.connector, "query_builders", ())
                if extra:
                    self.allowed_connectors = list(self.allowed_connectors) + [b for b in extra if b not in self.allowed_connectors]
        else:
            rendered = render(auth, dict(self.scopes(), secrets=secrets))
            self.authenticator = Authenticator(rendered, self.session, self.redact)
        source_http = dict(source.get("http") or {})
        headers_spec = source_http.pop("headers", None)
        self.http = render(source_http, self.scopes())
        if headers_spec is not None:
            rendered_headers = render(headers_spec, dict(self.scopes(), secrets=secrets))
            if isinstance(rendered_headers, dict):
                self.http["headers"] = dict((k, v) for k, v in rendered_headers.items() if v not in ("", None))
            else:
                self.http["headers"] = rendered_headers
        # the source-level limit is shared by every stream; a stream's own `rate_limit` replaces it
        self.shared_limiter = self.limiter(self.http.get("rate_limit"))

    def scopes(self, partition=None, window=None):
        scopes = {"config": self.config, "today": self.today}
        if partition:
            scopes["partition"] = partition
        if window:
            scopes["window"] = {"start": window[0], "end": window[1]}
        return scopes

    # partitions and windows ---------------------------------------------------------

    def parent(self, item):
        """The stream a `from_stream` partition names (names ignore case, as streamwright-validate compares them)."""
        return self.stream_names.get(item["from_stream"].lower(), item["from_stream"])

    def partitions(self, stream):
        """A stream's partitions: its partition items crossed (`from_stream`: the values its parent's export had)."""
        items = stream.get("partitions") or []
        if not items:
            return [{}]
        choices = []  # per item: its partitions, each a tuple of (name, value) pairs
        for item in items:
            if "from_stream" in item:
                fields, names = sql.partition_fields(item)
                rows = (self.collected.get(self.parent(item)) or {}).get(fields)
                if rows is None:  # (the plan runs parents first)
                    raise SourceError("stream %r: its `from_stream` parent %r has not run" % (
                        stream["name"], self.parent(item)))
                choices.append([tuple(zip(names, row)) for row in rows.values])
                continue
            values = _values(render(item["values"], self.scopes()))
            choices.append([((item["name"], value),) for value in values])
        return [dict(pair for pairs in combination for pair in pairs)
                for combination in itertools.product(*choices)]

    def _check_batch_sizes(self, stream, request, items, stream_names, request_names):
        """Rejects invalid `batch_size:` on a request's partitions before the run (a whole number >= 1, and on
        `{from, fields}` exactly one field that is not a stream partition; a step may not list other partitions')."""
        for item in items:
            if "batch_size" not in item:
                continue
            size = item.get("batch_size")
            what = "stream %r: request %r: `batch_size`" % (stream["name"], request["name"])
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise SourceError("%s must be a whole number of at least 1, got %r" % (what, size))
            if "from" not in item:
                continue
            fields, names = sql.partition_fields(item)
            own = [field for field, name in zip(fields, names) if name not in stream_names]
            if "fields" in item and len(own) != 1:
                raise SourceError("%s on `{from, fields}` needs exactly one field that is not a stream partition (the "
                                  "one batched), got %s" % (what, ", ".join(map(str, names))))
            missing = [name for name in stream_names if name not in names]
            if own and missing and str(item["from"]).lower() not in request_names:
                # a step has every stream partition's rows: only its stream partition fields keep the current one's
                raise SourceError("%s on step %r would list other stream partitions' values: use `{from: %s, fields: "
                                  "[%s], batch_size: %d}`" % (what, item["from"], item["from"],
                                                              ", ".join(map(str, stream_names + own)), size))

    def request_partitions(self, stream, request):
        """
        A function: stream partition -> the partitions of a run-mode request, its own partition items crossed (each
        partition holds the stream partition's values too). Values `from:` an earlier request come from the records
        it read under the same stream partition, at dotted paths; from a step, from its rows (JSON values decoded).
        An item whose names include a stream partition's name uses only the rows with that partition's value there
        (as `from_stream` fields do). Values are distinct, and a row with a missing value gives no partition. Each
        `from:` source is read once, here, into an index by stream partition: many stream partitions stay linear.
        An item with `batch_size: N` gives one partition per list of up to N of its distinct values (_batches), made
        from the stream partition's own rows: `{from, fields, batch_size}` batches its one field that is not a stream
        partition, which names the list (the others keep only the stream partition's rows).
        """
        items = request.get("partitions") or []
        if not items:
            return lambda partition: [partition]
        stream_names = _partition_names(stream)
        request_names = dict((item["name"].lower(), item["name"]) for item in stream.get("requests") or [])
        step_names = dict((step["name"].lower(), step["name"])
                          for step in (stream.get("transform") or {}).get("steps") or [])
        self._check_batch_sizes(stream, request, items, stream_names, request_names)
        sandbox = self.workspace()
        sources = []  # per item: ("values", item) | ("from", names, own positions, index, key of a stream partition)
        for item in items:
            if "values" in item:
                sources.append(("values", item))
                continue
            fields, names = sql.partition_fields(item)
            shared = [position for position, name in enumerate(names) if name in stream_names]
            own = [position for position, name in enumerate(names) if name not in stream_names]
            index = collections.defaultdict(_Distinct)
            source = item["from"].lower()
            if source in request_names:
                for read_under, record in sandbox.raw_rows(request_names[source]):
                    row = tuple(_lookup(record, field) for field in fields)
                    if any(value is None for value in row):
                        continue
                    base = dict((name, read_under.get(name)) for name in stream_names)
                    if all(_same(row[position], base[names[position]]) for position in shared):
                        index[partition_key(base)].add(tuple(row[position] for position in own))
                sources.append(("from", names, own, index, lambda partition: partition_key(sql.as_json(partition))))
                continue
            for row in sandbox.distinct_rows(step_names.get(source, item["from"]), fields):
                row = tuple(_partition_value(value) for value in row)
                if all(value is not None for value in row):  # (JSON nulls)
                    index[tuple(to_text(row[position]) for position in shared)].add(
                        tuple(row[position] for position in own))
            sources.append(("from", names, own, index, lambda partition, shared=shared, names=names: tuple(
                to_text(partition[names[position]]) for position in shared)))

        def resolve(partition):
            choices = []
            for item, source in zip(items, sources):
                batch = item.get("batch_size")
                if source[0] == "values":
                    values = _values(render(item["values"], self.scopes(partition)))
                    if item["name"] in partition:  # (streamwright-validate rejects it) only the stream partition's own value
                        choices.append([()] if any(_same(value, partition[item["name"]]) for value in values) else [])
                    elif batch:
                        choices.append([((item["name"], chunk),) for chunk in _batches(values, batch)])
                    else:
                        choices.append([((item["name"], value),) for value in values])
                    continue
                _, names, own, index, key = source
                rows = index.get(key(partition))
                rows = rows.values if rows is not None else []
                if batch and own:  # its one own field (checked above): this stream partition's values, in lists
                    choices.append([((names[own[0]], chunk),) for chunk in _batches([row[0] for row in rows], batch)])
                    continue
                choices.append([tuple((names[position], value) for position, value in zip(own, row))
                                for row in rows])
            return [dict(partition, **dict(pair for pairs in combination for pair in pairs))
                    for combination in itertools.product(*choices)]
        return resolve

    def bookmark(self, name, partition):
        """
        The saved cursor of a stream partition, or None. A bookmark saved before DECIMAL values were exact, under the
        partition's key with its decimals as floats (_legacy_partition_key), is moved to its key (partition_key).
        """
        bookmarks = self.state["bookmarks"].get(name) or {}
        key = partition_key(partition)
        if key not in bookmarks:
            legacy = _legacy_partition_key(partition)
            if legacy != key and legacy in bookmarks:
                self.unwritten_change()
                bookmarks[key] = bookmarks.pop(legacy)
        return bookmarks.get(key)

    def windows(self, stream, partition):
        """
        Day windows to read. The saved cursor is the last complete day; reading resumes the day after it,
        `lookback` days earlier (but not before `start`, unless the cursor itself is older than `start`).
        """
        incremental = stream.get("incremental")
        if not incremental:
            return [None]
        name = stream["name"]
        start = as_date(render(incremental["start"], self.scopes()), self.today)
        size = _days(incremental["window"], "window", name, 1) if incremental.get("window") else None
        lookback = _days(incremental["lookback"], "lookback", name, 0) if incremental.get("lookback") else 0
        cursor = self.bookmark(name, partition)
        begin = start
        if cursor:
            resume = as_date(cursor) + _DAY
            begin = max(min(start, resume), resume - datetime.timedelta(days=lookback))
        windows = []
        while begin <= self.today:
            end = self.today if size is None else min(begin + datetime.timedelta(days=size - 1), self.today)
            windows.append((begin, end))
            begin = end + _DAY
        return windows

    # running -------------------------------------------------------------------

    def limiter(self, limit):
        if not limit:
            return None
        return RateLimiter(limit["requests"], parse_duration(limit["per"]).total_seconds(), self.clock, self.sleep)

    def stream_limiter(self, stream):
        return self.limiter(stream["rate_limit"]) if stream.get("rate_limit") else self.shared_limiter

    def retry_policy(self, stream):
        return stream.get("retry") or self.http.get("retry")

    def client(self, stream, limiter=None):
        return HttpClient(base_url=self.http.get("base_url"), headers=self.http.get("headers"),
                          authenticator=self.authenticator, retry=self.retry_policy(stream),
                          rate_limiter=limiter if limiter is not None else self.stream_limiter(stream),
                          redact=self.redact, session=self.session, sleep=self.sleep, metrics=self.metrics,
                          clock=self.clock)

    def download_client(self, stream):
        """Report files are fetched without the API's credentials: their URLs are pre-signed."""
        return HttpClient(retry=self.retry_policy(stream), redact=self.redact, session=self.session, sleep=self.sleep,
                          metrics=self.metrics, clock=self.clock)

    def connect(self, context):
        """The connector's SDK client, built once per run; failing to connect stops the run."""
        if self.connector_client is None:
            problems = self.connector.check_auth(self.connector_auth)
            if problems:
                raise SourceError("; ".join(problems))
            started = self.clock()
            try:
                self.connector_client = context.call(self.connector.connect, dict(self.connector_auth), context)
            except ConnectorError as exc:
                raise SourceError(self.redact("%s: cannot connect: %s" % (self.provider, exc)))
            seconds = self.clock() - started
            logs.NETWORK.info("%s: connected in %.2f s", self.provider, seconds,
                              extra=logs.fields(event="connect", connector=self.provider,
                                                duration_ms=int(round(seconds * 1000))))
        return self.connector_client

    def run(self, selected=None):
        """
        Runs the `selected` streams (stream or export names; default: every stream) and the streams their
        `from_stream` partitions come from, parents first. Every stream that runs writes all its exports. Every
        stream is checked (SQL, connectors, query builders) before the first request.
        """
        selected = list(selected or list(self.streams))
        unknown = [name for name in selected if name not in self.streams and name not in self.export_owner]
        if unknown:
            raise SourceError("unknown stream(s): %s (streams/exports: %s)" % (
                ", ".join(unknown), ", ".join(sorted(set(self.streams) | set(self.export_owner)))))
        try:
            # the steps are compiled against empty tables in a DuckDB of their own, so none of them stays in the
            # run's DuckDB
            planner = sql.Sandbox()
            try:
                self.plan = sql.SourcePlan(planner, self.source)
            finally:
                planner.close()
            running = self.plan.closure(selected)
            problems = self.plan.messages(running) + ["stream %r: %s" % (name, problem) for name in running
                                                      for problem in self.stream_problems(self.streams[name])]
            if problems:
                raise SourceError("; ".join(problems))
            for name in running:
                for item in self.streams[name].get("partitions") or []:
                    if "from_stream" in item:
                        fields, _ = sql.partition_fields(item)
                        self.collected.setdefault(self.parent(item), {}).setdefault(fields, _Distinct())
            for name in running:
                self.run_stream(self.streams[name])
        finally:
            if self.sandbox is not None:
                self.sandbox.close()
                self.sandbox = None
        return self.state

    def workspace(self):
        """The run's DuckDB (sql.Sandbox), opened when the first stream runs."""
        if self.sandbox is None:
            self.sandbox = sql.Sandbox()
        return self.sandbox

    def stream_problems(self, stream):
        """What stops a stream before it starts: requests its auth cannot make, connector and query builder checks."""
        problems = []
        for position, item in enumerate(stream.get("requests") or []):
            request, where = components.request_body(item), components.path_text(("requests", position))
            calls = components.sdk_calls(request)
            if calls and self.connector is None:
                problems.append("%s: sdk requests need a connector `auth.provider`" % where)
                continue
            problems += ["%s: %s" % (where, problem) for call in calls
                         for problem in components.check_call(self.connector, call, self.builder_names)]
            problems += ["%s: %s" % (components.path_text(("requests", position) + path), message)
                         for path, message, _ in components.check_queries(request, self.allowed_connectors,
                                                                          self.builder_names)]
            if "async_job" in request and "sdk" not in (request["async_job"].get("submit") or {}):
                problems.append("%s: an async job's `submit` must be an sdk request (its `poll` calls the same "
                                "service)" % where)
            if self.connector is not None:
                if getattr(self.connector, "transport", "sdk") == "http":
                    if calls:
                        problems.append("%s: %s is a REST API connector: use an http request (http: {path, method, ...}), not sdk" % (where, self.provider))
                else:
                    if _http_requests(request):
                        problems.append("%s: http requests need a built-in `auth.type`; auth provider %r only authenticates "
                                        "sdk requests" % (where, self.provider))
        return problems

    def run_stream(self, stream):
        """Runs one stream (`transform.mode`: page or run) and writes its exports."""
        name = stream["name"]
        plan = self.plan.streams[name]
        for _, _, parent in plan.parents:
            if parent in self.incomplete:  # it gave this stream fewer partitions (parents run first)
                self.incomplete.setdefault(name, "lacks the partitions its parent %r lost" % parent)
        partitions = self.partitions(stream)
        stats = self.metrics.start_stream(name, plan.mode, len(partitions),
                                          [item["name"] for item in stream.get("requests") or []])
        completed = False
        try:
            if plan.mode == "page":
                self.run_pages(stream, plan, partitions, stats)
            else:
                self.run_steps(stream, plan, partitions, stats)
            completed = True
        finally:
            self.metrics.end_stream(name, completed)

    def request_setup(self, stream):
        """The stream's HTTP client and connector context (the connector connects once per run, if a request calls
        it)."""
        limiter = self.stream_limiter(stream)
        client = self.client(stream, limiter)
        context = None
        if any(components.sdk_calls(components.request_body(item)) for item in stream.get("requests") or []):
            if self.connector and getattr(self.connector, "transport", "sdk") != "http":
                context = ConnectorContext(self.connector, self.redact, retry=self.retry_policy(stream),
                                           rate_limiter=limiter, sleep=self.sleep, metrics=self.metrics, clock=self.clock)
                self.connect(context)
        return client, context

    def parameters(self, plan, step):
        """The `$name` parameters of a step: the config values it reads (null when not given)."""
        return dict((name, self.config.get(name)) for name in plan.parameters.get(step, ()))

    def run_pages(self, stream, plan, partitions, stats):
        """
        `transform.mode: page`: each page of the stream's one request is the request's table, the steps run in list
        order, and each export's rows are written. Each export's columns and schema are written once, before the
        first page; the bookmark of a partition advances after each window. `stats`: the stream's StreamMetrics.
        """
        request = plan.requests[0][1]
        client, context = self.request_setup(stream)
        sandbox = self.workspace()
        steps = [(step["name"], plan.queries[step["name"]], self.parameters(plan, step["name"]),
                  plan.conform.get(step["name"])) for _, step in plan.steps]
        exports = [(export, step, plan.columns[step]) for export, _, step in plan.exports]
        for export, spec, step in plan.exports:
            self.write_export_schema(export, plan.columns[step], spec.get("primary_key") or [], stats)
        wanted = self.collected.get(stream["name"], {}) if len(exports) == 1 else {}
        sandbox.create_raw(request["name"])
        try:
            for partition in partitions:
                try:
                    for window in self.windows(stream, partition):
                        scope = {"partition": partition, "config": self.config, "window": window, "today": self.today}
                        where = _where(stream, request, partition, window)
                        read = self.metrics.read(where)
                        pages = self.request_pages(stream, request, client, context, self.scopes(partition, window),
                                                   where, read)
                        for page in _named_errors(pages, "request %r" % request["name"]):
                            sandbox.run_page(request["name"], page, scope, steps)
                            for export, step, columns in exports:
                                self.write_records(export, sandbox.table_records(step, columns), wanted, stats)
                        if window is not None:
                            stats.windows += 1
                            self.advance_bookmark(stream, partition, window)
                        read.done(request=False)
                except _PARTITION_ERRORS as exc:
                    self.skip_partition(stream, partition, exc, stats)
        finally:
            for table in [request["name"]] + [step[0] for step in steps]:
                sandbox.drop_table(table)
        self.mark_partial(stream, plan)

    def run_steps(self, stream, plan, partitions, stats):
        """
        `transform.mode: run`: the requests and steps run in dependency order (StreamPlan.order), each request for
        all its partitions, windows and pages, each step once. A stream partition that fails (with
        `on_partition_error: skip`) leaves no rows: they are removed from the request tables, and steps that ran on
        them run again. Then every export's key is checked, its rows written (ordered by the key), and the bookmarks
        of the partitions that completed advance; the state is written once. `stats`: the stream's StreamMetrics.
        """
        name = stream["name"]
        client, context = self.request_setup(stream)
        sandbox = self.workspace()
        partitions = [{"partition": partition, "failed": False, "windows": self.windows(stream, partition)}
                      for partition in partitions]
        steps = [step["name"] for _, step in plan.steps]
        tables, ran = [], {"steps": False, "stale": False}  # the request tables made; steps ran on rows since removed
        try:
            for kind, item in plan.order:
                if kind == "request":
                    self.run_request(stream, item, partitions, client, context, tables, ran, stats)
                else:
                    self.run_step(name, plan, item["name"])
                    ran["steps"] = True
            if ran["stale"]:
                for step in steps:
                    self.run_step(name, plan, step)
            while tables:  # the steps hold what the exports need
                sandbox.drop_table(tables.pop())
            outputs = []
            for export, spec, step in plan.exports:
                keys = spec.get("primary_key") or []
                try:
                    problem, columns = sandbox.key_problem(step, keys) if keys else None, sandbox.table_columns(step)
                except sql.SqlError as exc:
                    problem = str(exc)
                if problem:
                    raise SourceError(self.redact("stream %r: export %r: %s" % (name, export, problem)))
                outputs.append((export, step, columns, keys))
            wanted = self.collected.get(name, {}) if len(outputs) == 1 else {}
            for export, step, columns, keys in outputs:
                self.write_export_schema(export, columns, keys, stats)
                self.write_records(export, sandbox.table_records(step, columns, keys), wanted, stats)
        finally:
            for table in tables + steps:
                sandbox.drop_table(table)
        self.mark_partial(stream, plan)
        if stream.get("incremental"):
            for item in partitions:
                for window in [] if item["failed"] else item["windows"]:
                    stats.windows += 1
                    self.advance_bookmark(stream, item["partition"], window, write=False)
            self.write_state()

    def run_request(self, stream, request, partitions, client, context, tables, ran, stats):
        """
        One request of a run-mode stream: for each stream partition (and window, if it uses the window). Each stream
        partition is a unit of the run's metrics: its request partitions and windows, and a line when it completes.
        """
        sandbox = self.workspace()
        table = request["name"]
        windowed = _uses_window(components.request_body(request))
        sandbox.create_raw(table)
        tables.append(table)
        try:
            resolve = self.request_partitions(stream, request)
        except sql.SqlError as exc:
            raise SourceError(self.redact("stream %r: request %r: partitions: %s" % (stream["name"], table, exc)))
        for item in partitions:
            if item["failed"]:
                continue
            partition = item["partition"]
            windows = item["windows"] if windowed else [None]
            span = (windows[0][0], windows[-1][1]) if windows and windows[0] is not None else None
            read = self.metrics.read(_where(stream, request, partition, span))
            try:
                for request_partition in resolve(partition):
                    for window in windows:
                        scope = {"partition": request_partition, "config": self.config, "window": window,
                                 "today": self.today}
                        for page in self.request_pages(stream, request, client, context,
                                                       self.scopes(request_partition, window),
                                                       _where(stream, request, request_partition, window), read):
                            sandbox.add_raw(table, page, scope)
                if windows:
                    read.done()
            except _PARTITION_ERRORS as exc:
                self.skip_partition(stream, partition, "request %r: %s" % (table, exc), stats)
                item["failed"] = True
                for other in tables:  # the partition's rows of this and earlier requests
                    sandbox.delete_partition(other, partition)
                ran["stale"] = ran["stale"] or ran["steps"]

    def run_step(self, stream_name, plan, step):
        """One step of a run-mode stream, into its table; a failure stops the run, naming the stream and step."""
        try:
            self.workspace().materialize(step, plan.queries[step], self.parameters(plan, step),
                                         plan.conform.get(step))
        except sql.SqlError as exc:
            raise SourceError(self.redact("stream %r: step %r: %s" % (stream_name, step, exc)))

    def write_export_schema(self, export, columns, keys, stats=None):
        if hasattr(self.output, "write_columns"):
            self.output.write_columns(export, columns)
        self.output.write_schema(export, sql.table_schema(columns), list(keys))
        if stats is not None:
            stats.exports.setdefault(export, 0)

    def write_records(self, export, records, wanted, stats):
        """
        Writes an export's records, counted in `stats` (StreamMetrics); `wanted`: (fields) -> _Distinct of the values
        `from_stream` children take from them (a missing value gives no partition).
        """
        written = 0
        try:
            for record in records:
                self.output.write_record(export, record)
                written += 1
                for fields, rows in wanted.items():
                    row = tuple(_partition_value(record.get(field)) for field in fields)
                    if all(value is not None for value in row):
                        rows.add(row)
        finally:
            stats.exports[export] = stats.exports.get(export, 0) + written

    def mark_partial(self, stream, plan):
        """An incomplete stream's exports lack partitions: outputs keep the last complete table of unkeyed ones."""
        if stream["name"] in self.incomplete and hasattr(self.output, "mark_partial"):
            for export, _, _ in plan.exports:
                self.output.mark_partial(export)

    def skip_partition(self, stream, partition, error, stats):
        """Called while handling a partition's error: fails the stream, unless it can skip partitions."""
        name = stream["name"]
        if stream.get("on_partition_error", "fail") != "skip":
            raise SourceError(self.redact("stream %r, partition %s: %s" % (name, partition_key(partition), error)))
        stats.failed_partitions += 1
        self.incomplete.setdefault(name, "skipped partitions")
        LOG.warning(self.redact("stream %r: skipping partition %s: %s" % (name, partition_key(partition), error)),
                    extra=logs.fields(event="partition_skipped", stream=name, partition=json_numbers(partition)))

    def advance_bookmark(self, stream, partition, window, write=True):
        """
        Saves the last complete day of a partition's window as its bookmark; `write`: write the state now (else the
        caller writes it later, and the run's metrics keep the state written last until then).
        """
        bookmarks = self.state["bookmarks"].get(stream["name"])
        key = partition_key(partition)
        complete = min(window[1], self.today - _DAY)
        if bookmarks is None or key not in bookmarks or as_date(bookmarks[key]) < complete:
            if not write:
                self.unwritten_change()
            self.state["bookmarks"].setdefault(stream["name"], {})[key] = complete.isoformat()
        if write:
            self.write_state()

    def write_state(self):
        """
        Writes the state to the output. The run's metrics keep the state written last: the state itself, copied only
        before it changes without being written (unwritten_change), not on every write - a copy per window would
        cost a run with many partitions time quadratic in their number.
        """
        self.output.write_state(self.state)
        self.metrics.state = self.state

    def unwritten_change(self):
        """The state is about to change before it is written: the run's metrics keep a copy of the state written."""
        if self.metrics.state is self.state:
            self.metrics.state = copy.deepcopy(self.state)

    # requests --------------------------------------------------------------------

    def request_select(self, request, response):
        """The records in one response of a request: its `records.path` (or the response itself) as a list."""
        path = (request.get("records") or {}).get("path")
        if not path:
            data = response
        else:
            try:
                data = get_path(response, path)
            except (KeyError, TypeError, IndexError):
                # a response without the path's first key means a wrong path; below it, an absent or null value
                # (`{"data": null}`, an empty SOAP element) means no records
                if isinstance(response, dict) and response and path.split(".")[0] not in response:
                    raise SourceError("records path %r not found in the response" % (path,))
                return []
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        raise SourceError("records at %r are %s, expected a list" % (path, type(data).__name__))

    def request_pages(self, stream, request, client, context, scopes, where=None, read=None):
        """
        Lists of raw records, one per page, response or chunk of a downloaded file, with its own `records`. `where`:
        the stream, request, partition and window read (log lines, metrics); `read`: the unit of work
        (RunMetrics.read) that counts the pages.
        """
        body = components.request_body(request)
        effective_records = request["records"] if "records" in request else self.http.get("records")
        explode = (effective_records or {}).get("explode")

        def select(response):
            return self.request_select({"records": effective_records}, response)
        if "http" in body:
            pages = self.http_pages(body["http"], request, client, scopes, where)
        elif "sdk" in body:
            if self.connector and getattr(self.connector, "transport", "sdk") == "http":
                raise SourceError("%s is a REST API connector: use an http request (http: {path, method, ...}), not sdk" % self.provider)
            pages = self.sdk_responses(body, context, scopes, where, select)
        else:
            pages = self.async_job(stream, body["async_job"], client, context, scopes, select, where)
        if explode:
            pages = (list(self.explode(page, explode)) for page in pages)
        return pages if read is None else read.count(pages)

    def http_pages(self, request, item, client, scopes, where=None):
        """The pages of an http request; `item`: its request item (`paginator`, `records`)."""
        effective_paginator_spec = item["paginator"] if "paginator" in item else self.http.get("paginator")
        effective_records = item["records"] if "records" in item else self.http.get("records")
        effective_encoding = request.get("params_encoding") or self.http.get("params_encoding", "plain")

        unrendered_req = dict(request)
        req_headers_spec = unrendered_req.pop("headers", None)
        http = self.build(unrendered_req, render(unrendered_req, scopes))

        rendered_req_headers = render(req_headers_spec, dict(scopes, secrets=self.secrets)) if req_headers_spec else {}
        merged_headers = merge_headers(self.http.get("headers"), rendered_req_headers)

        def send(patch):
            if patch.url:
                url = client.follow(patch.url)
                res = client.request(http.get("method", "GET"), url, headers=merged_headers, fields=where)
                send.last_headers = client.last_headers
                return res

            params = dict(http.get("params") or {})
            if patch.params:
                params.update(patch.params)
            encoded_params = encode_params(params, effective_encoding)

            json_body = http.get("json")
            if patch.body:
                json_copy = copy.deepcopy(json_body) if json_body is not None else {}
                for path, val in patch.body.items():
                    set_dotted_path(json_copy, path, val)
                json_body = json_copy

            res = client.request(http.get("method", "GET"), http["path"], params=encoded_params,
                                 headers=merged_headers, json_body=json_body, fields=where)
            send.last_headers = client.last_headers
            return res

        def paginator_for_resp(response):
            if effective_paginator_spec is None:
                return None
            return render(effective_paginator_spec, dict(scopes, response=response or {}))

        select_item = {"records": effective_records}
        for _, records in paginate(send, paginator_for_resp, lambda response: self.request_select(select_item, response)):
            yield records

    def build(self, unrendered, rendered):
        """The rendered request, with the query builder calls written in the source replaced by query text."""
        calls = queries.find_calls(unrendered, self.builder_names)
        if not calls:
            return rendered
        for _, name, _ in calls:
            if name not in self.builders:
                try:
                    self.builders[name] = components.load(name, self.allowed_connectors, kind="query builder")
                except ComponentLoadError as exc:
                    raise SourceError(str(exc))
        return queries.build_queries(rendered, self.builders, calls)

    def sdk_responses(self, request, context, scopes, where=None, select=None):
        """
        Responses of an sdk request ({service, method, arguments, headers}) rendered with scopes; with `select`, the
        records of each response. `where`: the stream, request, partition and window the calls read, for the run's
        metrics and the network log lines (one per response: its page number, records, time and attempt).
        """
        unrendered = components.sdk_call(request)
        call = self.build(unrendered, render(unrendered, scopes))
        call["arguments"] = call.get("arguments") or {}
        name = "%s.%s" % (call.get("service"), call.get("method"))
        responses, number = None, 0
        try:
            while True:
                context.where = where  # the calls the connector makes now are for this read
                started = self.clock()
                try:
                    if responses is None:
                        responses = iter(self.connector.request(self.connector_client, call, context))
                    response = next(responses)
                except StopIteration:
                    return
                except ConnectorError:
                    raise
                except Exception as exc:  # SDK errors the connector did not catch
                    error = self.connector.error(exc)
                    if error is None:
                        raise
                    raise error from exc
                number += 1
                page = response if select is None else select(response)
                logs.sdk_summary(self.provider, name, where, number, None if select is None else len(page),
                                 self.clock() - started, context.attempt or 1)
                yield page
        finally:
            close = getattr(responses, "close", None)
            if close is not None:
                close()

    def async_job(self, stream, job, client, context, scopes, select, where=None):
        """The pages of an async job; `select` finds the records in a response (the request's `records`)."""
        name = stream["name"]
        submit = job["submit"]
        submitted = _first(self.sdk_responses(submit, context, scopes, where))
        scopes = dict(scopes, submit=_as_scope(submitted))
        # only a plain job ID is logged: responses can hold pre-signed URLs, which are credentials
        LOG.debug("stream %r: submitted the job%s", name, "" if submitted is None or isinstance(
            submitted, (dict, list)) else " " + to_text(submitted))
        scopes["poll"] = self.poll(name, job["poll"], submit, context, scopes, where)
        if "results" in job:
            for page in self.sdk_responses(job["results"], context, scopes, where, select):
                yield page
            return
        download = render(job["download"], scopes)
        url = download.get("url")
        if url in (None, ""):
            window = scopes.get("window")
            LOG.info("stream %r: the job finished without a file to download, which usually means no data%s", name,
                     " from %s to %s" % (window["start"], window["end"]) if window else "",
                     extra=logs.fields(event="no_file", **(where or {"stream": name})))
            return
        if not isinstance(url, str):
            raise SourceError("the download url is %s, expected text" % type(url).__name__)
        self.redact.add(urlsplit(url).query)  # pre-signed URLs carry credentials
        file_format = download["format"]
        with tempfile.TemporaryFile() as handle:
            size = self.download_client(stream).download(url, handle, where)
            LOG.info("stream %r: downloaded %d bytes", name, size,
                     extra=logs.fields(event="download", bytes=size, **(where or {"stream": name})))
            for page in downloads.read(handle, file_format, download.get("compression")):
                yield select(page) if file_format == "json" else page

    def poll(self, name, poll, submit, context, scopes, where=None):
        """Calls the poll method every `every` until `done_when` matches; fails on `fail_when` or `timeout`."""
        every = parse_duration(poll["every"]).total_seconds()
        timeout = parse_duration(poll["timeout"]).total_seconds()
        call = components.poll_call(submit, poll)
        paths = [poll["done_when"]["path"]]  # the status fields that are logged (not URLs, which can be credentials)
        if poll.get("fail_when") and poll["fail_when"]["path"] not in paths:
            paths.append(poll["fail_when"]["path"])
        started = self.clock()
        while True:
            status = _as_scope(_first(self.sdk_responses(call, context, scopes, where)))
            LOG.debug("stream %r: job status: %s", name, ", ".join("%s=%s" % (path, to_text(_lookup(status, path)))
                                                                  for path in paths))
            if poll.get("fail_when") and _matches(status, poll["fail_when"]):
                raise SourceError(self.redact("the job failed: %s" % _summary(status, self.redact)))
            if _matches(status, poll["done_when"]):
                return status
            if self.clock() - started + every > timeout:
                raise SourceError(self.redact("the job did not finish within %s; last status: %s" % (
                    poll["timeout"], _summary(status, self.redact))))
            LOG.info("stream %r: the job is not done yet; checking again in %s", name, poll["every"],
                     extra=logs.fields(event="poll_wait", duration_ms=int(round(every * 1000)),
                                       **(where or {"stream": name})))
            self.sleep(every)

    @staticmethod
    def explode(records, field):
        for record in records:
            if not field:
                yield record
                continue
            children = _lookup(record, field)
            if not isinstance(children, list):
                continue
            parent = dict((key, value) for key, value in record.items() if key != field)
            for child in children:
                merged = dict(parent)
                merged.update(child if isinstance(child, dict) else {field: child})
                yield merged
