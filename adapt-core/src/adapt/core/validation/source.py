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
adapt-validate checks for `kind: source` documents (see docs/design/source-format.md).

Used by adapt.core.validation.engine, which provides the YAML loading, reporting and entry points.
"""

import datetime
import os
import re

from adapt.core.config import loader
from adapt.core.validation import schema as spec
from adapt.core.validation.engine import ERROR, _Checker, _describe, _suggest


__all__ = ["SourceChecker"]

_TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.S)
_HEAD = re.compile(r"^([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))?$")
_FILTER = re.compile(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", re.S)
_NAME = re.compile(spec.NAME_PATTERN)
_DURATION = re.compile(spec.DURATION_PATTERN)
_DATE = re.compile(r"^(today|-?[0-9]+d|[0-9]{4}-[0-9]{2}-[0-9]{2})$")
_LITERAL = re.compile(r"""^('[^']*'|"[^"]*"|-?[0-9]+(\.[0-9]+)?|true|false|null)$""")


_split = spec.split_outside_quotes
_RECORDS_AS_RETURNED = "use `transform` steps; for records as returned: `SELECT record FROM <request>`"
# stream keys of earlier formats, each with what replaced it (the last versions are the git tags fields-dsl and
# transform-cross-stream)
_REMOVED_STREAM_KEYS = {
    "fields": "`fields` was replaced by SQL: `transform` steps (DuckDB SELECTs over the stream's requests) shape the "
              "records (see docs/design/source-format.md)",
    "on_record_error": "`on_record_error` was removed with `fields`: use TRY_CAST in a step's `select` for values "
                       "that may not convert",
    "request": "`request` was removed: use `requests` with one named item",
    "select": "a stream-level `select` was removed: " + _RECORDS_AS_RETURNED,
    "raw": "`raw` was removed: " + _RECORDS_AS_RETURNED,
    "primary_key": "a stream-level `primary_key` was removed: put `primary_key` on the export",
    "transform_mode": "`transform_mode` was removed: use `transform.mode`",
    "paginator": "a stream-level `paginator` was removed: put `paginator` on the request item",
    "records": "a stream-level `records` was removed: put `records` on the request item",
}
# removed keys that stand for a key the stream misses: that one is not reported as missing on top
_REPLACED_BY = {"requests": ("request",), "transform": ("select", "raw", "fields")}
_MISSING_STREAM_KEYS = {
    "requests": "a stream needs `requests`: streams read only their own requests",
    "transform": "a stream needs `transform`: {mode: page or run, steps: [{name, select}, ...]}",
    "export": "a stream needs `export`: at least one export (export name -> {step, primary_key?})",
}
_WINDOW_REFERENCE = re.compile(r"\{\{\s*window\.")


def _is_date(value):
    # PyYAML loads an unquoted 2025-01-01 as a datetime.date
    return isinstance(value, datetime.date) or (isinstance(value, str) and bool(_DATE.match(value)))


def _lower(value):
    return value.lower() if isinstance(value, str) else value


def _request_body(item):
    """A `requests` item without the keys that are not part of the call (as the runner reads it)."""
    return dict((key, value) for key, value in item.items()
                if key not in ("name", "records", "paginator", "partitions"))


def _uses_window(value):
    """True when a request references the window: it then runs once per window (as the runner decides)."""
    if isinstance(value, str):
        return _WINDOW_REFERENCE.search(value) is not None
    if isinstance(value, dict):
        return any(_uses_window(item) for item in value.values())
    if isinstance(value, list):
        return any(_uses_window(item) for item in value)
    return False


def _steps(stream):
    """The list of a stream's `transform.steps`, or None when there is none to read."""
    transform = stream.get("transform")
    steps = transform.get("steps") if isinstance(transform, dict) else None
    return steps if isinstance(steps, list) else None


def _expressions(value):
    """The inside of each {{ reference }} in a value (recursively)."""
    if isinstance(value, dict):
        return [inner for item in value.values() for inner in _expressions(item)]
    if isinstance(value, list):
        return [inner for item in value for inner in _expressions(item)]
    return _TEMPLATE.findall(value) if isinstance(value, str) else []


class _Stream(object):
    """What a stream makes available to references and cross-checks."""

    def __init__(self, name):
        self.name = name
        self.partitions = []
        self.incremental = False
        self.request_kind = None
        self.tables = {}          # lowercase request or step name -> (kind, index, name): the stream's tables
        self.steps_known = True   # False without steps to read (reported already): references to steps are not checked


class SourceChecker(_Checker):

    def __init__(self, filename, loader, options):
        super(SourceChecker, self).__init__(filename, loader, options)
        self.inputs = {"config": {}, "secrets": {}}
        self.used_inputs = set()
        self.auth_provider = None
        self.auth_type = None
        self.base_url = None
        self.streams = []        # the source's streams
        self.stream_names = {}   # lowercase stream name -> index of the first stream with it
        self.export_owners = {}  # lowercase export name -> (export name, index of its stream)

    # document ----------------------------------------------------------------

    def check_document(self, data):
        if not self.check_preamble(data):
            return
        if "version" in data and str(data["version"]) not in spec.SUPPORTED_VERSIONS:
            self.error("unsupported-version", ("version",), "`kind: source` has no version %r; omit `version` (it "
                       "defaults to the current format)" % (data["version"],), data, "version")
            return
        # `kind` may come from --kind for files that do not declare it
        required = [key for key in spec.REQUIRED_TOP_LEVEL_KEYS if key != "kind"]
        if self.folder is not None and "streams" not in data:
            required.remove("streams")
            self.error("missing-key", (), "a source folder needs a file for each stream in %s/" % os.path.join(
                self.folder, loader.STREAMS_FOLDER), data)
        self.require_keys(data, required, (), "a source")
        for key in data:
            if key not in spec.TOP_LEVEL_KEYS and not str(key).startswith(spec.EXTENSION_PREFIX):
                if key == "models":
                    self.error("removed-key", (key,), loader.MODELS_REMOVED_HINT, data, key)
                else:
                    self.error("unknown-key", (key,), "unknown top-level key %r%s; use an `%s` prefix for custom keys "
                               "(e.g. to hold YAML anchors)" % (key, _suggest(key, spec.TOP_LEVEL_KEYS),
                                                               spec.EXTENSION_PREFIX), data, key)
        if "name" in data:
            self.check_name(data["name"], ("name",), data, "name")
        if "description" in data:
            self._expect(isinstance(data["description"], str), "text", data["description"], ("description",),
                         data, "description")
        self.check_spec(data)
        self.check_auth(data)
        self.check_http_defaults(data)
        self.check_streams(data)
        streams = data.get("streams") if isinstance(data.get("streams"), list) else []
        queries = " ".join(step["select"] for stream in streams if isinstance(stream, dict)
                           for step in _steps(stream) or () if isinstance(step, dict)
                           and isinstance(step.get("select"), str))
        for section in spec.INPUT_SECTIONS:
            inputs = self.inputs[section]
            for name in inputs:
                if section == "config" and re.search(r"\b%s\b" % re.escape(name), queries):
                    continue  # read in a step's `select`: $name, or config->>'name'
                if (section, name) not in self.used_inputs:
                    self.warn("unused-input", ("spec", section, name), "%s input %r is declared but never used" % (
                        section, name), self.spec_section(data, section), name)
        if self.options.source_check is not None and not any(issue.severity == ERROR for issue in self.issues):
            self.options.source_check(self, data)  # e.g. the checks of installed connectors and query builders

    def spec_section(self, data, section):
        spec_block = data.get("spec")
        return spec_block.get(section) if isinstance(spec_block, dict) else None

    def check_name(self, value, path, container, key):
        if self._expect(isinstance(value, str), "a name", value, path, container, key) and not _NAME.match(value):
            self.error("bad-value", path, "%r is not a valid name (letters, digits and _; not starting with a "
                       "digit)" % value, container, key)

    def check_duration(self, value, path, container, key):
        if not (isinstance(value, str) and _DURATION.match(value)):
            self.error("bad-value", path, "expected a duration such as 15s, 30m, 1h or 7d, got %s" % _describe(value),
                       container, key)

    def check_choice(self, value, choices, path, container, key, what):
        if isinstance(value, str) and value in choices:
            return True
        self.error("bad-value", path, "unknown %s %r%s; one of: %s" % (
            what, value, _suggest(value, choices), ", ".join(str(c) for c in choices)), container, key)
        return False

    def check_strings(self, mapping, keys, path, expected="text"):
        for key in keys:
            if key in mapping:
                self._expect(isinstance(mapping[key], str), expected, mapping[key], path + (key,), mapping, key)

    def check_mapping(self, value, allowed, path, container, key, what, required=()):
        """Expects a mapping with only `allowed` keys; returns it (or None)."""
        if not self._expect(isinstance(value, dict), "a mapping", value, path, container, key):
            return None
        self.require_keys(value, required, path, what)
        self.unknown_keys(value, allowed, path, ERROR, "%s does not support it" % what)
        return value

    # references ----------------------------------------------------------------

    def scopes(self, stream=None, *extra):
        """Scopes available in stream requests and field values: config, today, partition, window, + extra."""
        available = {"config": set(self.inputs["config"]), "today": ()}
        if stream is not None and stream.partitions:
            available["partition"] = set(stream.partitions)
        if stream is not None and stream.incremental:
            available["window"] = set(spec.WINDOW_ATTRIBUTES)
        for scope in extra:
            available[scope] = None
        return available

    def check_templates(self, value, path, container, key, scopes, stream=None):
        """Checks every {{ reference }} in value (recursively) against the available scopes."""
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str) and ("{{" in k or "}}" in k):
                    self.error("template-syntax", path + (k,), "references are not allowed in mapping keys",
                               value, k)
                self.check_templates(v, path + (k,), value, k, scopes, stream)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.check_templates(v, path + (i,), value, i, scopes, stream)
        elif isinstance(value, str) and ("{{" in value or "}}" in value):
            if "{{" in _TEMPLATE.sub("", value):
                self.error("template-syntax", path, "unclosed `{{` in %r" % value, container, key)
            for inner in _TEMPLATE.findall(value):
                self.check_reference(inner, path, container, key, scopes, stream)

    def check_reference(self, inner, path, container, key, scopes, stream):
        parts = _split(inner, "|")
        match = _HEAD.match(parts[0])
        shown = "{{ %s }}" % inner.strip()
        if not match:
            self.error("template-syntax", path, "invalid reference %s" % shown, container, key)
            return
        scope, attribute = match.group(1), match.group(2)
        if scope not in spec.SCOPES:
            self.error("unknown-reference", path, "unknown scope %r in %s%s; scopes: %s" % (
                scope, shown, _suggest(scope, spec.SCOPES), ", ".join(spec.SCOPES)), container, key)
            return
        if scope not in scopes:
            if scope == "secrets":
                self.error("secret-outside-auth", path, "%s: secrets can only be used inside `auth`" % shown,
                           container, key)
            elif scope == "window" and stream is not None:
                self.error("unavailable-reference", path, "%s needs `incremental` on stream %r" % (
                    shown, stream.name), container, key)
            elif scope == "partition" and stream is not None:
                self.error("unavailable-reference", path, "%s: stream %r has no partitions" % (
                    shown, stream.name), container, key)
            else:
                self.error("unavailable-reference", path, "%s cannot be used here%s" % (
                    shown, "; available: " + ", ".join(sorted(scopes)) if scopes else ""), container, key)
            return
        allowed = scopes[scope]
        if scope == "today":
            if attribute:
                self.error("template-syntax", path, "%s: `today` has no attributes" % shown, container, key)
        elif not attribute:
            self.error("template-syntax", path, "%s needs a name, e.g. {{ %s.x }}" % (shown, scope), container, key)
        elif allowed is not None:
            name = attribute.split(".")[0]
            if "." in attribute:
                self.error("template-syntax", path, "%s: %s values have no attributes" % (shown, scope),
                           container, key)
            elif name not in allowed:
                where = {"config": "spec.config", "secrets": "spec.secrets"}.get(scope, scope)
                self.error("unknown-reference", path, "%s: %r is not declared in %s%s" % (
                    shown, name, where, _suggest(name, allowed)), container, key)
            if scope in spec.INPUT_SECTIONS:
                self.used_inputs.add((scope, name))
        for text in parts[1:]:
            self.check_filter(text, shown, path, container, key)

    def check_filter(self, text, shown, path, container, key):
        match = _FILTER.match(text)
        if not match:
            self.error("template-syntax", path, "invalid filter %r in %s" % (text, shown), container, key)
            return
        name, arguments = match.group(1), match.group(2)
        if name not in spec.FILTERS:
            self.error("unknown-filter", path, "unknown filter %r in %s%s; filters: %s" % (
                name, shown, _suggest(name, spec.FILTERS), ", ".join(spec.FILTERS)), container, key)
            return
        values = [value for value in _split(arguments, ",") if value] if arguments is not None else []
        if len(values) != spec.FILTERS[name]:
            self.error("bad-value", path, "filter %r takes %d argument(s), got %d in %s" % (
                name, spec.FILTERS[name], len(values), shown), container, key)
        for value in values:
            if not _LITERAL.match(value):
                self.error("template-syntax", path, "filter arguments must be quoted text or numbers, got %r in %s "
                           "(references are not allowed inside filters)" % (value, shown), container, key)

    # spec, auth, http ----------------------------------------------------------

    def check_spec(self, data):
        if "spec" not in data:
            return
        block = self.check_mapping(data["spec"], spec.INPUT_SECTIONS, ("spec",), data, "spec", "`spec`")
        if block is None:
            return
        for section in spec.INPUT_SECTIONS:
            if section not in block or block[section] is None:
                continue
            inputs = block[section]
            spath = ("spec", section)
            if not self._expect(isinstance(inputs, dict), "a mapping of input names", inputs, spath, block, section):
                continue
            for name, definition in inputs.items():
                self.check_name(name, spath + (name,), inputs, name)
                self.check_input(section, name, definition, spath + (name,), inputs)
                self.inputs[section][name] = definition

    def check_input(self, section, name, definition, path, container):
        what = "%s input %r" % (section, name)
        definition = self.check_mapping(definition, spec.INPUT_KEYS, path, container, name, what, ("type",))
        if definition is None:
            return
        self.check_templates(definition, path, container, name, {})
        input_type = definition.get("type")
        if "type" in definition and not self.check_choice(input_type, spec.INPUT_TYPES, path + ("type",),
                                                          definition, "type", "input type"):
            return
        if input_type == "list":
            if "items" not in definition:
                self.error("missing-key", path, "%s is a list and needs `items` (one of: %s)" % (
                    what, ", ".join(spec.LIST_ITEM_TYPES)), container, name)
            else:
                self.check_choice(definition["items"], spec.LIST_ITEM_TYPES, path + ("items",), definition,
                                  "items", "item type")
        elif "items" in definition:
            self.error("bad-value", path + ("items",), "`items` only applies to `type: list`", definition, "items")
        for flag in ("required",):
            if flag in definition:
                self._expect(isinstance(definition[flag], bool), "true or false", definition[flag],
                             path + (flag,), definition, flag)
        if "description" in definition:
            self._expect(isinstance(definition["description"], str), "text", definition["description"],
                         path + ("description",), definition, "description")
        if "default" in definition:
            if section == "secrets":
                self.warn("secret-default", path + ("default",), "secrets should not have defaults in config "
                          "files; pass them at run time", definition, "default")
            self.check_default(input_type, definition["default"], path + ("default",), definition)

    def check_default(self, input_type, value, path, container):
        checks = {
            "string": (lambda v: isinstance(v, str), "text"),
            "integer": (lambda v: isinstance(v, int) and not isinstance(v, bool), "a whole number"),
            "number": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "a number"),
            "boolean": (lambda v: isinstance(v, bool), "true or false"),
            "date": (_is_date, "a date: YYYY-MM-DD, today or -30d"),
            "list": (lambda v: isinstance(v, list), "a list"),
        }
        if input_type in checks:
            ok, expected = checks[input_type]
            self._expect(value is None or ok(value), expected, value, path, container, "default")

    def check_auth(self, data):
        if "auth" not in data:
            return
        auth = data["auth"]
        path = ("auth",)
        if not self._expect(isinstance(auth, dict), "a mapping", auth, path, data, "auth"):
            return
        self.auth_type = auth.get("type")
        if "provider" in auth:
            if "type" in auth:
                self.error("bad-value", path, "use either `provider` (a connector) or `type` (built-in), not both",
                           data, "auth")
            provider = auth["provider"]
            self.check_name(provider, path + ("provider",), auth, "provider")
            if isinstance(provider, str):
                self.auth_provider = provider
                self.check_connector(provider, path + ("provider",), auth, "provider")
        elif "type" not in auth:
            self.error("missing-key", path, "`auth` needs `type` (one of: %s) or a connector `provider`" %
                       ", ".join(spec.AUTH_TYPES), data, "auth")
        elif self.check_choice(auth["type"], spec.AUTH_TYPES, path + ("type",), auth, "type", "auth type"):
            keys = spec.AUTH_TYPES[auth["type"]]
            self.require_keys(auth, keys["required"], path, "`auth: %s`" % auth["type"])
            self.unknown_keys(auth, ("type",) + keys["required"] + keys["optional"], path, ERROR,
                              "`auth: %s` does not support it" % auth["type"])
            self.check_strings(auth, keys["required"], path, "text or a reference")
            if "in" in auth:
                self.check_choice(auth["in"], spec.API_KEY_LOCATIONS, path + ("in",), auth, "in", "location")
            if "scopes" in auth:
                self._expect(isinstance(auth["scopes"], list), "a list", auth["scopes"], path + ("scopes",),
                             auth, "scopes")
        self.check_templates(auth, path, data, "auth", dict(self.scopes(), secrets=set(self.inputs["secrets"])))
        for key, value in auth.items():
            lowered = str(key).lower()
            secret_like = key == "value" or (any(hint in lowered for hint in spec.SECRET_KEY_HINTS)
                                             and not lowered.endswith("url"))
            if secret_like and isinstance(value, str) and "{{" not in value:
                self.warn("literal-secret", path + (key,), "`%s` is written in the file; reference a secret "
                          "instead, e.g. \"{{ secrets.%s }}\"" % (key, key), auth, key)

    def check_connector(self, name, path, container, key):
        allowed = self.options.allowed_connectors
        if allowed is not None and name not in allowed:
            self.error("connector-not-allowed", path, "connector %r is not in the allowed list (%s)" % (
                name, ", ".join(allowed) or "empty"), container, key)

    def check_http_defaults(self, data):
        if "http" not in data:
            return
        http = self.check_mapping(data["http"], spec.HTTP_KEYS, ("http",), data, "http", "`http`")
        if http is None:
            return
        path = ("http",)
        if "base_url" in http:
            url = http["base_url"]
            if self._expect(isinstance(url, str), "a URL", url, path + ("base_url",), http, "base_url"):
                if url.startswith("http://"):
                    self.warn("insecure-url", path + ("base_url",), "use https:// for API requests", http,
                              "base_url")
                elif not url.startswith(("https://", "{{")):
                    self.error("bad-value", path + ("base_url",), "expected an http(s):// URL, got %r" % url,
                               http, "base_url")
                self.base_url = url
        if "headers" in http:
            self.check_headers(http["headers"], path + ("headers",), http)
        self.check_limits(http, path)
        self.check_templates(http, path, data, "http", self.scopes())

    def check_headers(self, headers, path, container):
        if self._expect(isinstance(headers, dict), "a mapping of header names to values", headers, path,
                        container, "headers"):
            for name, value in headers.items():
                self._expect(isinstance(value, str), "text (quote numbers, e.g. \"2.0.0\")", value, path + (name,),
                             headers, name)

    def check_limits(self, block, path):
        if "rate_limit" in block:
            limit = self.check_mapping(block["rate_limit"], spec.RATE_LIMIT_KEYS, path + ("rate_limit",), block,
                                       "rate_limit", "`rate_limit`", spec.RATE_LIMIT_KEYS)
            if limit is not None:
                if "requests" in limit:
                    self._expect(isinstance(limit["requests"], int) and not isinstance(limit["requests"], bool)
                                 and limit["requests"] > 0, "a positive whole number", limit["requests"],
                                 path + ("rate_limit", "requests"), limit, "requests")
                if "per" in limit:
                    self.check_duration(limit["per"], path + ("rate_limit", "per"), limit, "per")
        if "retry" in block:
            retry = self.check_mapping(block["retry"], spec.RETRY_KEYS, path + ("retry",), block, "retry", "`retry`")
            if retry is not None:
                rpath = path + ("retry",)
                if "codes" in retry and self._expect(isinstance(retry["codes"], list), "a list of status or "
                                                     "error codes", retry["codes"], rpath + ("codes",), retry, "codes"):
                    for i, code in enumerate(retry["codes"]):
                        self._expect(isinstance(code, (int, str)) and not isinstance(code, bool), "a status or "
                                     "error code", code, rpath + ("codes", i), retry["codes"], i)
                if "max_attempts" in retry:
                    attempts = retry["max_attempts"]
                    self._expect(isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 1,
                                 "a whole number >= 1", attempts, rpath + ("max_attempts",), retry, "max_attempts")
                if "backoff" in retry:
                    self.check_choice(retry["backoff"], spec.BACKOFF_TYPES, rpath + ("backoff",), retry, "backoff",
                                      "backoff")
                if "max_delay" in retry:
                    self.check_duration(retry["max_delay"], rpath + ("max_delay",), retry, "max_delay")

    # streams -------------------------------------------------------------------

    def check_streams(self, data):
        if "streams" not in data:
            return
        streams = data["streams"]
        if not self._expect(isinstance(streams, list) and len(streams) > 0, "a non-empty list of streams",
                            streams, ("streams",), data, "streams"):
            return
        names = self.stream_names
        self.streams = streams
        for i, stream in enumerate(streams):
            if isinstance(stream, dict) and isinstance(stream.get("name"), str):
                key = stream["name"].lower()
                if key in names:
                    previous = streams[names[key]].get("name")
                    suffix = "" if previous == stream["name"] else " (names ignore case)"
                    self.error("duplicate-stream", ("streams", i, "name"), "stream name %r is used twice%s" %
                               (stream["name"], suffix), stream, "name")
                names.setdefault(key, i)
        self.check_export_names()
        checked = []
        for i, stream in enumerate(streams):
            path = ("streams", i)
            if self._expect(isinstance(stream, dict), "a stream mapping", stream, path, streams, i):
                self.check_stream(stream, path)
                checked.append(i)
        self.check_partition_sources(checked)

    def check_export_names(self):
        """Export names are unique in the source; an export may have its own stream's name, not another stream's."""
        streams, names = self.streams, self.stream_names
        for i, stream in enumerate(streams):
            if not (isinstance(stream, dict) and isinstance(stream.get("export"), dict)):
                continue
            own = _lower(stream.get("name"))
            if isinstance(own, str) and names.get(own) != i:
                continue  # a second stream with this name: reported as such
            for export in stream["export"]:
                if not isinstance(export, str):
                    continue
                key, epath = export.lower(), ("streams", i, "export", export)
                if key in names and key != own:
                    self.error("duplicate-export", epath, "export %r has the name of stream %r: an export can have "
                               "its own stream's name, not another stream's" % (export, streams[names[key]]["name"]),
                               stream["export"], export)
                elif key in self.export_owners:
                    previous, index = self.export_owners[key]
                    suffix = "" if previous == export else " (names ignore case)"
                    if index == i:
                        message = "export name %r is used twice%s" % (export, suffix)
                    else:
                        message = "export %r is also an export of stream %r%s: export names are unique in the " \
                                  "source" % (export, streams[index].get("name"), suffix)
                    self.error("duplicate-export", epath, message, stream["export"], export)
                else:
                    self.export_owners[key] = (export, i)

    def check_stream(self, stream, path):
        self.unknown_keys(stream, spec.STREAM_KEYS + tuple(_REMOVED_STREAM_KEYS), path, ERROR,
                          "streams do not support it")
        for key in stream:
            if key in _REMOVED_STREAM_KEYS:
                self.error("removed-key", path + (key,), _REMOVED_STREAM_KEYS[key], stream, key)
        self.require_keys(stream, ("name",), path, "a stream")
        info = _Stream(stream.get("name") if isinstance(stream.get("name"), str) else "?")
        if path[1] in self.stream_files:  # a source folder's stream: the file name is the stream name
            if not _NAME.match(stream["name"]):
                self.error("bad-value", path, "%r is not a valid stream name (letters, digits and _; not starting "
                           "with a digit): rename the file" % stream["name"], stream)
        elif "name" in stream:
            self.check_name(stream["name"], path + ("name",), stream, "name")
        if "description" in stream:
            self._expect(isinstance(stream["description"], str), "text", stream["description"],
                         path + ("description",), stream, "description")
        if "on_partition_error" in stream:
            self.check_choice(stream["on_partition_error"], spec.ERROR_POLICIES, path + ("on_partition_error",),
                              stream, "on_partition_error", "policy")
        self.check_limits(stream, path)
        if "partitions" in stream:
            self.check_partitions(stream["partitions"], path + ("partitions",), stream, info)
        if "incremental" in stream:
            incremental = self.check_mapping(stream["incremental"], spec.INCREMENTAL_KEYS, path + ("incremental",),
                                             stream, "incremental", "`incremental`", ("cursor_field", "start"))
            if incremental is not None:
                info.incremental = True
                ipath = path + ("incremental",)
                if "cursor_field" in incremental:
                    self._expect(isinstance(incremental["cursor_field"], str), "a field name",
                                 incremental["cursor_field"], ipath + ("cursor_field",), incremental, "cursor_field")
                for duration in ("window", "lookback"):
                    if duration in incremental:
                        self.check_duration(incremental[duration], ipath + (duration,), incremental, duration)
                        value = incremental[duration]
                        minimum = 1 if duration == "window" else 0
                        if isinstance(value, str) and _DURATION.match(value) and \
                                (value[-1] != "d" or int(value[:-1]) < minimum):
                            self.error("bad-value", ipath + (duration,), "incremental `%s` is whole days%s, got %r" % (
                                duration, " (at least 1d)" if minimum else "", value), incremental, duration)
                if "start" in incremental:
                    self.check_templates(incremental["start"], ipath + ("start",), incremental, "start",
                                         self.scopes())
                    start = incremental["start"]
                    if not (isinstance(start, str) and "{{" in start) and not _is_date(start):
                        self.error("bad-value", ipath + ("start",), "expected a date (YYYY-MM-DD, today or -30d) "
                                   "or a reference, got %s" % _describe(start), incremental, "start")
        for key in spec.REQUIRED_STREAM_KEYS:
            if key in _MISSING_STREAM_KEYS and key not in stream and \
                    not any(old in stream for old in _REPLACED_BY.get(key, ())):
                self.error("missing-key", path, _MISSING_STREAM_KEYS[key], stream)
        self.collect_tables(stream, path, info)
        if "requests" in stream:
            self.check_requests(stream["requests"], path + ("requests",), stream, info)
        if "transform" in stream:
            self.check_transform(stream["transform"], path + ("transform",), stream)
        if "export" in stream:
            self.check_export(stream["export"], path + ("export",), stream, info)
        self.check_request_rules(stream, path, info)

    def check_request_rules(self, stream, path, info):
        """`mode: page` reads one request, without request partitions; `incremental` needs a request with a window."""
        requests = stream.get("requests") if isinstance(stream.get("requests"), list) else []
        transform = stream.get("transform")
        if isinstance(transform, dict) and transform.get("mode") == "page":
            if len(requests) > 1:
                self.error("bad-value", path + ("transform", "mode"), "`mode: page` runs the steps on each page of "
                           "one request, and this stream has %d: use `mode: run` to read several requests" %
                           len(requests), transform, "mode")
            for j, request in enumerate(requests):
                if isinstance(request, dict) and "partitions" in request:
                    self.error("bad-value", path + ("requests", j, "partitions"), "request-level partitions need "
                               "`transform.mode: run`", request, "partitions")
        bodies = [_request_body(item) for item in requests if isinstance(item, dict)]
        if info.incremental and bodies and not any(_uses_window(body) for body in bodies):
            self.error("bad-value", path + ("incremental",), "`incremental` needs a request that uses "
                       "`{{ window.start }}` or `{{ window.end }}`", stream, "incremental")

    def collect_tables(self, stream, path, info):
        """The names of the stream's requests and steps: its tables, one namespace (names ignore case)."""
        requests = stream.get("requests") if isinstance(stream.get("requests"), list) else []
        steps = _steps(stream)
        info.steps_known = bool(steps)
        entries = [("request", j, item, path + ("requests", j, "name")) for j, item in enumerate(requests)]
        entries += [("step", j, item, path + ("transform", "steps", j, "name")) for j, item in enumerate(steps or ())]
        for kind, index, item, npath in entries:
            if not (isinstance(item, dict) and isinstance(item.get("name"), str)):
                continue
            name = item["name"]
            previous = info.tables.get(name.lower())
            if previous is None:
                info.tables[name.lower()] = (kind, index, name)
                continue
            suffix = "" if previous[2] == name else " (names ignore case)"
            if previous[0] == kind:
                message = "%s name %r is used twice%s" % (kind, name, suffix)
            else:
                message = "step %r has the name of request %r%s: a stream's requests and steps are its tables, " \
                          "so their names are unique" % (name, previous[2], suffix)
            self.error("duplicate-%s" % kind, npath, message, item, "name")

    def check_transform(self, transform, path, stream):
        if isinstance(transform, list):
            self.error("bad-value", path, "`transform` is a mapping {mode: page or run, steps: [...]}: move these "
                       "steps to `transform.steps`", stream, "transform")
            return
        transform = self.check_mapping(transform, spec.TRANSFORM_KEYS, path, stream, "transform", "`transform`",
                                       spec.TRANSFORM_KEYS)
        if transform is None:
            return
        if "mode" in transform:
            self.check_choice(transform["mode"], spec.TRANSFORM_MODES, path + ("mode",), transform, "mode",
                              "transform mode")
        if "steps" not in transform:
            return
        steps = transform["steps"]
        if not self._expect(isinstance(steps, list) and len(steps) > 0, "a non-empty list of steps", steps,
                            path + ("steps",), transform, "steps"):
            return
        for j, step in enumerate(steps):
            spath = path + ("steps", j)
            if not self._expect(isinstance(step, dict), "a step mapping {name, select}", step, spath, steps, j):
                continue
            self.unknown_keys(step, spec.STEP_KEYS, spath, ERROR, "steps do not support it")
            self.require_keys(step, ("name", "select"), spath, "a step")
            if "name" in step:
                self.check_name(step["name"], spath + ("name",), step, "name")
            if "select" in step:
                query = step["select"]
                if not (isinstance(query, str) and query.strip()):
                    self.error("bad-value", spath + ("select",), "`select` must be a SQL query, got %s" %
                               _describe(query), step, "select")
                elif _TEMPLATE.search(query):  # as the template engine finds them: `}}` alone ends a struct literal
                    self.error("bad-value", spath + ("select",), "`select` is SQL: read config values as `$name` "
                               "parameters (or the columns of the request tables: %s), not with `{{ ... }}` "
                               "references" % ", ".join(spec.REQUEST_COLUMNS), step, "select")
            if "description" in step:
                self._expect(isinstance(step["description"], str), "text", step["description"],
                             spath + ("description",), step, "description")

    def check_export(self, export, path, stream, info):
        if export is None or export == {}:
            self.error("bad-value", path, "a stream needs at least one export", stream, "export")
            return
        if not self._expect(isinstance(export, dict), "a mapping of export names to {step, primary_key?}", export,
                            path, stream, "export"):
            return
        steps = sorted(name for kind, _, name in info.tables.values() if kind == "step")
        for name, definition in export.items():
            epath = path + (name,)
            self.check_name(name, epath, export, name)
            if not self._expect(isinstance(definition, dict), "an export mapping {step, primary_key?}", definition,
                                epath, export, name):
                continue
            self.unknown_keys(definition, spec.EXPORT_KEYS, epath, ERROR, "exports do not support it")
            self.require_keys(definition, ("step",), epath, "an export")
            step = definition.get("step")
            if "step" in definition and self._expect(isinstance(step, str), "a step name", step, epath + ("step",),
                                                     definition, "step") and info.steps_known:
                table = info.tables.get(step.lower())
                if table is None:
                    self.error("unknown-step", epath + ("step",), "unknown step %r%s" % (step, _suggest(step, steps)),
                               definition, "step")
                elif table[0] == "request":
                    self.error("bad-value", epath + ("step",), "%r is a request: an export writes a step's rows (for "
                               "the records as returned: a step `SELECT record FROM %s`)" % (step, table[2]),
                               definition, "step")
            if "primary_key" in definition:
                keys = definition["primary_key"]
                if self._expect(isinstance(keys, list) and len(keys) > 0, "a non-empty list of column names", keys,
                                epath + ("primary_key",), definition, "primary_key"):
                    for k, key in enumerate(keys):
                        self._expect(isinstance(key, str), "a column name", key, epath + ("primary_key", k), keys, k)
            if "description" in definition:
                self._expect(isinstance(definition["description"], str), "text", definition["description"],
                             epath + ("description",), definition, "description")

    def check_partitions(self, partitions, path, stream, info):
        if not self._expect(isinstance(partitions, list) and len(partitions) > 0, "a non-empty list of partitions",
                            partitions, path, stream, "partitions"):
            return
        for i, item in enumerate(partitions):
            ppath = path + (i,)
            if not self._expect(isinstance(item, dict), "a mapping", item, ppath, partitions, i):
                continue
            names = []  # (name, path, container, key) of the partitions this item defines
            batched = (spec.BATCH_SIZE,)  # reported on its own below, not as an unknown key
            if spec.BATCH_SIZE in item:
                self.error("bad-value", ppath + (spec.BATCH_SIZE,), "`batch_size` is not supported on %s: each "
                           "stream partition is one value, with its own state; batch a request partition of a run-mode "
                           "stream instead ({name, values, batch_size} or {name, from, field, batch_size})" % (
                               "a `from_stream` partition" if "from_stream" in item else "stream partitions"),
                           item, spec.BATCH_SIZE)
            if "from_stream" in item:
                several = "fields" in item
                if several:
                    self.check_mapping(item, spec.PARTITION_RECORD_KEYS + batched, ppath, partitions, i, "a partition "
                                       "with `fields` (named after the fields)", spec.PARTITION_RECORD_KEYS)
                    fields = item["fields"]
                    if self._expect(isinstance(fields, list) and len(fields) > 0, "a non-empty list of field names",
                                    fields, ppath + ("fields",), item, "fields"):
                        for j, field in enumerate(fields):
                            self.check_name(field, ppath + ("fields", j), fields, j)
                            names.append((field, ppath + ("fields", j), fields, j))
                else:
                    self.check_mapping(item, spec.PARTITION_STREAM_KEYS + batched, ppath, partitions, i,
                                       "a stream partition", spec.PARTITION_STREAM_KEYS)
                    self.check_strings(item, ("field",), ppath, "a name")
                self.check_strings(item, ("from_stream",), ppath, "a name")
                self.check_parent(item.get("from_stream"), ppath + ("from_stream",), item, stream)
            else:
                self.check_mapping(item, spec.PARTITION_VALUES_KEYS + batched, ppath, partitions, i, "a partition",
                                   spec.PARTITION_VALUES_KEYS)
                values = item.get("values")
                if "values" in item and not (isinstance(values, list) or (isinstance(values, str) and "{{" in values)):
                    self.error("bad-value", ppath + ("values",), "expected a list or a reference such as "
                               "\"{{ config.ids }}\", got %s" % _describe(values), item, "values")
                self.check_templates(values, ppath + ("values",), item, "values", self.scopes())
            if "name" in item and "fields" not in item:
                self.check_name(item["name"], ppath + ("name",), item, "name")
                names.append((item["name"], ppath + ("name",), item, "name"))
            for name, npath, container, key in names:
                if not isinstance(name, str):
                    continue
                if name in info.partitions:
                    self.error("duplicate-partition", npath, "partition %r is defined twice" % name, container, key)
                else:
                    info.partitions.append(name)

    def check_parent(self, parent, path, item, stream):
        """A `from_stream` parent: another stream of the source, with exactly one export (its rows are the values)."""
        if not isinstance(parent, str):
            return
        key = parent.lower()
        if key not in self.stream_names:
            owner = self.export_owners.get(key)
            if owner is not None:
                owner_name = self.streams[owner[1]].get("name")
                hint = "; %r is an export of stream %r: use `from_stream: %s`" % (parent, owner_name, owner_name)
            else:
                hint = _suggest(parent, [self.streams[i]["name"] for i in self.stream_names.values()])
            self.error("unknown-stream", path, "unknown stream %r%s" % (parent, hint), item, "from_stream")
        elif key == _lower(stream.get("name")):
            self.error("bad-value", path, "a stream cannot partition on itself", item, "from_stream")
        else:
            exports = self.streams[self.stream_names[key]].get("export")
            if isinstance(exports, dict) and len(exports) > 1:
                self.error("bad-value", path, "stream %r has %d exports (%s): `from_stream` reads a stream with "
                           "exactly one export" % (parent, len(exports), ", ".join(str(name) for name in exports)),
                           item, "from_stream")

    def check_partition_sources(self, checked):
        """`from_stream` parents run first, so they cannot form a cycle (names ignore case)."""
        streams, names = self.streams, self.stream_names
        parents = {}
        for i in checked:
            partitions = streams[i].get("partitions")
            if not (isinstance(partitions, list) and isinstance(streams[i].get("name"), str)):
                continue
            for item in partitions:
                parent = item.get("from_stream") if isinstance(item, dict) else None
                # the parent's export columns are checked when DuckDB compiles its steps; itself: reported already
                if isinstance(parent, str) and parent.lower() in names and parent.lower() != streams[i]["name"].lower():
                    parents.setdefault(streams[i]["name"].lower(), []).append(parent.lower())
        for start in sorted(parents):
            cycle = self._find_cycle(parents, start)
            if cycle:
                index = names[cycle[0]]
                self.error("partition-cycle", ("streams", index, "partitions"), "partitions form a cycle: %s" %
                           " -> ".join(streams[names[name]]["name"] for name in cycle), streams[index], "partitions")
                return

    @staticmethod
    def _find_cycle(parents, start):
        """Depth-first search for a path from `start` back to itself."""
        stack = [(start, [start])]
        while stack:
            node, trail = stack.pop()
            for parent in parents.get(node, ()):
                if parent == start:
                    return trail + [start]
                if parent not in trail:
                    stack.append((parent, trail + [parent]))
        return None

    # requests ------------------------------------------------------------------

    def check_requests(self, requests, path, stream, info):
        if not self._expect(isinstance(requests, list) and len(requests) > 0, "a non-empty list of requests",
                            requests, path, stream, "requests"):
            return
        for j, request in enumerate(requests):
            rpath = path + (j,)
            if not self._expect(isinstance(request, dict), "a request mapping", request, rpath, requests, j):
                continue
            self.require_keys(request, ("name",), rpath, "a request")
            if "name" in request:
                self.check_name(request["name"], rpath + ("name",), request, "name")
            request_info = _Stream(info.name)
            request_info.partitions = list(info.partitions)
            request_info.incremental = info.incremental
            if "partitions" in request:
                self.check_request_partitions(request["partitions"], rpath + ("partitions",), request, request_info,
                                              j, info, stream)
            kinds = [kind for kind in spec.REQUEST_KINDS if kind in request]
            if len(kinds) != 1:
                self.unknown_keys(request, spec.REQUEST_ITEM_KEYS, rpath, ERROR, "requests do not support it")
                self.error("bad-value", rpath, "a request needs exactly one of: http, sdk, async_job (found: %s)" %
                           (", ".join(kinds) or "none"), request)
            else:
                request_info.request_kind = kinds[0]
                allowed = {
                    "http": spec.REQUEST_ITEM_BASE_KEYS + ("http",),
                    "sdk": spec.REQUEST_ITEM_BASE_KEYS + spec.SDK_REQUEST_KEYS,
                    "async_job": spec.REQUEST_ITEM_BASE_KEYS + ("async_job",),
                }[request_info.request_kind]
                self.unknown_keys(request, allowed, rpath, ERROR, "requests do not support it")
                if request_info.request_kind == "async_job":
                    self.check_async_job(request["async_job"], rpath + ("async_job",), request, request_info)
                elif request_info.request_kind == "http":
                    self.check_http_request(request["http"], rpath + ("http",), request, self.scopes(request_info),
                                            request_info)
                else:
                    self.check_sdk_request(request, rpath, self.scopes(request_info), request_info,
                                           extra_allowed=spec.REQUEST_ITEM_BASE_KEYS)
            if "paginator" in request:
                self.check_paginator(request["paginator"], rpath + ("paginator",), request, request_info)
            if "records" in request:
                records = self.check_mapping(request["records"], spec.RECORDS_KEYS, rpath + ("records",), request,
                                             "records", "`records`")
                if records is not None:
                    for key in spec.RECORDS_KEYS:
                        if key in records:
                            self._expect(isinstance(records[key], str), "a dotted path", records[key],
                                         rpath + ("records", key), records, key)
                    self.check_templates(records, rpath + ("records",), request, "records", {})

    def check_request_partitions(self, partitions, path, request, info, index, stream_info, stream):
        """Request `index`'s own partitions (`info`: what the request sees; `stream_info`: its stream's tables)."""
        if not self._expect(isinstance(partitions, list) and len(partitions) > 0,
                            "a non-empty list of request partitions", partitions, path, request, "partitions"):
            return
        # a `from:` item can name a stream partition: it then uses only the rows with that partition's value
        stream_partitions = set(info.partitions)
        for k, item in enumerate(partitions):
            ppath = path + (k,)
            if not self._expect(isinstance(item, dict), "a mapping", item, ppath, partitions, k):
                continue
            names = []
            batched = (spec.BATCH_SIZE,)  # reported on its own below, not as an unknown key
            if "from" in item:
                if "fields" in item:
                    self.check_mapping(item, spec.REQUEST_PARTITION_RECORD_KEYS + batched, ppath, partitions, k,
                                       "a request partition with `fields` (named after the fields)",
                                       spec.REQUEST_PARTITION_RECORD_KEYS)
                    fields = item.get("fields")
                    if self._expect(isinstance(fields, list) and len(fields) > 0,
                                    "a non-empty list of field names", fields, ppath + ("fields",), item, "fields"):
                        for j, field in enumerate(fields):
                            self.check_name(field, ppath + ("fields", j), fields, j)
                            names.append((field, ppath + ("fields", j), fields, j))
                else:
                    self.check_mapping(item, spec.REQUEST_PARTITION_SOURCE_KEYS + batched, ppath, partitions, k,
                                       "a request partition", spec.REQUEST_PARTITION_SOURCE_KEYS)
                    self.check_strings(item, ("field",), ppath, "a name")
                self.check_strings(item, ("from",), ppath, "a name")
                self.check_partition_source(item.get("from"), ppath + ("from",), item, index, stream_info, stream)
            else:
                self.check_mapping(item, spec.REQUEST_PARTITION_VALUES_KEYS + batched, ppath, partitions, k,
                                   "a request partition", spec.REQUEST_PARTITION_VALUES_KEYS)
                values = item.get("values")
                if "values" in item and not (isinstance(values, list) or (isinstance(values, str) and "{{" in values)):
                    self.error("bad-value", ppath + ("values",), "expected a list or a reference such as "
                               "\"{{ config.ids }}\", got %s" % _describe(values), item, "values")
                self.check_partition_values(values, ppath + ("values",), item, info, stream_partitions)
            if spec.BATCH_SIZE in item:
                self.check_batch_size(item, ppath + (spec.BATCH_SIZE,), stream_partitions, stream_info)
            if "name" in item and "fields" not in item:
                self.check_name(item["name"], ppath + ("name",), item, "name")
                names.append((item["name"], ppath + ("name",), item, "name"))
            for name, npath, container, key in names:
                if not isinstance(name, str) or ("from" in item and name in stream_partitions):
                    continue
                if name in info.partitions:
                    self.error("duplicate-partition", npath, "partition %r is defined twice" % name, container, key)
                else:
                    info.partitions.append(name)

    def check_batch_size(self, item, path, stream_partitions, stream_info):
        """
        `batch_size: N` on a request partition: one request per list of up to N of its values, which a name holds.
        `{from, fields}` batches its one field that is not a stream partition, the others keeping only the stream
        partition's rows. A step has every stream partition's rows: in a partitioned stream, batching its values needs
        `{from, fields}` with all the stream partition fields (a request's records are those read under one).
        """
        size = item[spec.BATCH_SIZE]
        scoping = [name for name in stream_info.partitions if isinstance(name, str) and name in stream_partitions]
        fields = item.get("fields") if "from" in item else None
        if fields is None and "name" not in item or "fields" in item and not (isinstance(fields, list) and fields):
            return  # reported already
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            self.error("bad-value", path, "`batch_size` must be a whole number of at least 1 (the most values in one "
                       "request's list), got %s" % (repr(size) if isinstance(size, (int, float, str)) else
                                                    _describe(size)), item, spec.BATCH_SIZE)
            return
        if fields is not None:
            own = [field for field in fields if not (isinstance(field, str) and field in stream_partitions)]
            if not own:
                self.error("bad-value", path, "`batch_size`: %s %s stream partition%s, so this item only keeps the "
                           "rows with %s: it has no values of its own to batch; add the field to batch to `fields`" % (
                               ", ".join(repr(field) for field in fields), "is a" if len(fields) == 1 else "are",
                               "" if len(fields) == 1 else "s", "its value" if len(fields) == 1 else "their values"),
                           item, spec.BATCH_SIZE)
                return
            if len(own) > 1:
                self.error("bad-value", path, "`batch_size` on `{from, fields}` batches one field into one list, but "
                           "%s are not stream partitions: list only the stream partitions (%s) and the one field to "
                           "batch" % (", ".join(repr(field) for field in own),
                                      ", ".join(scoping) or "the stream has none"), item, spec.BATCH_SIZE)
                return
            batched, listed = own[0], fields
        elif "from" in item:
            if item.get("name") in stream_partitions:
                self.error("bad-value", path, "`batch_size`: %r is a stream partition, so this item only keeps the "
                           "rows with its value: it has no values of its own to batch" % item["name"], item,
                           spec.BATCH_SIZE)
                return
            batched, listed = item.get("field"), ()
        else:
            return
        # a request's records are those read under the stream partition; a step has every stream partition's rows
        table = stream_info.tables.get(item["from"].lower()) if isinstance(item.get("from"), str) else None
        if table is not None and table[0] == "step" and any(name not in listed for name in scoping):
            self.error("bad-value", path, "`batch_size`: step %r has the rows of every stream partition, so each list "
                       "would mix them: batching a step's values in a partitioned stream must scope each partition: "
                       "use `{from: %s, fields: [%s], batch_size: %s}` (the list is named after the field)" % (
                           item["from"], item["from"], ", ".join(scoping + [batched if isinstance(batched, str) else
                                                                            "<field>"]), size), item, spec.BATCH_SIZE)

    def check_partition_values(self, values, path, item, info, stream_partitions):
        """
        A request partition's `values`, which a run makes once per stream partition, before the request: their
        references can use config, today and the stream's partitions, not the window nor the request's own partitions
        (`info.partitions`: the stream's and the request's earlier ones).
        """
        unavailable = False
        for inner in _expressions(values):
            match = _HEAD.match(_split(inner, "|")[0])
            scope, name = (match.group(1), (match.group(2) or "").split(".")[0]) if match else (None, None)
            if scope == "window":
                why = "no window"
            elif scope == "partition" and name in info.partitions and name not in stream_partitions:
                why = "%r is a partition of this request" % name
            else:
                continue
            self.error("unavailable-reference", path, "{{ %s }} cannot be used in request partition `values`: a run "
                       "makes them once per stream partition, with config, today and the stream's partitions only "
                       "(%s)" % (inner.strip(), why), item, "values")
            unavailable = True
        if not unavailable:
            runtime = _Stream(info.name)
            runtime.partitions = [partition for partition in info.partitions if partition in stream_partitions]
            self.check_templates(values, path, item, "values", self.scopes(runtime), runtime)

    def check_partition_source(self, source, path, item, index, info, stream):
        """`from:` of request `index`: an earlier request or a step of the same stream."""
        if not isinstance(source, str):
            return
        key = source.lower()
        table = info.tables.get(key)
        if table is not None:
            kind, position, _ = table
            if kind == "request" and position == index:
                self.error("bad-value", path, "a request cannot partition on itself: use an earlier request or a "
                           "step", item, "from")
            elif kind == "request" and position > index:
                self.error("bad-value", path, "request partition source %r is a later request: use an earlier "
                           "request or a step" % source, item, "from")
            return
        exports = stream.get("export") if isinstance(stream.get("export"), dict) else {}
        own = [definition for name, definition in exports.items() if _lower(name) == key]
        other = self.export_owners.get(key)
        if own:
            step = own[0].get("step") if isinstance(own[0], dict) else None
            self.error("bad-value", path, "%r is this stream's export, which is written after its requests and steps "
                       "run%s" % (source, "; read its step %r" % step if isinstance(step, str) else ""), item, "from")
        elif (key in self.stream_names and key != _lower(stream.get("name"))) or other is not None:
            parent = self.streams[self.stream_names[key] if key in self.stream_names else other[1]].get("name")
            self.error("bad-value", path, "%r is not a request or step of this stream: requests read only their own "
                       "stream's requests and steps; to partition by stream %r, use a stream partition "
                       "`{from_stream: %s, ...}`" % (source, parent, parent), item, "from")
        elif info.steps_known:
            choices = [name for kind, position, name in info.tables.values() if kind == "step" or position < index]
            self.error("unknown-source", path, "unknown request partition source %r%s: use an earlier request or a "
                       "step of this stream" % (source, _suggest(source, choices)), item, "from")

    def check_job_request(self, request, path, container, key, info, scopes):
        """The `submit` or `results` request of an async job: an sdk request (an `http:` one is reported already)."""
        if not self._expect(isinstance(request, dict), "a mapping", request, path, container, key):
            return
        if "sdk" in request:
            self.check_sdk_request(request, path, scopes, info)
        else:
            self.error("missing-key", path, "`%s` needs `sdk`: async jobs run through a connector" % key, container,
                       key)

    def check_http_request(self, http, path, container, scopes, info):
        http = self.check_mapping(http, spec.HTTP_REQUEST_KEYS, path, container, "http", "an http request",
                                  ("path",))
        if http is None:
            return
        if self.auth_provider is not None and self.auth_type is None:  # both: already reported in `auth`
            self.error("bad-value", path, "http requests need a built-in `auth.type`; auth provider %r "
                       "authenticates sdk requests only" % self.auth_provider, container, "http")
        target = http.get("path")
        if "path" in http and self._expect(isinstance(target, str), "a path", target, path + ("path",), http, "path"):
            absolute = target.startswith(("http://", "https://"))
            if not absolute and self.base_url is None:
                self.error("missing-key", path + ("path",), "relative path %r needs `http.base_url` at the top "
                           "level" % target, http, "path")
        if "method" in http:
            self.check_choice(http["method"], spec.HTTP_METHODS, path + ("method",), http, "method", "HTTP method")
        if "headers" in http:
            self.check_headers(http["headers"], path + ("headers",), http)
        for name in ("params",):
            if name in http:
                self._expect(isinstance(http[name], dict), "a mapping", http[name], path + (name,), http, name)
        self.check_templates(http, path, container, "http", scopes, info)

    def check_sdk_request(self, request, path, scopes, info, extra_allowed=()):
        self.unknown_keys(request, spec.SDK_REQUEST_KEYS + tuple(extra_allowed), path, ERROR,
                          "sdk requests do not support it")
        self.require_keys(request, ("method",), path, "an sdk request")
        sdk = request.get("sdk")
        if self._expect(isinstance(sdk, str), "a connector name", sdk, path + ("sdk",), request, "sdk"):
            self.check_connector(sdk, path + ("sdk",), request, "sdk")
            if self.auth_provider is None:
                self.error("bad-value", path + ("sdk",), "sdk requests use the client built by a connector "
                           "`auth.provider`; set `auth: {provider: %s, ...}`" % sdk, request, "sdk")
            elif sdk != self.auth_provider:
                self.error("bad-value", path + ("sdk",), "sdk %r does not match auth.provider %r" % (
                    sdk, self.auth_provider), request, "sdk")
        for name in ("service", "method"):
            if name in request:
                self._expect(isinstance(request[name], str), "a name", request[name], path + (name,), request, name)
        if "arguments" in request:
            self._expect(isinstance(request["arguments"], dict), "a mapping", request["arguments"],
                         path + ("arguments",), request, "arguments")
        if "headers" in request:
            headers = request["headers"]
            if self._expect(isinstance(headers, dict), "a mapping of header names to values", headers,
                            path + ("headers",), request, "headers"):
                for header, value in headers.items():
                    self._expect(isinstance(value, (str, int)) and not isinstance(value, bool), "text or a number",
                                 value, path + ("headers", header), headers, header)
        body = dict((key, request[key]) for key in spec.SDK_REQUEST_KEYS if key in request)
        if id(request) in self.nodes:
            self.nodes[id(body)] = self.nodes[id(request)]
        self.check_templates(body, path, None, None, scopes, info)

    def check_async_job(self, job, path, container, info):
        job = self.check_mapping(job, spec.ASYNC_JOB_KEYS, path, container, "async_job", "`async_job`",
                                 ("submit", "poll"))
        if job is None:
            return
        outputs = [name for name in ("download", "results") if name in job]
        if len(outputs) != 1:
            self.error("bad-value", path, "`async_job` needs exactly one of `download` or `results`", container,
                       "async_job")
        base = self.scopes(info)
        for part in ("submit", "results"):
            if isinstance(job.get(part), dict) and "http" in job[part]:
                self.error("bad-value", path + (part,), "`%s` must be an sdk request: async jobs run through a "
                           "connector (its `poll` calls the same service)" % part, job, part)
        if "submit" in job and not (isinstance(job["submit"], dict) and "http" in job["submit"]):
            self.check_job_request(job["submit"], path + ("submit",), job, "submit", info, base)
        if "poll" in job:
            poll = self.check_mapping(job["poll"], spec.POLL_KEYS, path + ("poll",), job, "poll", "`poll`",
                                      ("method", "every", "timeout", "done_when"))
            if poll is not None:
                ppath = path + ("poll",)
                self.check_strings(poll, ("method",), ppath, "a method name")
                for duration in ("every", "timeout"):
                    if duration in poll:
                        self.check_duration(poll[duration], ppath + (duration,), poll, duration)
                for name in ("done_when", "fail_when"):
                    if name in poll:
                        condition = self.check_mapping(poll[name], spec.POLL_CONDITION_KEYS, ppath + (name,), poll,
                                                       name, "`%s`" % name, ("path",))
                        if condition is not None:
                            self.check_strings(condition, ("path",), ppath + (name,), "a dotted path")
                        if condition is not None and len([k for k in ("equals", "in") if k in condition]) != 1:
                            self.error("bad-value", ppath + (name,), "needs exactly one of `equals` or `in`", poll,
                                       name)
                self.check_templates(poll, ppath, job, "poll", self.scopes(info, "submit"), info)
        if "download" in job:
            download = self.check_mapping(job["download"], spec.DOWNLOAD_KEYS, path + ("download",), job,
                                          "download", "`download`", ("url", "format"))
            if download is not None:
                dpath = path + ("download",)
                self.check_strings(download, ("url",), dpath, "a URL or a reference")
                if "format" in download:
                    self.check_choice(download["format"], spec.DOWNLOAD_FORMATS, dpath + ("format",), download,
                                      "format", "format")
                if "compression" in download:
                    self.check_choice(download["compression"], spec.COMPRESSIONS, dpath + ("compression",),
                                      download, "compression", "compression")
                self.check_templates(download, dpath, job, "download", self.scopes(info, "submit", "poll"), info)
        if "results" in job and not (isinstance(job["results"], dict) and "http" in job["results"]):
            self.check_job_request(job["results"], path + ("results",), job, "results", info,
                                   self.scopes(info, "submit", "poll"))

    def check_paginator(self, paginator, path, stream, info):
        if not self._expect(isinstance(paginator, dict), "a mapping", paginator, path, stream, "paginator"):
            return
        if "type" not in paginator:
            self.error("missing-key", path, "`paginator` needs `type` (one of: %s)" % ", ".join(spec.PAGINATOR_TYPES),
                       stream, "paginator")
            return
        kind = paginator["type"]
        if not self.check_choice(kind, spec.PAGINATOR_TYPES, path + ("type",), paginator, "type", "paginator"):
            return
        keys = spec.PAGINATOR_TYPES[kind]
        self.require_keys(paginator, keys["required"], path, "`paginator: %s`" % kind)
        self.unknown_keys(paginator, ("type",) + keys["required"] + keys["optional"], path, ERROR,
                          "`paginator: %s` does not support it" % kind)
        self.check_strings(paginator, ("offset_param", "limit_param", "page_param", "size_param", "token_path",
                                       "param", "next_url_path"), path, "a name")
        if kind == "cursor":
            token = "token_path" in paginator or "param" in paginator
            if token == ("next_url_path" in paginator) or (token and not ("token_path" in paginator and
                                                                         "param" in paginator)):
                self.error("bad-value", path, "`paginator: cursor` needs `token_path` and `param`, or "
                           "`next_url_path`", stream, "paginator")
        for name in ("page_size", "start"):
            if name in paginator:
                number = paginator[name]
                minimum = 1 if name == "page_size" else 0
                self._expect(isinstance(number, int) and not isinstance(number, bool) and number >= minimum,
                             "a whole number >= %d" % minimum, number, path + (name,), paginator, name)
        if kind != "none" and info.request_kind in ("sdk", "async_job"):
            self.error("bad-value", path + ("type",), "%s requests are paginated by the connector; remove `paginator` "
                       "or use `type: none`" % info.request_kind, paginator, "type")
        self.check_templates(paginator, path, stream, "paginator", self.scopes(info, "response"), info)
