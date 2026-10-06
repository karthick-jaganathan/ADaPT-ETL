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
The `files` connector: local csv, tsv, json, jsonl and parquet files as records (object storage: the s3 and gcs
connectors).

- `auth: {provider: files, roots: ["{{ config.data_root }}"]}`: the local folders files can be read from (a list;
  references are rendered before connect; a URL is refused). connect() opens a Reader
  (adapt.connectors.files.reader): a DuckDB connection of its own that can only read inside the roots, with external
  access off and no extension loaded.
- A `requests` item `{name, sdk: files, service: file, method: read, arguments: {path, format, options, on_missing,
  match, recursive}}` reads the files `path` names - a file or a glob (`*`, `?`, `[ab]`, `**`), relative to the first
  root, or a list of them (e.g. a `batch_size` partition) - each row one record, a JSON object, in pages of at most
  1,000 records. With `match` (a literal Python regex), `path` is one folder (not a glob): the files in it whose path
  relative to it fully matches `match` are read, in the order of those paths; `recursive: true` (default false) looks
  in its sub-folders too. Every file is checked to be inside a root, once symbolic links are followed, before any
  file is read. `format`: csv, tsv, json, jsonl, parquet or auto (by the file's extension; the default). `options`:
  per format (see reader.OPTIONS). `on_missing`: skip (a warning; the default) or error, when nothing matches.
- Each file read is a call (rate limit, retries and the run's request counts) and logs a line on `adapt.network`:
  the file, its rows, bytes and time.

Only this read-only method can be called; files are never written.
"""

import os
import re
import time

from adapt.core.runtime import logs
from adapt.core.runtime.components import Connector, ConnectorError
from adapt.connectors.files.reader import (AccessError, FORMATS, OBJECT_STORAGE, PAGE_SIZE, ReadError, Reader,
                                           allowed_roots, format_of, is_glob, is_url, option_problems, path_problem,
                                           regex_problem)


__all__ = ["FilesConnector", "SERVICES", "ARGUMENTS", "ON_MISSING"]

DEFAULT_SERVICE = "file"
SERVICES = {"file": {"read": ("path", "format", "options", "on_missing", "match", "recursive")}}
REQUIRED = {("file", "read"): ("path",)}
ARGUMENTS = SERVICES["file"]["read"]
ON_MISSING = ("skip", "error")
_SECRETS = re.compile(r"\{\{\s*secrets\b")
_WHOLE_REFERENCE = re.compile(r"^\s*\{\{[^{}]*\}\}\s*$")
_REFERENCES = re.compile(r"\{\{.*?\}\}")


def _has_secret(value):
    if isinstance(value, str):
        return bool(_SECRETS.search(value))
    if isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    if isinstance(value, dict):
        return any(_has_secret(item) for item in value.values())
    return False


def _reference(value):
    return isinstance(value, str) and "{{" in value


def _paths(path):
    """A rendered `path`: a text, or a list of texts (e.g. a batch_size partition)."""
    return list(path) if isinstance(path, (list, tuple)) else [path]


class FilesConnector(Connector):

    name = "files"
    auth_required = ("roots",)
    auth_optional = ()
    network_loggers = ()  # DuckDB writes no logs
    category = "files"
    summary = "Local files: CSV, JSON, JSONL, Parquet, TSV"

    def __init__(self, page_size=PAGE_SIZE, clock=time.monotonic):
        self.page_size = page_size
        self.clock = clock

    # checks -----------------------------------------------------------------------

    def check_auth(self, auth):
        """
        Problems with the auth block - unrendered (adapt validate) or rendered (adapt run): `roots`, a list of local
        folders (not URLs), without secrets.
        """
        problems = super(FilesConnector, self).check_auth(auth)
        if "roots" not in auth:
            return problems
        roots = auth["roots"]
        what = "auth: provider 'files': `roots`"
        if _has_secret(roots):
            problems.append("%s cannot use secrets: roots are folders, not credentials" % what)
        elif isinstance(roots, str):
            if not _WHOLE_REFERENCE.match(roots):  # (a reference to a list config value is fine)
                problems.append("%s must be a list of folders, e.g. [\"{{ config.data_root }}\"]" % what)
        elif not isinstance(roots, list) or not roots:
            problems.append("%s must be a non-empty list of folders" % what)
        elif not all(isinstance(root, str) and root.strip() for root in roots):
            problems.append("%s must be a list of folders (non-empty texts)" % what)
        else:
            problems += ["%s: %r is a URL: roots are local folders; %s" % (what, root, OBJECT_STORAGE)
                         for root in roots if is_url(root)]
        return problems

    def check_request(self, request):
        return self._check_call(request, rendered=False)

    @staticmethod
    def _check_call(request, rendered):
        """The service, method and arguments of a call; `rendered`: its references were rendered already."""
        service = request.get("service") or DEFAULT_SERVICE
        methods = SERVICES.get(service)
        if methods is None:
            return ["files: service %r is not supported (supported: %s)" % (service, ", ".join(SERVICES))]
        method = request.get("method")
        if method not in methods:
            return ["files: %s.%s is not supported (supported: %s)" % (service, method, ", ".join(methods))]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["files: `arguments` must be a mapping"]
        expected = methods[method]
        problems = ["files: %s.%s needs `%s`" % (service, method, key) for key in REQUIRED[(service, method)]
                    if key not in arguments]
        problems += ["files: %s.%s does not take `%s` (arguments: %s)" % (service, method, key, ", ".join(expected))
                     for key in arguments if key not in expected]
        if _has_secret(arguments):
            problems.append("files: arguments cannot use secrets (paths and options are not credentials)")
        if "path" in arguments:
            problems += FilesConnector._path_problems(arguments["path"], rendered)
        if "match" in arguments:
            problems += FilesConnector._match_problems(arguments, rendered)
        if "recursive" in arguments:
            if not isinstance(arguments["recursive"], bool):
                problems.append("files: `recursive` must be true or false, got %r" % (arguments["recursive"],))
            if "match" not in arguments:
                problems.append("files: `recursive` goes with `match` (with a glob `path`, ** reads sub-folders)")
        file_format = arguments.get("format", "auto")
        if file_format != "auto" and file_format not in FORMATS:
            problems.append("files: unknown `format` %r (formats: auto, %s)" % (file_format, ", ".join(FORMATS)))
            file_format = "auto"
        problems += ["files: %s" % problem for problem in option_problems(file_format, arguments.get("options"))]
        on_missing = arguments.get("on_missing", "skip")
        if on_missing not in ON_MISSING:
            problems.append("files: unknown `on_missing` policy %r (policies: %s)" % (
                on_missing, ", ".join(ON_MISSING)))
        return problems

    @staticmethod
    def _path_problems(path, rendered):
        if not rendered and _reference(path) and _WHOLE_REFERENCE.match(path):
            return []  # e.g. "{{ partition.files }}": a text or a list, known at run time
        if isinstance(path, (list, tuple)):
            if not path:
                return ["files: `path` is an empty list"]
            items = path
        elif isinstance(path, str):
            items = [path]
        else:
            return ["files: `path` must be a file path, a glob or a list of them"]
        problems = []
        for item in items:
            if not isinstance(item, str):
                problems.append("files: `path` %r is not a text" % (item,))
                continue
            problem = None if rendered else path_problem(item)  # (at run time: resolve_files refuses them)
            if problem:
                problems.append("files: path %r %s" % (item, problem))
        return problems

    @staticmethod
    def _match_problems(arguments, rendered):
        """`match`: a valid literal regex; with it, `path` is one folder - a text, not a list or a glob."""
        match = arguments["match"]
        problem = regex_problem(match)
        problems = ["files: `match` %r %s" % (match, problem)] if problem else []
        path = arguments.get("path")
        if isinstance(path, (list, tuple)):
            problems.append("files: with `match`, `path` is one folder, not a list")
        elif isinstance(path, str) and is_glob(path if rendered else _REFERENCES.sub("", path)):
            problems.append("files: with `match`, `path` is a folder, not a glob: %r (the regex `match` picks the "
                            "files)" % path)
        return problems

    # running ----------------------------------------------------------------------

    def connect(self, auth, context):
        problems = self.check_auth(auth)
        if not problems and not isinstance(auth["roots"], list):  # a reference that did not give a list
            problems = ["auth: provider 'files': `roots` must be a list of folders, got %r" % (auth["roots"],)]
        if problems:
            raise ConnectorError("; ".join(problems))
        try:
            return Reader(allowed_roots(auth["roots"]), page_size=self.page_size)
        except (AccessError, ReadError) as exc:
            raise ConnectorError("files: %s" % exc)

    def request(self, client, request, context):
        problems = self._check_call(request, rendered=True)
        if problems:
            raise ConnectorError("; ".join(problems))
        arguments = request["arguments"]
        file_format = arguments.get("format") or "auto"
        options = arguments.get("options") or {}
        on_missing = arguments.get("on_missing") or "skip"
        match = arguments.get("match")
        files, seen = [], set()
        for path in _paths(arguments["path"]):  # every file is checked before any is read
            try:
                if match is None:
                    found = client.resolve(path)
                else:
                    found = client.resolve(path, match=match, recursive=arguments.get("recursive", False))
            except AccessError as exc:
                raise ConnectorError("files: %s" % exc, code="ACCESS_DENIED")
            except ReadError as exc:
                raise ConnectorError("files: %s" % exc, code="READ_ERROR")
            if not found:
                self._missing(path, match, on_missing, context)
            for item in found:
                if item[0] not in seen:
                    seen.add(item[0])
                    files.append(item)
        for real, root in files:
            kind = format_of(real) if file_format == "auto" else file_format
            if kind is None:
                raise ConnectorError("files: cannot tell the format of %s from its extension: set `format`" % real,
                                     code="READ_ERROR")
            for page in self._read(client, real, root, kind, options, context):
                yield page

    def _missing(self, path, match, on_missing, context):
        """Nothing matches a path: on_missing: error fails the request, skip logs a warning."""
        what = "matches %r" % path if match is None else "in %r matches %r" % (path, match)
        if on_missing == "error":
            raise ConnectorError("files: no file %s" % what, code="NOT_FOUND")
        where = context.where
        context.log.warning("%sfiles: no file %s; skipping it (on_missing: skip)", _prefix(where), what,
                            extra=logs.fields(event="file_missing", connector=self.name, path=path,
                                              **(where or {})))

    def _read(self, client, real, root, kind, options, context):
        """The pages of one file; the first is read through context.call(), then one adapt.network line."""
        name = os.path.relpath(real, root)
        where = context.where

        def start():
            pages = client.pages(real, kind, options, filename=name)
            return next(pages, None), pages
        rows, seconds = 0, 0.0
        started = self.clock()
        try:
            page, pages = context.call(start)
            seconds += self.clock() - started
            while page is not None:
                rows += len(page)
                yield page
                started = self.clock()
                page = next(pages, None)
                seconds += self.clock() - started
        except ReadError as exc:
            raise ConnectorError("files: %s" % exc, code="READ_ERROR")
        size = os.path.getsize(real)
        logs.NETWORK.info("%sfiles read %s: %s row(s), %s bytes, %.2f s", _prefix(where), real, "{:,}".format(rows),
                          "{:,}".format(size), seconds,
                          extra=logs.fields(event="file_read", connector=self.name, call="file.read", path=real,
                                            records=rows, bytes=size, duration_ms=int(round(seconds * 1000)),
                                            **(where or {})))

    def error(self, exc):
        if isinstance(exc, ReadError):
            return ConnectorError("files: %s" % exc, code="READ_ERROR")
        if isinstance(exc, AccessError):
            return ConnectorError("files: %s" % exc, code="ACCESS_DENIED")
        try:
            import duckdb
        except ImportError:  # pragma: no cover - duckdb is a dependency
            return None
        if isinstance(exc, duckdb.Error):
            return ConnectorError("files: %s" % exc, code="READ_ERROR")
        if isinstance(exc, OSError):
            return ConnectorError("files: %s" % exc, code="READ_ERROR")
        return None


def _prefix(where):
    text = logs.describe(where) if where else ""
    return text + ": " if text else ""
