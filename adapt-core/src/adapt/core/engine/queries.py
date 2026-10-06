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
Query builder calls in requests (docs/design/source-format.md).

Query builders are components (adapt.core.runtime.components.QueryBuilder), registered by connector packages in the
`adapt.query_builders` entry-point group: `gaql`, for example, comes with adapt-google-ads. In a request, a mapping
with a single key that is an installed builder's name is a call of that builder:

    arguments:
      query:
        gaql: {select: [campaign.id], from: campaign, where: [...]}

At run time the call's references are rendered first, with their types, and the builder replaces the call with the
query text it writes from them, checking, quoting and escaping every value, so input values are never pasted into
queries. This module finds and replaces calls; the query languages live in the builders.
"""


__all__ = ["QueryError", "BuiltQuery", "find_calls", "replace_calls", "build_queries"]


class QueryError(Exception):
    """A query builder cannot write a query from the values it was given."""


class BuiltQuery(object):
    """
    In requests checked before a run (Connector.check_request), stands for the text a query builder will write at run
    time, so a connector can accept it where it expects query text.
    """

    def __init__(self, builder):
        self.builder = builder

    def __repr__(self):
        return "<query built by %s>" % self.builder

    def __eq__(self, other):
        return isinstance(other, BuiltQuery) and other.builder == self.builder

    def __hash__(self):
        return hash(self.builder)


def _call(value, names):
    """(name, spec) when value is a call of one of the builders `names`, else None."""
    if isinstance(value, dict) and len(value) == 1:
        (name, spec), = value.items()
        if name in names:
            return name, spec
    return None


def find_calls(value, names, path=()):
    """(path, name, spec) of each call of the builders `names` in value (mappings and lists, recursively)."""
    found = _call(value, names)
    if found is not None:
        return [(path,) + found]
    calls = []
    if isinstance(value, dict):
        for key, item in value.items():
            calls += find_calls(item, names, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            calls += find_calls(item, names, path + (index,))
    return calls


def replace_calls(value, names, replace):
    """A copy of value in which each call of the builders `names` is replaced by replace(name, spec)."""
    found = _call(value, names)
    if found is not None:
        return replace(*found)
    if isinstance(value, dict):
        return dict((key, replace_calls(item, names, replace)) for key, item in value.items())
    if isinstance(value, list):
        return [replace_calls(item, names, replace) for item in value]
    return value


def _build(builder, name, spec):
    try:
        text = builder.build(spec)
    except QueryError:
        raise
    except Exception as exc:  # a builder that fails on unexpected input fails the query, with its name
        raise QueryError("%s: %s: %s" % (name, type(exc).__name__, exc))
    if not isinstance(text, str):
        raise QueryError("%s: the query builder returned %s, not text" % (name, type(text).__name__))
    return text


def build_queries(value, builders, calls):
    """
    The rendered value, with the builder calls found in its unrendered form (`calls`, from find_calls) replaced by
    the query text of `builders` ({name: QueryBuilder}): input values and API responses never become calls.
    """
    for path, name, _ in calls:
        try:
            node = value
            for part in path:
                node = node[part]
            spec = node[name]
        except (KeyError, IndexError, TypeError):
            raise QueryError("%s: the call at %s did not render to a mapping" % (name, list(path)))
        text = _build(builders[name], name, spec)
        if not path:
            return text
        container = value
        for part in path[:-1]:
            container = container[part]
        container[path[-1]] = text
    return value
