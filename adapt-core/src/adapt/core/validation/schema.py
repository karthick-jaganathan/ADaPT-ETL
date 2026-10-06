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
Specification of the ADaPT source configuration format (`kind: source`).

Single source of truth for adapt-validate's source checks (source_validator) and the published
JSON Schemas (`adapt-validate --export-schema DIR` writes DIR/source.schema.json and DIR/stream.schema.json for the
stream files of source folders).
Design: docs/design/source-format.md.
"""

__all__ = ["VERSION", "SUPPORTED_VERSIONS", "KIND", "json_schema", "stream_json_schema", "split_outside_quotes"]

# `version` is optional; omitting it means VERSION. A new version is only introduced for a breaking change.
VERSION = "1"
SUPPORTED_VERSIONS = ("1", "1.0")
KIND = "source"

TOP_LEVEL_KEYS = ("version", "kind", "name", "description", "spec", "auth", "http", "streams")
REQUIRED_TOP_LEVEL_KEYS = ("kind", "name", "streams")
EXTENSION_PREFIX = "x-"  # free-form top-level keys, e.g. to hold YAML anchors
_MERGE_KEY = "<<"  # YAML merge key, as an editor in YAML 1.2 mode sees `<<: *anchor`

NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
DURATION_PATTERN = r"^[0-9]+[smhd]$"

# * ------
# * inputs
# * ------
INPUT_SECTIONS = ("config", "secrets")
INPUT_TYPES = ("string", "integer", "number", "boolean", "date", "list")
LIST_ITEM_TYPES = ("string", "integer", "number", "boolean", "date")
INPUT_KEYS = ("type", "items", "required", "default", "description")

# * ----
# * auth
# * ----
AUTH_TYPES = {
    "oauth2_refresh_token": {"required": ("token_url", "client_id", "client_secret", "refresh_token"),
                             "optional": ("scopes",)},
    "api_key": {"required": ("name", "value"), "optional": ("in",)},
    "bearer": {"required": ("token",), "optional": ()},
    "basic": {"required": ("username", "password"), "optional": ()},
}
API_KEY_LOCATIONS = ("header", "query")
SECRET_KEY_HINTS = ("secret", "token", "password")  # auth keys whose literal values should be secrets

# * ----------------------
# * HTTP, retries and limits
# * ----------------------
HTTP_KEYS = ("base_url", "headers", "rate_limit", "retry")
RATE_LIMIT_KEYS = ("requests", "per")
# not `on`: YAML 1.1 reads the key `on` as the boolean true
RETRY_KEYS = ("codes", "max_attempts", "backoff", "max_delay")
BACKOFF_TYPES = ("exponential", "constant")

# * -------
# * streams
# * -------
# a stream reads its own `requests` (each one a table named after it), shapes them with the SQL `steps` of its
# `transform` and writes the steps its `export` names
STREAM_KEYS = ("name", "description", "partitions", "incremental", "requests", "transform", "export",
               "on_partition_error", "retry", "rate_limit")
REQUIRED_STREAM_KEYS = ("name", "requests", "transform", "export")
REQUEST_COLUMNS = ("record", "partition", "config", "window_start", "window_end", "today")  # of each request's table
ERROR_POLICIES = ("fail", "skip")
TRANSFORM_KEYS = ("mode", "steps")
TRANSFORM_MODES = ("page", "run")
STEP_KEYS = ("name", "select", "description")
EXPORT_KEYS = ("step", "primary_key", "description")
PARTITION_VALUES_KEYS = ("name", "values")
PARTITION_STREAM_KEYS = ("name", "from_stream", "field")  # one field of each parent record
PARTITION_RECORD_KEYS = ("from_stream", "fields")         # several fields of each parent record, named after them
REQUEST_PARTITION_VALUES_KEYS = PARTITION_VALUES_KEYS
REQUEST_PARTITION_SOURCE_KEYS = ("name", "from", "field")
REQUEST_PARTITION_RECORD_KEYS = ("from", "fields")
BATCH_SIZE = "batch_size"  # on request partitions: lists of up to N values of one field, one request each
INCREMENTAL_KEYS = ("cursor_field", "start", "window", "lookback")

REQUEST_KINDS = ("http", "sdk", "async_job")
HTTP_REQUEST_KEYS = ("path", "method", "params", "headers", "json")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SDK_REQUEST_KEYS = ("sdk", "service", "method", "arguments", "headers")  # headers: the connector's request_headers
REQUEST_ITEM_BASE_KEYS = ("name", "paginator", "records", "partitions")
REQUEST_ITEM_KEYS = REQUEST_ITEM_BASE_KEYS + ("http", "async_job") + SDK_REQUEST_KEYS
ASYNC_JOB_KEYS = ("submit", "poll", "download", "results")
POLL_KEYS = ("method", "arguments", "every", "timeout", "done_when", "fail_when")
POLL_CONDITION_KEYS = ("path", "equals", "in")
DOWNLOAD_KEYS = ("url", "format", "compression")
DOWNLOAD_FORMATS = ("csv", "jsonl", "json")
COMPRESSIONS = ("zip", "gzip", "none")

PAGINATOR_TYPES = {
    "none": {"required": (), "optional": ()},
    "offset": {"required": ("offset_param", "limit_param", "page_size"), "optional": ()},
    # cursor: `token_path` + `param`, or `next_url_path` alone
    "cursor": {"required": (), "optional": ("token_path", "param", "next_url_path")},
    "page_number": {"required": ("page_param",), "optional": ("page_size", "size_param", "start")},
}
RECORDS_KEYS = ("path", "explode")

# * ---------------------
# * references: {{ ... }}
# * ---------------------
SCOPES = ("config", "secrets", "partition", "window", "response", "submit", "poll", "today")
WINDOW_ATTRIBUTES = ("start", "end")
FILTERS = {"default": 1, "join": 1, "date": 1, "int": 0, "lower": 0, "upper": 0}  # name -> number of arguments


def split_outside_quotes(text, separator):
    """Splits on `separator` outside single or double quotes; parts are stripped. Shared by checks and runtime."""
    parts, current, quote = [], "", None
    for char in text:
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == separator:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    parts.append(current.strip())
    return parts


# * --------------------------------
# * JSON SCHEMA (editor autocomplete)
# * --------------------------------
_STR = {"type": "string"}
_DURATION = {"type": "string", "pattern": DURATION_PATTERN, "description": "e.g. 15s, 30m, 1h, 7d"}
_DAYS = {"type": "string", "pattern": r"^[0-9]+d$", "description": "whole days, e.g. 0d, 3d"}
_WINDOW = {"type": "string", "pattern": r"^0*[1-9][0-9]*d$", "description": "whole days, at least 1d, e.g. 1d, 7d"}
_NAME = {"type": "string", "pattern": NAME_PATTERN}
_FROM = dict(_NAME, description="An earlier request of this stream (fields are dotted paths in its records) or a step "
                                "of this stream (fields are its columns).")
_BATCH = {"type": "integer", "minimum": 1, "description": "One request per list of up to N distinct values: the "
                                                          "partition's name holds the list (e.g. for GAQL `IN`)."}


def _ref(name):
    return {"$ref": "#/definitions/%s" % name}


def _obj(properties, required=(), description=None):
    properties = dict(properties, **{_MERGE_KEY: {}})
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def _enum(values, description=None):
    schema = {"enum": list(values)}
    if description:
        schema["description"] = description
    return schema


def _dispatch(options, key="type"):
    """{key: name} selects options[name]."""
    return {
        "type": "object",
        "required": [key],
        "properties": {key: _enum(options)},
        "allOf": [{"if": {"properties": {key: {"const": name}}, "required": [key]}, "then": schema}
                  for name, schema in options.items()],
    }


def _definitions():
    definitions = {
        "input": _obj({
            "type": _enum(INPUT_TYPES),
            "items": _enum(LIST_ITEM_TYPES, "Item type of a `list` input."),
            "required": {"type": "boolean", "description": "Default true."},
            "default": {"description": "Dates accept offsets from today, e.g. -30d."},
            "description": _STR,
        }, required=("type",)),
        "inputs": {"type": ["object", "null"], "propertyNames": _NAME, "additionalProperties": _ref("input")},
        "rate_limit": _obj({"requests": {"type": "integer", "minimum": 1}, "per": _DURATION},
                           required=("requests", "per")),
        "retry": _obj({
            "codes": {"type": "array", "items": {"type": ["integer", "string"]},
                   "description": "HTTP status codes or provider error codes."},
            "max_attempts": {"type": "integer", "minimum": 1},
            "backoff": _enum(BACKOFF_TYPES),
            "max_delay": _DURATION,
        }),
        "http_request": _obj({
            "path": _STR, "method": _enum(HTTP_METHODS), "params": {"type": "object"},
            "headers": {"type": "object", "additionalProperties": _STR}, "json": {},
        }, required=("path",)),
        "sdk_request": _obj({"sdk": _STR, "service": _STR, "method": _STR, "arguments": {"type": "object"},
                             "headers": {"type": "object", "additionalProperties": {"type": ["string", "integer"]},
                                         "description": "Per-request headers the connector supports, e.g. "
                                                        "CustomerAccountId (microsoft_ads)."}},
                            required=("sdk", "method")),
        "poll_condition": _obj({"path": _STR, "equals": {}, "in": {"type": "array"}}, required=("path",)),
        "async_job": {
            "type": "object",
            "properties": {
                "submit": _ref("sdk_request"),
                "poll": _obj({
                    "method": _STR, "arguments": {"type": "object"}, "every": _DURATION, "timeout": _DURATION,
                    "done_when": _ref("poll_condition"), "fail_when": _ref("poll_condition"),
                }, required=("method", "every", "timeout", "done_when")),
                "download": _obj({"url": _STR, "format": _enum(DOWNLOAD_FORMATS), "compression": _enum(COMPRESSIONS)},
                                 required=("url", "format")),
                "results": _ref("sdk_request"),
            },
            "required": ["submit", "poll"],
            "oneOf": [{"required": ["download"]}, {"required": ["results"]}],
            "additionalProperties": False,
        },
        "request_item": {"type": "object", "properties": {
            "name": _NAME,
            "http": _ref("http_request"),
            "sdk": _STR,
            "service": _STR,
            "method": _STR,
            "arguments": {"type": "object"},
            "headers": {"type": "object", "additionalProperties": {"type": ["string", "integer"]}},
            "async_job": _ref("async_job"),
            "paginator": _ref("paginator"),
            "records": _ref("records"),
            "partitions": {"type": "array", "minItems": 1, "items": _ref("request_partition")},
            _MERGE_KEY: {},
        }, "required": ["name"], "additionalProperties": False, "oneOf": [
            {"required": ["http"], "not": {"anyOf": [
                {"required": ["sdk"]}, {"required": ["service"]}, {"required": ["method"]},
                {"required": ["arguments"]}, {"required": ["headers"]}, {"required": ["async_job"]},
            ]}},
            {"required": ["sdk", "method"], "not": {"anyOf": [{"required": ["http"]}, {"required": ["async_job"]}]}},
            {"required": ["async_job"], "not": {"anyOf": [
                {"required": ["http"]}, {"required": ["sdk"]}, {"required": ["service"]},
                {"required": ["method"]}, {"required": ["arguments"]}, {"required": ["headers"]},
            ]}},
        ]},
        "paginator": _dispatch({
            "none": _obj({"type": {}}),
            "offset": _obj({"type": {}, "offset_param": _STR, "limit_param": _STR,
                            "page_size": {"type": "integer", "minimum": 1}},
                           required=PAGINATOR_TYPES["offset"]["required"]),
            "cursor": {"anyOf": [
                _obj({"type": {}, "token_path": _STR, "param": _STR}, required=("token_path", "param")),
                _obj({"type": {}, "next_url_path": _STR}, required=("next_url_path",)),
            ]},
            "page_number": _obj({"type": {}, "page_param": _STR, "page_size": {"type": "integer", "minimum": 1},
                                 "size_param": _STR, "start": {"type": "integer"}}, required=("page_param",)),
        }),
        "partition": {"oneOf": [
            _obj({"name": _NAME, "values": {"type": ["array", "string"]}}, required=PARTITION_VALUES_KEYS),
            _obj({"name": _NAME, "from_stream": _STR, "field": _STR}, required=PARTITION_STREAM_KEYS),
            _obj({"from_stream": _STR, "fields": {"type": "array", "items": _NAME, "minItems": 1, "uniqueItems": True}},
                 required=PARTITION_RECORD_KEYS, description="One partition per distinct combination of these "
                                                             "fields in the parent's records, named after them."),
        ]},
        "request_partition": {"oneOf": [
            _obj({"name": _NAME, "values": {"type": ["array", "string"]}, BATCH_SIZE: _BATCH},
                 required=REQUEST_PARTITION_VALUES_KEYS),
            _obj({"name": _NAME, "from": _FROM, "field": _STR, BATCH_SIZE: _BATCH},
                 required=REQUEST_PARTITION_SOURCE_KEYS),
            _obj({"from": _FROM, "fields": {"type": "array", "items": _NAME, "minItems": 1, "uniqueItems": True},
                  BATCH_SIZE: dict(_BATCH, description="One request per list of up to N distinct values of the one "
                                                       "field that is not a stream partition, which names the list "
                                                       "(the stream partition fields keep the partition's rows).")},
                 required=REQUEST_PARTITION_RECORD_KEYS, description="One request partition per distinct "
                                                                     "combination of these fields."),
        ]},
        "incremental": _obj({"cursor_field": _STR, "start": {}, "window": _WINDOW, "lookback": _DAYS},
                            required=("cursor_field", "start")),
        "records": _obj({"path": _STR, "explode": _STR}),
        "transform": _obj({
            "mode": _enum(TRANSFORM_MODES, "page: the steps run on each page of the stream's one request; run: the "
                                           "requests and steps run in dependency order over all of the run's rows, "
                                           "then the exports are written."),
            "steps": {"type": "array", "minItems": 1, "items": _ref("step")},
        }, required=TRANSFORM_KEYS, description="Named SQL steps that shape the stream's requests."),
        "step": _obj({
            "name": _NAME,
            "select": {"type": "string", "minLength": 1,
                       "description": "One DuckDB SELECT over the stream's request tables (columns record, "
                                      "partition, config: JSON; window_start, window_end, today: DATE) and earlier "
                                      "steps; `$name` reads spec.config.<name>."},
            "description": _STR,
        }, required=("name", "select")),
        "export": _obj({
            "step": _NAME,
            "primary_key": {"type": "array", "items": _STR, "minItems": 1},
            "description": _STR,
        }, required=("step",)),
        "stream": _obj({
            "name": _NAME,
            "description": _STR,
            "partitions": {"type": "array", "items": _ref("partition")},
            "incremental": _ref("incremental"),
            "requests": {"type": "array", "minItems": 1, "items": _ref("request_item"),
                         "description": "Named requests: the raw rows of each one are a table of the stream's SQL, "
                                        "named after it (columns %s)." % ", ".join(REQUEST_COLUMNS)},
            "transform": _ref("transform"),
            "export": {"type": "object", "minProperties": 1, "propertyNames": _NAME,
                       "additionalProperties": _ref("export"),
                       "description": "The stream's outputs: each export is an output table with the rows of one "
                                      "step."},
            "on_partition_error": _enum(ERROR_POLICIES),
            "retry": _ref("retry"),
            "rate_limit": _ref("rate_limit"),
        }, required=REQUIRED_STREAM_KEYS),
    }
    builtin_auth = dict((name, _obj(dict([("type", {})] + [(k, {}) for k in keys["required"] + keys["optional"]]),
                                    required=keys["required"]))
                        for name, keys in AUTH_TYPES.items())
    definitions["auth"] = {
        "type": "object",
        "if": {"required": ["provider"]},
        "then": {"properties": {"provider": _STR}, "not": {"required": ["type"]},
                 "description": "Connector provider; its keys are defined by the connector."},
        "else": _dispatch(builtin_auth),
    }
    return definitions


def json_schema():
    """JSON Schema (draft-07) for `kind: source` documents, for editor autocomplete."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ADaPT source configuration",
        "description": "A source with streams. Generated by `adapt-validate --export-schema`; run `adapt validate` "
                       "for the full set of checks (references, connectors and each step's `select`, compiled by "
                       "DuckDB).",
        "type": "object",
        "properties": {
            "version": {"enum": [1, "1", "1.0"], "description": "Optional; omitting it means the current format."},
            "kind": {"const": KIND},
            "name": _NAME,
            "description": _STR,
            "spec": _obj({"config": _ref("inputs"), "secrets": _ref("inputs")}),
            "auth": _ref("auth"),
            "http": _obj({"base_url": _STR, "headers": {"type": "object", "additionalProperties": _STR},
                          "rate_limit": _ref("rate_limit"), "retry": _ref("retry")}),
            "streams": {"type": "array", "minItems": 1, "items": _ref("stream")},
        },
        # not `streams`: the source.yaml of a source folder has none (adapt-validate checks that a source has streams)
        "required": [key for key in REQUIRED_TOP_LEVEL_KEYS if key != "streams"],
        "patternProperties": {"^x-": {}},
        "additionalProperties": False,
        "definitions": _definitions(),
    }


def stream_json_schema():
    """JSON Schema (draft-07) for the stream files of source folders (streams/<name>.yaml), for editor autocomplete."""
    definitions = _definitions()
    stream = definitions["stream"]
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ADaPT source stream",
        "description": "One stream of a source folder: streams/<name>.yaml, where the file name is the stream name. "
                       "Generated by `adapt-validate --export-schema`; run adapt-validate on the folder for the full "
                       "set of checks.",
        "type": "object",
        "properties": dict((key, value) for key, value in stream["properties"].items() if key != "name"),
        "required": [key for key in stream["required"] if key != "name"],
        "additionalProperties": False,
        "definitions": definitions,
    }
