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
The `azure_blob` connector: csv, tsv, json, jsonl and parquet objects in Azure Blob Storage as records.

- `auth: {provider: azure_blob, roots: ["azure://container/prefix/"], connection_string, ...}`: `roots` (required) are
  the URL prefixes objects can be read from (references allowed, no secrets);
  credentials (`connection_string`, `account_key`, or `client_secret`) must be `{{ secrets.* }}` references,
  masked in every message.
  connect() opens a Reader (streamwright.connectors.azure_blob.reader): a DuckDB connection of its own with azure,
  the credentials and settings as a DuckDB secret scoped to the roots, that can only read inside the roots.
- A `requests` item `{name, sdk: azure_blob, service: object, method: read, arguments: {path, format, options,
  on_missing, match, recursive}}` reads the objects `path` names - an azure:// URL or a key relative to the first root;
  a glob (`*`, `[ab]`, `**`; never `?`); a list of them; or, with `match` (a literal Python regex), the folder whose
  objects' keys relative to it fully match it (sub-folders too with `recursive: true`).
"""

import re
import time

from streamwright.core.runtime import logs
from streamwright.core.runtime.components import Connector, ConnectorError, ConnectorSpec
from streamwright.connectors.azure_blob.reader import (CREDENTIALS, FORMATS, PAGE_SIZE, PROVIDER, SCHEME, SCHEMES,
                                                      SETTINGS, AccessError, MissingError, ReadError, Reader,
                                                      allowed_roots, format_of, is_glob, is_url, masked_forms,
                                                      option_problems, path_problem, regex_problem, setting_problem,
                                                      url_problem)


__all__ = ["AzureBlobConnector", "SERVICES", "DEFAULT_SERVICE", "ARGUMENTS", "ON_MISSING"]

DEFAULT_SERVICE = "object"
SERVICES = {"object": {"read": ("path", "format", "options", "on_missing", "match", "recursive")}}
REQUIRED = {("object", "read"): ("path",)}
ARGUMENTS = SERVICES["object"]["read"]
ON_MISSING = ("skip", "error")
_SECRETS = re.compile(r"\{\{\s*secrets\b")
_SECRET_REFERENCE = re.compile(r"^\s*\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}\s*$")
_WHOLE_REFERENCE = re.compile(r"^\s*\{\{[^{}]*\}\}\s*$")
_WHAT = "auth: provider %r:" % PROVIDER


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


def _example(key):
    return "\"{{ secrets.%s_%s }}\"" % (PROVIDER, key)


def _credential_problem(key, value):
    """Problems with a credential: a non-empty text, and one `{{ secrets.* }}` reference if it is a reference."""
    if not isinstance(value, str) or not value.strip():
        return ["%s `%s` must be a secret reference, e.g. %s" % (_WHAT, key, _example(key))]
    if _reference(value) and not _SECRET_REFERENCE.match(value):
        return ["%s `%s` must be one secret reference, e.g. %s: credentials are never written in a source or its "
                "config" % (_WHAT, key, _example(key))]
    return []


def _roots_problems(roots):
    what = "%s `roots`" % _WHAT
    if _has_secret(roots):
        return ["%s cannot use secrets: roots are URL prefixes, not credentials" % what]
    if isinstance(roots, str):
        if _WHOLE_REFERENCE.match(roots):  # (a reference to a list config value is fine)
            return []
        return ["%s must be a list of %s:// URL prefixes, e.g. [\"%s://container/prefix/\"]" % (what, SCHEME, SCHEME)]
    if not isinstance(roots, list) or not roots:
        return ["%s must be a non-empty list of %s:// URL prefixes" % (what, SCHEME)]
    problems = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            problems.append("%s must be a list of %s:// URL prefixes (non-empty texts)" % (what, SCHEME))
        elif is_url(root):
            problem = url_problem(root, root=True)
            if problem:
                problems.append("%s: %r %s" % (what, root, problem))
        elif not _WHOLE_REFERENCE.match(root):
            problems.append("%s: %r is not an %s:// URL prefix" % (what, root, SCHEME))
    return problems


class AzureBlobConnector(Connector):

    spec = ConnectorSpec(
        name=PROVIDER,
        title="Azure Blob Storage",
        category="object storage",
        transport="duckdb",
        extension="azure",
    )
    auth_required = ("roots",)
    auth_optional = tuple(CREDENTIALS) + tuple(SETTINGS)

    def __init__(self, page_size=PAGE_SIZE, clock=time.monotonic):
        self.page_size = page_size
        self.clock = clock

    # checks -----------------------------------------------------------------------

    def check_auth(self, auth):
        """
        Problems with the auth block - unrendered (streamwright validate) or rendered (streamwright run): `roots` azure:// URL
        prefixes without secrets; each credential one `{{ secrets.* }}` reference; settings literal (checked) or references.
        """
        problems = super(AzureBlobConnector, self).check_auth(auth)
        if "roots" in auth:
            problems += _roots_problems(auth["roots"])
        for key in CREDENTIALS:
            if key in auth:
                problems += _credential_problem(key, auth[key])
        for key in SETTINGS:
            if key in auth and not _reference(auth[key]):
                problem = setting_problem(key, auth[key])
                if problem:
                    problems.append("%s `%s`: %s" % (_WHAT, key, problem))

        # Check authentication method completeness
        has_conn_str = "connection_string" in auth
        has_acc_key = "account_key" in auth and "account_name" in auth
        has_sp = "client_secret" in auth and "account_name" in auth
        if not (has_conn_str or has_acc_key or has_sp):
            # Check if any partial creds were provided
            if "account_key" in auth and "account_name" not in auth:
                problems.append("%s `account_key` requires `account_name`" % _WHAT)
            elif "client_secret" in auth and "account_name" not in auth:
                problems.append("%s `client_secret` requires `account_name`" % _WHAT)
            elif not any(k in auth for k in CREDENTIALS):
                problems.append("%s needs credentials: `connection_string`, or `account_name` and `account_key`, "
                                "or `account_name`, `tenant_id`, `client_id`, and `client_secret`" % _WHAT)

        return problems

    def check_request(self, request):
        return self._check_call(request, rendered=False)

    @staticmethod
    def _check_call(request, rendered):
        """The service, method and arguments of a call; `rendered`: its references were rendered already."""
        name = PROVIDER
        service = request.get("service") or DEFAULT_SERVICE
        methods = SERVICES.get(service)
        if methods is None:
            return ["%s: service %r is not supported (supported: %s)" % (name, service, ", ".join(SERVICES))]
        method = request.get("method")
        if method not in methods:
            return ["%s: %s.%s is not supported (supported: %s)" % (name, service, method, ", ".join(methods))]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["%s: `arguments` must be a mapping" % name]
        expected = methods[method]
        problems = ["%s: %s.%s needs `%s`" % (name, service, method, key) for key in REQUIRED[(service, method)]
                    if key not in arguments]
        problems += ["%s: %s.%s does not take `%s` (arguments: %s)" % (name, service, method, key, ", ".join(
            expected)) for key in arguments if key not in expected]
        if _has_secret(arguments):
            problems.append("%s: arguments cannot use secrets (paths and options are not credentials: credentials "
                            "go in the auth block)" % name)
        match = arguments.get("match")
        if "path" in arguments:
            problems += AzureBlobConnector._path_problems(arguments["path"], rendered, match is not None)
        if match is not None:
            problem = regex_problem(match)
            if problem:
                problems.append("%s: `match` %r %s" % (name, match, problem))
        if "recursive" in arguments:
            if not isinstance(arguments["recursive"], bool):
                problems.append("%s: `recursive` must be true or false (a literal)" % name)
            elif match is None:
                problems.append("%s: `recursive` goes with `match` (the folder to look in is `path`)" % name)
        file_format = arguments.get("format", "auto")
        if file_format != "auto" and file_format not in FORMATS:
            problems.append("%s: unknown `format` %r (formats: auto, %s)" % (name, file_format, ", ".join(FORMATS)))
            file_format = "auto"
        problems += ["%s: %s" % (name, problem) for problem in option_problems(file_format, arguments.get("options"))]
        on_missing = arguments.get("on_missing", "skip")
        if on_missing not in ON_MISSING:
            problems.append("%s: unknown `on_missing` policy %r (policies: %s)" % (
                name, on_missing, ", ".join(ON_MISSING)))
        return problems

    @staticmethod
    def _path_problems(path, rendered, folder):
        """Problems with `path` (`folder`: with `match`, it names folders, not globs)."""
        name = PROVIDER
        if not rendered and _reference(path) and _WHOLE_REFERENCE.match(path):
            return []  # e.g. "{{ partition.keys }}": a text or a list, known at run time
        if isinstance(path, (list, tuple)):
            if not path:
                return ["%s: `path` is an empty list" % name]
            items = path
        elif isinstance(path, str):
            items = [path]
        else:
            return ["%s: `path` must be an %s:// URL (or a key relative to the first root), a glob or a list of "
                    "them" % (name, SCHEME)]
        problems = []
        for item in items:
            if not isinstance(item, str):
                problems.append("%s: `path` %r is not a text" % (name, item))
                continue
            problem = None if rendered else path_problem(item)
            if problem:
                problems.append("%s: path %r %s" % (name, item, problem))
            elif folder and is_glob(item) and not rendered:
                problems.append("%s: path %r is a glob: with `match`, `path` is the folder to look in" % (name, item))
        return problems

    # running ----------------------------------------------------------------------

    def connect(self, auth, context):
        problems = self.check_auth(auth)
        if not problems and not isinstance(auth["roots"], list):  # a reference that did not give a list
            problems = ["%s `roots` must be a list of %s:// URL prefixes, got %r" % (_WHAT, SCHEME, auth["roots"])]
        credentials = dict((key, auth[key]) for key in CREDENTIALS if auth.get(key) is not None)
        # a secret of the run is masked whole already; a credential written in the source is not
        written = [key for key, value in credentials.items() if context.redact(str(value)).strip() != "***"]
        for value in masked_forms(credentials.values()):
            context.secret(value)
        if problems:
            raise ConnectorError(context.redact("; ".join(problems)))
        if written:
            raise ConnectorError("%s: %s must come from secrets: write e.g. %s in the source's auth and pass the value "
                                 "as a secret (credentials are never written in a source)" % (
                                     PROVIDER, ", ".join("`%s`" % key for key in written), _example(written[0])))
        settings = dict((key, auth[key]) for key in SETTINGS if auth.get(key) is not None)
        try:
            return Reader(allowed_roots(auth["roots"]), credentials=credentials, settings=settings,
                          page_size=self.page_size)
        except (AccessError, ReadError) as exc:
            raise ConnectorError(context.redact("%s: %s" % (PROVIDER, exc)), code="CONNECT_ERROR")

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
        for path in _paths(arguments["path"]):
            try:
                if match is not None:
                    found = client.match(path, match, bool(arguments.get("recursive")))
                else:
                    found = client.resolve(path)
            except AccessError as exc:
                raise ConnectorError("%s: %s" % (PROVIDER, exc), code="ACCESS_DENIED")
            except ReadError as exc:
                raise ConnectorError("%s: %s" % (PROVIDER, exc), code="READ_ERROR")
            if not found:
                self._missing(path, on_missing, context)
            for item in found:
                if item[0] not in seen:
                    seen.add(item[0])
                    files.append(item)
        for url, root in files:
            kind = format_of(url) if file_format == "auto" else file_format
            if kind is None:
                raise ConnectorError("%s: cannot tell the format of %s from its extension: set `format`" % (
                    PROVIDER, url), code="READ_ERROR")
            for page in self._read(client, url, root, kind, options, on_missing, context):
                yield page

    def _missing(self, path, on_missing, context):
        """Nothing matches a path: on_missing: error fails the request, skip logs a warning."""
        if on_missing == "error":
            raise ConnectorError("%s: no object matches %r" % (PROVIDER, path), code="NOT_FOUND")
        where = context.where
        context.log.warning("%s%s: no object matches %r; skipping it (on_missing: skip)", _prefix(where), PROVIDER,
                            path, extra=logs.fields(event="file_missing", connector=self.name, path=path,
                                                    **(where or {})))

    def _read(self, client, url, root, kind, options, on_missing, context):
        """
        The pages of one object; the first is read through context.call(), then one streamwright.network line. A URL that
        names no object (known only now) is missing, as on_missing says.
        """
        name = url[len(root):]
        where = context.where

        def start():
            pages = client.pages(url, kind, options, filename=name)
            try:
                return next(pages, None), pages
            except MissingError:
                return None, None
        rows, seconds = 0, 0.0
        started = self.clock()
        try:
            page, pages = context.call(start)
            if pages is None:
                self._missing(url, on_missing, context)
                return
            seconds += self.clock() - started
            while page is not None:
                rows += len(page)
                yield page
                started = self.clock()
                page = next(pages, None)
                seconds += self.clock() - started
        except AccessError as exc:
            raise ConnectorError("%s: %s" % (PROVIDER, exc), code="ACCESS_DENIED")
        except ReadError as exc:
            raise ConnectorError("%s: %s" % (PROVIDER, exc), code="READ_ERROR")
        logs.NETWORK.info("%s%s read %s: %s row(s), %.2f s", _prefix(where), PROVIDER, url, "{:,}".format(rows),
                          seconds, extra=logs.fields(event="file_read", connector=self.name, call="object.read",
                                                     path=url, records=rows, duration_ms=int(round(seconds * 1000)),
                                                     **(where or {})))

    def error(self, exc):
        if isinstance(exc, MissingError):
            return ConnectorError("%s: %s" % (PROVIDER, exc), code="NOT_FOUND")
        if isinstance(exc, ReadError):
            return ConnectorError("%s: %s" % (PROVIDER, exc), code="READ_ERROR")
        if isinstance(exc, AccessError):
            return ConnectorError("%s: %s" % (PROVIDER, exc), code="ACCESS_DENIED")
        try:
            import duckdb
        except ImportError:  # pragma: no cover
            return None
        if isinstance(exc, duckdb.Error):
            return ConnectorError("%s: %s" % (PROVIDER, exc), code="READ_ERROR")
        return None


def _prefix(where):
    text = logs.describe(where) if where else ""
    return text + ": " if text else ""
