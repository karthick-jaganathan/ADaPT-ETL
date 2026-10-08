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
Rendering of {{ references }}, and the text and JSON of values (to_text, to_json).

A value that is exactly one reference keeps its type ("{{ config.ids }}" stays a list); text containing
references renders to a string. streamwright-validate has already checked scopes, names and filters.

DECIMAL values (decimal.Decimal, from SQL steps) are exact: as text and in JSON they are written in plain notation,
without an exponent or trailing zeros (decimal_text: 2.60 is 2.6, 100.00 is 100), and a value that is exactly one
reference to one is a JSON number (an int when it is whole, else the nearest float), which requests and connectors take.
"""

import datetime
import decimal
import json
import re
import uuid

from streamwright.core.config.inputs import as_date
from streamwright.core.validation.schema import split_outside_quotes


__all__ = ["TemplateError", "render", "evaluate", "to_text", "to_json", "decimal_text", "json_numbers", "get_path",
           "expressions"]

_TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.S)
_FILTER = re.compile(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", re.S)
# a Decimal while to_json writes a value: a JSON string that no other value can be (its random part is this process's
# own), replaced by the number's text once json.dumps has written the rest
_DECIMAL_MARK = "streamwright-decimal-%s-" % uuid.uuid4().hex
_DECIMAL_MARKS = re.compile('"%s([0-9]+)"' % re.escape(_DECIMAL_MARK))


class TemplateError(Exception):
    pass


def get_path(data, path):
    """data[path]; when there is no such key, a dotted path is followed through nested mappings (KeyError if not)."""
    try:
        return data[path]
    except (KeyError, TypeError, IndexError):
        if not (isinstance(path, str) and "." in path):
            raise KeyError(path)
    value = data
    try:
        for part in path.split("."):
            value = value[part]
    except (KeyError, TypeError, IndexError):
        raise KeyError(path)
    return value


def decimal_text(value):
    """
    The exact value of a decimal.Decimal in plain notation, without an exponent or trailing zeros:
    Decimal("2.60") -> "2.6", Decimal("100.00") -> "100", Decimal("0.00") -> "0". NaN and infinities give their names.
    """
    text = format(value, "f")  # (exact: format() rounds only to a precision it is given)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def to_json(value, sort_keys=False, default=None):
    """
    json.dumps(value, sort_keys=sort_keys, default=default), with every decimal.Decimal in it (at any depth) written
    as an exact JSON number, its decimal_text: 2.6, 100, 12345678901234567890.123456 (NaN and infinities as null).
    Everything else is written as json.dumps writes it.
    """
    numbers = []

    def encode(item):
        if isinstance(item, decimal.Decimal):
            numbers.append(decimal_text(item) if item.is_finite() else "null")
            return "%s%d" % (_DECIMAL_MARK, len(numbers) - 1)
        if default is None:
            raise TypeError("Object of type %s is not JSON serializable" % type(item).__name__)
        return default(item)

    text = json.dumps(value, sort_keys=sort_keys, default=encode)
    return _DECIMAL_MARKS.sub(lambda match: numbers[int(match.group(1))], text) if numbers else text


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, decimal.Decimal):
        return decimal_text(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return ",".join(to_text(item) for item in value)
    if isinstance(value, dict):
        return to_json(value, default=str, sort_keys=True)
    return str(value)


def json_numbers(value):
    """A value with its decimal.Decimal values (in lists and mappings too) as JSON numbers: int if whole, else float."""
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return None
        text = decimal_text(value)
        return float(text) if "." in text else int(text)
    if isinstance(value, dict):
        return dict((key, json_numbers(item)) for key, item in value.items())
    if isinstance(value, list):
        return [json_numbers(item) for item in value]
    return value


def _literal(text):
    if text[:1] in ("'", '"'):
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    return float(text) if "." in text else int(text)


def _filter_date(value, fmt):
    return None if value is None else as_date(value).strftime(fmt)


_FILTERS = {
    "default": lambda value, fallback: fallback if value is None or value == "" else value,
    "join": lambda value, sep: sep.join(to_text(v) for v in value) if isinstance(value, (list, tuple))
    else to_text(value),
    "date": _filter_date,
    "int": lambda value: None if value is None else int(value),
    "lower": lambda value: None if value is None else to_text(value).lower(),
    "upper": lambda value: None if value is None else to_text(value).upper(),
}


def evaluate(expression, scopes):
    """Evaluates the inside of one {{ ... }}."""
    parts = split_outside_quotes(expression, "|")
    scope, _, attribute = parts[0].partition(".")
    if scope == "today":
        value = scopes.get("today") or datetime.date.today()
    elif scope not in scopes:
        raise TemplateError("{{ %s }} is not available here" % expression.strip())
    elif not attribute:
        value = scopes[scope]
    else:
        try:
            value = get_path(scopes[scope], attribute)
        except KeyError:
            value = None
    for text in parts[1:]:
        match = _FILTER.match(text)
        if not match or match.group(1) not in _FILTERS:
            raise TemplateError("invalid filter %r in {{ %s }}" % (text, expression.strip()))
        arguments = [_literal(a) for a in split_outside_quotes(match.group(2), ",") if a] if match.group(2) else []
        try:
            value = _FILTERS[match.group(1)](value, *arguments)
        except (TypeError, ValueError) as exc:
            raise TemplateError("filter %r failed in {{ %s }}: %s" % (match.group(1), expression.strip(), exc))
    return value


def expressions(text):
    """The inside of each {{ ... }} reference in a text."""
    return [match.group(1) for match in _TEMPLATE.finditer(text)] if isinstance(text, str) else []


def render(value, scopes):
    """Renders every reference in value (recursively); exactly one reference to a DECIMAL is a JSON number."""
    if isinstance(value, dict):
        return dict((key, render(item, scopes)) for key, item in value.items())
    if isinstance(value, list):
        return [render(item, scopes) for item in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    stripped = value.strip()
    if stripped.startswith("{{") and stripped.endswith("}}") and stripped.count("{{") == 1:
        return json_numbers(evaluate(stripped[2:-2], scopes))
    return _TEMPLATE.sub(lambda match: to_text(evaluate(match.group(1), scopes)), value)
