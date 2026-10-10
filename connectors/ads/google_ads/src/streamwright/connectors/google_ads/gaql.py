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
The `gaql` query builder: Google Ads Query Language text from a typed mapping, so input values are never pasted into
queries.

    query:
      gaql:
        select: [campaign.id, metrics.clicks]
        from: campaign
        where:
          - {field: segments.date, op: BETWEEN, type: date, value: ["{{ window.start }}", "{{ window.end }}"]}
          - {field: campaign.id, op: IN, type: int, value: "{{ config.campaign_ids }}", skip_if_empty: true}
        order_by: [metrics.clicks DESC]
        limit: 100

`where` items are joined with AND; `type` (int, string, enum, date) says how a value is written: numbers are checked,
text is quoted and escaped, enum values are bare names. `skip_if_empty: true` drops an item whose value (or a
BETWEEN bound) is missing. Grammar: https://developers.google.com/google-ads/api/docs/query/grammar
"""

import datetime
import difflib
import re

from streamwright.core.config.inputs import InputError, as_date
from streamwright.core.runtime.components import QueryBuilder
from streamwright.core.engine.queries import QueryError


__all__ = ["GaqlBuilder", "gaql", "OPERATORS", "VALUE_TYPES"]

KEYS = ("select", "from", "where", "order_by", "limit")
WHERE_KEYS = ("field", "op", "type", "value", "skip_if_empty")
OPERATORS = ("=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "NOT LIKE", "BETWEEN", "IS NULL",
             "IS NOT NULL", "CONTAINS ANY", "CONTAINS ALL", "CONTAINS NONE")
LIST_OPERATORS = ("IN", "NOT IN", "CONTAINS ANY", "CONTAINS ALL", "CONTAINS NONE")
NO_VALUE_OPERATORS = ("IS NULL", "IS NOT NULL")
VALUE_TYPES = ("int", "string", "enum", "date")

_FIELD = re.compile(r"^[a-z][a-zA-Z0-9._]*$")
_RESOURCE = re.compile(r"^[a-z][a-zA-Z0-9_]*$")
_ENUM = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ORDERING = re.compile(r"^(\S+)(?:\s+(ASC|DESC))?$", re.I)
_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _operator(item):
    return " ".join(str(item.get("op") or "").upper().split())


def _empty(value):
    return value is None or value == "" or value == []


def _skipped(item):
    """`skip_if_empty: true` drops an item whose value (or either BETWEEN bound) is missing."""
    if not item.get("skip_if_empty") or _operator(item) in NO_VALUE_OPERATORS:
        return False
    value = item.get("value")
    if _operator(item) == "BETWEEN" and isinstance(value, (list, tuple)) and value:
        return any(_empty(bound) for bound in value)
    return _empty(value)


def _reference(value):
    return isinstance(value, str) and "{{" in value


def _suggest(word, choices):
    match = difflib.get_close_matches(str(word), choices, n=1, cutoff=0.6)
    return " (did you mean %r?)" % match[0] if match else ""


# * --------------------------
# * checks: before a run starts
# * --------------------------

def _check_keys(mapping, allowed, required, path, what):
    problems = [(path + (key,), "unknown key %r%s; %s does not support it" % (key, _suggest(key, allowed), what))
                for key in mapping if key not in allowed]
    problems += [(path, "%s needs `%s`" % (what, key)) for key in required if key not in mapping]
    return problems


def check(spec):
    """Problems with an unrendered `gaql` mapping, as (path inside it, message); values may still be references."""
    if not isinstance(spec, dict):
        return [((), "`gaql` must be a mapping with `select` and `from`")]
    problems = _check_keys(spec, KEYS, ("select", "from"), (), "`gaql`")
    select = spec.get("select")
    if "select" in spec and not _reference(select):
        if not isinstance(select, list) or not select:
            problems.append((("select",), "`select` must be a non-empty list of fields"))
        else:
            problems += [(("select", i), "%r is not a field name" % (field,)) for i, field in enumerate(select)
                         if not _reference(field) and not (isinstance(field, str) and _FIELD.match(field))]
    resource = spec.get("from")
    if "from" in spec and not _reference(resource) and not (isinstance(resource, str) and _RESOURCE.match(resource)):
        problems.append((("from",), "%r is not a resource name" % (resource,)))
    if "where" in spec:
        problems += _check_where(spec["where"])
    limit = spec.get("limit")
    if "limit" in spec and not _reference(limit) and (isinstance(limit, bool) or not isinstance(limit, int) or
                                                      limit < 1):
        problems.append((("limit",), "`limit` must be a positive whole number, got %r" % (limit,)))
    return problems


def _check_where(items):
    if _reference(items):
        return []
    if not isinstance(items, list):
        return [(("where",), "`where` must be a list of conditions")]
    problems = []
    for i, item in enumerate(items):
        path = ("where", i)
        if not isinstance(item, dict):
            problems.append((path, "a where item must be a mapping with `field`, `op` and `type`"))
            continue
        problems += _check_keys(item, WHERE_KEYS, ("field", "op", "type"), path, "a where item")
        field = item.get("field")
        if "field" in item and not _reference(field) and not (isinstance(field, str) and _FIELD.match(field)):
            problems.append((path + ("field",), "%r is not a field name" % (field,)))
        op = _operator(item)
        if "op" in item and op not in OPERATORS:
            problems.append((path + ("op",), "unknown operator %r%s; one of: %s" % (
                item["op"], _suggest(op, OPERATORS), ", ".join(OPERATORS))))
        if "type" in item and item["type"] not in VALUE_TYPES:
            problems.append((path + ("type",), "unknown value type %r%s; one of: %s" % (
                item["type"], _suggest(item["type"], VALUE_TYPES), ", ".join(VALUE_TYPES))))
        if "skip_if_empty" in item and not isinstance(item["skip_if_empty"], bool):
            problems.append((path + ("skip_if_empty",), "`skip_if_empty` must be true or false"))
        value = item.get("value")
        if op in NO_VALUE_OPERATORS:
            if "value" in item:
                problems.append((path + ("value",), "`%s` takes no value" % op))
        elif "op" in item and op in OPERATORS and "value" not in item:
            problems.append((path, "`%s` needs a `value`" % op))
        elif op == "BETWEEN" and not _reference(value) and not (isinstance(value, list) and len(value) == 2):
            problems.append((path + ("value",), "`BETWEEN` needs a list of two values"))
        elif op in LIST_OPERATORS and not _reference(value) and not isinstance(value, list):
            problems.append((path + ("value",), "`%s` needs a list or a list reference" % op))
    return problems


# * ------------------------------------
# * building: from the rendered mapping
# * ------------------------------------

def _field(value):
    if not isinstance(value, str) or not _FIELD.match(value):
        raise QueryError("gaql: %r is not a valid field name" % (value,))
    return value


def _text(text):
    return "'%s'" % "".join(_ESCAPES.get(char, char) for char in text)


def _value(kind, value, field):
    if value is None:
        raise QueryError("gaql: %s has an empty value" % field)
    if kind == "int":
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            raise QueryError("gaql: %s expects a whole number, got %r" % (field, value))
        try:
            return str(int(str(value).strip()) if not isinstance(value, float) else int(value))
        except ValueError:
            raise QueryError("gaql: %s expects a whole number, got %r" % (field, value))
    if kind == "date":
        if not isinstance(value, (datetime.date, str)):
            raise QueryError("gaql: %s expects a date, got %r" % (field, value))
        try:
            return _text(as_date(value).isoformat())
        except InputError as exc:
            raise QueryError("gaql: %s: %s" % (field, exc))
    if kind == "enum":
        text = str(value)
        if not _ENUM.match(text):
            raise QueryError("gaql: %s expects an enum value (letters, digits, _), got %r" % (field, value))
        return text
    if isinstance(value, (dict, list)):
        raise QueryError("gaql: %s expects text, got %s" % (field, type(value).__name__))
    return _text(str(value))


def _condition(item):
    field = _field(item.get("field"))
    op = _operator(item)
    if op not in OPERATORS:
        raise QueryError("gaql: %r is not a supported operator (%s)" % (item.get("op"), ", ".join(OPERATORS)))
    kind = item.get("type", "string")
    value = item.get("value")
    if op in NO_VALUE_OPERATORS:
        return "%s %s" % (field, op)
    if op == "BETWEEN":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise QueryError("gaql: %s BETWEEN needs two values, got %r" % (field, value))
        return "%s BETWEEN %s AND %s" % (field, _value(kind, value[0], field), _value(kind, value[1], field))
    if op in LIST_OPERATORS:
        values = value if isinstance(value, (list, tuple)) else [value]
        if not values:
            raise QueryError("gaql: %s %s needs at least one value" % (field, op))
        return "%s %s (%s)" % (field, op, ", ".join(_value(kind, v, field) for v in values))
    return "%s %s %s" % (field, op, _value(kind, value, field))


def gaql(spec):
    """SELECT ... FROM ... [WHERE ...] [ORDER BY ...] [LIMIT n] from a rendered `gaql` mapping."""
    if not isinstance(spec, dict):
        raise QueryError("gaql: expected a mapping with `select` and `from`")
    select = spec.get("select")
    if not isinstance(select, list) or not select:
        raise QueryError("gaql: `select` needs at least one field")
    resource = spec.get("from")
    if not isinstance(resource, str) or not _RESOURCE.match(resource):
        raise QueryError("gaql: %r is not a valid resource name" % (resource,))
    query = "SELECT %s FROM %s" % (", ".join(_field(field) for field in select), resource)
    conditions = [_condition(item) for item in spec.get("where") or [] if not _skipped(item)]
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    order_by = spec.get("order_by")
    if order_by:
        orderings = []
        for ordering in order_by if isinstance(order_by, list) else [order_by]:
            match = _ORDERING.match(str(ordering).strip())
            if not match:
                raise QueryError("gaql: %r is not a valid ordering (field [ASC|DESC])" % (ordering,))
            orderings.append(_field(match.group(1)) + (" " + match.group(2).upper() if match.group(2) else ""))
        query += " ORDER BY " + ", ".join(orderings)
    limit = spec.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise QueryError("gaql: `limit` must be a positive whole number, got %r" % (limit,))
        query += " LIMIT %d" % limit
    return query


class GaqlBuilder(QueryBuilder):
    """`{gaql: {...}}` in a request: registered in the streamwright.query_builders entry-point group."""

    name = "gaql"
    connector = "google_ads"

    def check(self, spec):
        return check(spec)

    def build(self, spec):
        return gaql(spec)
