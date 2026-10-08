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

"""Config and secret inputs: reading, type conversion, dates and durations."""

import datetime
import os
import re
import stat

import yaml

from streamwright.core.validation import schema


__all__ = ["InputError", "SECRET_ENV_PREFIX", "parse_duration", "as_date", "resolve_inputs", "read_values_file",
           "read_config_file", "secrets_from_env"]

SECRET_ENV_PREFIX = "STREAMWRIGHT_SECRET_"
CONFIG_FILE_SECTIONS = ("config", "streams")

_DURATION = re.compile(r"^([0-9]+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_OFFSET = re.compile(r"^(-?[0-9]+)d$")
_TRUE = ("true", "1", "yes", "y", "on")
_FALSE = ("false", "0", "no", "n", "off")


class InputError(Exception):
    pass


def parse_duration(text):
    match = _DURATION.match(str(text))
    if not match:
        raise InputError("invalid duration %r (expected e.g. 15s, 30m, 1h, 7d)" % (text,))
    return datetime.timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])


def as_date(value, today=None):
    """date, datetime, 'today', an offset such as '-30d', or an ISO date / datetime string -> datetime.date."""
    today = today or datetime.date.today()
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "today":
            return today
        offset = _OFFSET.match(text)
        if offset:
            return today + datetime.timedelta(days=int(offset.group(1)))
        try:
            return datetime.date.fromisoformat(text[:10])
        except ValueError:
            pass
    raise InputError("invalid date %r (expected YYYY-MM-DD, today or an offset such as -30d)" % (value,))


def _coerce_scalar(kind, value, today):
    if kind == "string":
        if isinstance(value, (dict, list)):
            raise InputError("expected text, got %s" % type(value).__name__)
        return value if isinstance(value, str) else str(value)
    if kind == "integer":
        if isinstance(value, bool):
            raise InputError("expected a whole number, got %r" % value)
        try:
            return int(str(value).strip())
        except ValueError:
            raise InputError("expected a whole number, got %r" % (value,))
    if kind == "number":
        if isinstance(value, bool):
            raise InputError("expected a number, got %r" % value)
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except ValueError:
            raise InputError("expected a number, got %r" % (value,))
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in _TRUE:
            return True
        if str(value).strip().lower() in _FALSE:
            return False
        raise InputError("expected true or false, got %r" % (value,))
    if kind == "date":
        return as_date(value, today)
    raise InputError("unknown input type %r" % (kind,))


def _coerce(definition, value, today):
    kind = definition.get("type")
    if kind != "list":
        return _coerce_scalar(kind, value, today)
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise InputError("expected a list (or comma-separated text), got %s" % type(value).__name__)
    return [_coerce_scalar(definition.get("items", "string"), item, today) for item in value]


def resolve_inputs(definitions, provided, section, today=None, hide_values=False):
    """
    Applies defaults, required checks and types; raises one InputError listing every problem.
    With hide_values (secrets), the messages never include the values.
    """
    definitions = definitions or {}
    values, problems = {}, []
    for name, definition in definitions.items():
        definition = definition or {}
        if provided.get(name) is not None:
            raw = provided[name]
        elif definition.get("default") is not None:
            raw = definition["default"]
        else:
            if definition.get("required", True):
                problems.append("missing required %s %r" % (section, name))
            values[name] = None
            continue
        try:
            values[name] = _coerce(definition, raw, today)
        except InputError as exc:
            if hide_values:
                problems.append("%s %r is not a valid %s" % (section, name, definition.get("type", "string")))
            else:
                problems.append("%s %r: %s" % (section, name, exc))
    unknown = sorted(set(provided) - set(definitions))
    if unknown:
        problems.append("unknown %s: %s (declared: %s)" % (section, ", ".join(unknown),
                                                         ", ".join(sorted(definitions)) or "none"))
    if problems:
        raise InputError("; ".join(problems))
    return values


def _read_mapping(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (IOError, OSError) as exc:
        raise InputError("cannot read %r: %s" % (path, exc))
    except yaml.YAMLError as exc:
        raise InputError("%r is not valid YAML/JSON: %s" % (path, exc))


def _is_values(data):
    return isinstance(data, dict) and all(isinstance(k, str) and not isinstance(v, dict) for k, v in data.items())


def read_values_file(path, warn=None):
    """Reads a YAML/JSON mapping of input values; warns when a secrets file is readable by other users."""
    data = _read_mapping(path)
    if data is None:
        return {}
    if not _is_values(data):
        raise InputError("%r must be a mapping of names to values" % path)
    if warn is not None and os.name == "posix" and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warn("%r is accessible by other users; run: chmod 600 %s" % (path, path))
    return data


def read_config_file(path):
    """
    Reads a --config file, such as one client's settings for a source: `config` (values for spec.config) and
    `streams` (the streams to run). Returns (values, streams); streams is None when the file does not choose them.
    """
    data = _read_mapping(path)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        raise InputError("%r must be a mapping with `config` and `streams`" % path)
    unknown = sorted(str(key) for key in data if key not in CONFIG_FILE_SECTIONS)
    if unknown:
        hint = ("secrets go in --secrets FILE or %s<NAME> variables" % SECRET_ENV_PREFIX if "secrets" in unknown
                else "config values go under `config:`")
        raise InputError("%r: unknown section(s) %s (sections: %s); %s" % (
            path, ", ".join(unknown), ", ".join(CONFIG_FILE_SECTIONS), hint))
    values = data.get("config")
    if values is None:
        values = {}
    if not _is_values(values):
        raise InputError("%r: `config` must be a mapping of names to values" % path)
    streams = data.get("streams")
    if isinstance(streams, str):
        streams = [name.strip() for name in streams.split(",") if name.strip()]
    if streams is not None and (not isinstance(streams, list) or not streams or
                                not all(isinstance(name, str) for name in streams)):
        raise InputError("%r: `streams` must be a list of stream names" % path)
    return values, streams


def secrets_from_env(names, environ=None):
    """
    {name: value} for the declared secret `names` set as STREAMWRIGHT_SECRET_<NAME> (matched case-insensitively).
    Other STREAMWRIGHT_SECRET_* variables are ignored: they may belong to other sources.
    """
    environ = os.environ if environ is None else environ
    declared = dict((name.lower(), name) for name in names)
    found = {}
    for key, value in environ.items():
        if key.startswith(SECRET_ENV_PREFIX) and key[len(SECRET_ENV_PREFIX):].lower() in declared:
            found[declared[key[len(SECRET_ENV_PREFIX):].lower()]] = value
    return found


def check_names(values):
    """Raises InputError for names that cannot be referenced as {{ config.<name> }}."""
    bad = [name for name in values if not re.match(schema.NAME_PATTERN, str(name))]
    if bad:
        raise InputError("invalid input names: %s" % ", ".join(map(repr, bad)))
