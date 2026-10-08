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
Helpers for testing sources and connectors without network access, used by this repository's tests and available to
third-party connectors:

- FakeApi: a local HTTP server with canned routes (a context manager).
- MemoryOutput: an output that keeps a run's SCHEMA, RECORD and STATE messages in memory (and has summary()).
- FakeClock: a clock and sleep function for SourceRunner(clock=..., sleep=...) that never waits.
- page_stream: a stream with one request, one page-mode step and one export, all named simply.

    with FakeApi() as api:
        api.routes[("GET", "/items")] = lambda request: (200, {"data": [{"id": 1}]})
        source = {"kind": "source", "name": "demo", "http": {"base_url": api.url}, "streams": [page_stream(
            "items", {"http": {"path": "/items"}}, "SELECT (record->>'id')::BIGINT AS id FROM records",
            records={"path": "data"}, primary_key=["id"])]}
        output = MemoryOutput()
        SourceRunner(source, config, secrets, output=output).run()
"""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse


__all__ = ["FakeApi", "MemoryOutput", "FakeClock", "page_stream"]


def page_stream(name, request, select, records=None, primary_key=None, **extra):
    """
    A stream `name` with one request (`request`: its http, sdk or async_job body, with its `paginator` if any) named
    `records` unless it has a `name`, the request's `records` (path, explode), one page-mode step `select` and one
    export of it, both named `name`, keyed on `primary_key`. `extra`: the stream's other keys (partitions,
    incremental, on_partition_error, retry, rate_limit, ...).
    """
    item = dict({"name": "records"}, **request)
    if records is not None:
        item["records"] = records
    export = {"step": name}
    if primary_key:
        export["primary_key"] = list(primary_key)
    stream = {"name": name, "requests": [item], "transform": {"mode": "page", "steps": [{"name": name,
                                                                                         "select": select}]},
              "export": {name: export}}
    stream.update(extra)
    return stream


class FakeApi(object):
    """
    A local HTTP server at `url`. routes[(method, path)] = handler(request) -> (status, payload[, headers]), where
    request has method, path, params, headers and body; bytes payloads are sent as files, anything else as JSON.
    Unknown routes answer 404. Every request is kept in `requests`.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                api.handle(self, "GET")

            def do_POST(self):
                api.handle(self, "POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def handle(self, handler, method):
        parsed = urlparse(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length).decode("utf-8") if length else ""
        request = {"method": method, "path": parsed.path, "params": dict(parse_qsl(parsed.query)),
                   "headers": dict(handler.headers), "body": body}
        self.requests.append(request)
        route = self.routes.get((method, parsed.path))
        status, payload, headers = (route(request) + ({},))[:3] if route else (404, {"error": "not found"}, {})
        if isinstance(payload, bytes):
            data, content_type = payload, "application/octet-stream"
        else:
            data, content_type = json.dumps(payload).encode("utf-8"), "application/json"
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def calls(self, path):
        """The requests made to `path`."""
        return [request for request in self.requests if request["path"] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class MemoryOutput(object):
    """
    schemas[export] = (schema, key properties); columns[export] = its (name, DuckDB type) columns; records =
    [(export, record)]; states = [state, ...]; partial = the exports marked partial. summary() is what every output
    has: one {"export", "records"} entry per export, in the order of their schemas.
    """

    def __init__(self):
        self.schemas, self.records, self.states = {}, [], []
        self.columns, self.partial = {}, []
        self.closed = None

    def write_columns(self, name, columns):
        self.columns[name] = columns

    def write_schema(self, name, schema, key_properties):
        self.schemas[name] = (schema, key_properties)

    def write_record(self, name, record):
        self.records.append((name, record))

    def write_state(self, state):
        self.states.append(copy.deepcopy(state))

    def mark_partial(self, name):
        self.partial.append(name)

    def close(self, failed=False):
        self.closed = "failed" if failed else "ok"

    def summary(self):
        counts = dict((name, 0) for name in self.schemas)
        for name, _ in self.records:
            counts[name] = counts.get(name, 0) + 1
        return [{"export": name, "records": count} for name, count in counts.items()]


class FakeClock(object):
    """Time that only moves when sleep() is called: SourceRunner(clock=clock, sleep=clock.sleep)."""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
