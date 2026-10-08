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
SQL in sources (docs/design/source-format.md): each stream shapes what its requests read with the SQL steps of its
`transform`, DuckDB queries over the stream's own request tables and earlier steps; its `export` names the steps
that are written. Streams never read each other's tables.

Each request of a stream is a table named after it, with one row per record the request read:

    record        JSON  the record as the API returned it: record->>'$.campaign.name', (record->>'clicks')::BIGINT
    partition     JSON  the partition's values: partition->>'account_id'
    config        JSON  the source's config values: config->>'currency'
    window_start  DATE  the incremental window (null for requests that do not use it)
    window_end    DATE
    today         DATE

`$name` in a step is the value of the config input `name` (spec.config): a bound parameter, never SQL text, with the
type spec.config gives it (PARAMETER_TYPES: string VARCHAR, integer BIGINT, number DOUBLE, boolean BOOLEAN, date
DATE, list a LIST of its items' type). A value that is not given and has no default is null. Secrets are never
parameters. A value is tested against a list with `x = ANY($ids)`, `list_contains($ids, x)` or `x IN $ids`; IN and
comparisons that take a list as one value (`x IN ($ids)`, `x = $ids`) are reported.

DuckDB runs embedded and locked down: each step is one SELECT statement that reads only its stream's tables (and its
own CTEs) and the table functions range, generate_series, unnest, json_each and json_tree; no files, network,
extensions, settings or logs; one thread and 1GB of memory, spilling to a private temporary folder that is removed
after the run. In `page` mode the steps of one page get 60 seconds, and each step at most 1,000,000 rows; in `run`
mode each step gets 10 minutes.

These limits are best effort inside the process: DuckDB checks the time between batches of rows (one slow function
call runs to its end), and memory outside its buffer manager is not counted. When the queries are not trusted (a
platform running customers' sources), run each connector run in its own container with hard memory, CPU, disk and
time limits.
"""

import base64
import datetime
import decimal
import json
import math
import re
import shutil
import tempfile
import threading
import uuid

from streamwright.core.runtime.templates import to_json


__all__ = ["SqlError", "COLUMNS", "MODES", "PARAMETER_TYPES", "Sandbox", "SourcePlan", "StreamPlan", "table_schema",
           "param_type", "partition_fields", "exports_of", "incremental_exports", "as_json", "check_source",
           "plan_source"]

COLUMNS = (("record", "JSON"), ("partition", "JSON"), ("config", "JSON"), ("window_start", "DATE"),
           ("window_end", "DATE"), ("today", "DATE"))
LIMITS = {"memory_limit": "1GB", "threads": 1}
TIMEOUT = 60.0             # seconds for the steps of one page (page mode), and for compiling a query
TRANSFORM_TIMEOUT = 600.0  # seconds for one step of a run-mode stream
MAX_ROWS = 1000000         # rows a page-mode step can make from one page
MODES = ("page", "run")
# table functions a query can use (no others: they read settings, files or change the connection)
TABLE_FUNCTIONS = ("range", "generate_series", "unnest", "json_each", "json_tree")
# functions that read or change the connection's settings or logs (built-in macros that run a subquery, which could
# read other table functions, are denied too: Sandbox.denied)
DENIED_FUNCTIONS = ("current_setting", "getvariable", "getenv", "enable_logging", "disable_logging",
                    "truncate_duckdb_logs", "write_log", "query", "query_table")
# the DuckDB type of a config input (by its spec.config type) as a `$name` parameter; a list is a LIST of its items'
PARAMETER_TYPES = {"string": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE"}
# a `$name` parameter in a step: a scalar subquery, which DuckDB never folds into a constant, so a step's column types
# are the same whatever the values (`'a' || NULL` folds to an INTEGER NULL); a plain cast where DuckDB allows no
# subquery (lambdas, arguments that must be constants, `= ANY(...)`); right after IN, either one in a cast: `x IN $ids`
# is DuckDB's list membership, which `x IN (...)` is not
_SCALAR, _CAST, _BARE = "(SELECT CAST(%s AS %s))", "(CAST(%s AS %s))", "CAST(%s AS %s)"
# the errors of the places where DuckDB takes no subquery (lambdas, arguments that must be constants, non-inner joins,
# reused column aliases, TRY, ...), the only ones that make a step's parameters plain casts; and of the lists of ANY
# and ALL: DuckDB reads `x = ANY($ids)` as `x = ANY(SELECT unnest($ids))`, but `x = ANY((SELECT ...))` compares x
# with the subquery's one row, the whole list
_NO_SUBQUERY = re.compile(r"subquer|constant", re.I)
_QUANTIFIED = "IN/ANY/ALL"
_LIST_COMPARED = ("`$%(name)s` is a list (%(type)s), which IN and comparisons take as one value: use "
                  "`= ANY($%(name)s)` or `list_contains($%(name)s, x)` (to compare whole lists, cast it: "
                  "`$%(name)s::%(type)s`)")
# the values plain casts are compiled with (a NULL folds into an untyped constant): their types are the values'
_SAMPLES = {"VARCHAR": "x", "BIGINT": 1, "DOUBLE": 1.0, "BOOLEAN": True, "DATE": datetime.date(2000, 1, 1)}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PARAMETER = re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)")  # a `$name` parameter, in a query's UTF-8 bytes
_IN = re.compile(rb"IN\b", re.I)  # the keyword IN, at the start of a token
_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
             "UHUGEINT")
_MULTIWORD_TYPES = ("TIMESTAMP WITH TIME ZONE", "TIME WITH TIME ZONE", "DOUBLE PRECISION")
_ARRAYS = re.compile(r"(\[\d*\])+$")  # the list and array suffixes of a type: TIMESTAMP WITH TIME ZONE[]
_BUILTIN = "%r is the name of a DuckDB built-in table or view: name it otherwise"
_READS = "a step reads its stream's requests and earlier steps"


class SqlError(Exception):
    """A step's query that cannot run."""


def _message(exc):
    """DuckDB's message without its query excerpt: the error and its hints, on one line."""
    text = str(exc).strip().split("\n\n")[0]
    return " ".join(line.strip() for line in text.splitlines() if line.strip()) or type(exc).__name__


def _json_text(value):
    return to_json(value, default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item))


def as_json(value):
    """A value as it reads back from a JSON column (dates become text, decimals numbers)."""
    return json.loads(_json_text(value))


def _checked(name):
    """A request, step or column name, which can be a table or column name: letters, digits and _."""
    if not (isinstance(name, str) and _NAME.match(name)):
        raise SqlError("%r is not a valid table or column name" % (name,))
    return name


def _quoted(name):
    """The SQL identifier of a request, step or column name."""
    return '"%s"' % _checked(name)


def _identifier(name):
    """Any column name as a SQL identifier (a step's columns can have any names)."""
    return '"%s"' % str(name).replace('"', '""')


def _column_problem(names):
    """Why result columns with these names cannot be output fields, or None."""
    for name in names:
        if not _NAME.match(name):
            return ("column %r is not a valid field name (letters, digits and _, not starting with a digit): name it "
                    "with AS" % name)
    # DuckDB compares names without case (a table keeps one of `id` and `Id`), and so do most warehouses
    lowered = [name.lower() for name in names]
    repeated = sorted(set(name for name in names if lowered.count(name.lower()) > 1))
    if repeated:
        return "columns named more than once: %s%s" % (", ".join(repeated), " (names ignore case)" if len(
            repeated) > len(set(name.lower() for name in repeated)) else "")
    return None


def _pointer(key):
    """The JSON pointer (RFC 6901) of a key of a JSON object: any key, with no quoting rules to get wrong."""
    return "/" + key.replace("~", "~0").replace("/", "~1")


def _partition_condition(partition):
    """
    (SQL, parameters) that select the raw rows read for a stream partition: their `partition` has its keys, with
    values that are equal as JSON ("1" is not 1). The values are parameters, never SQL text.
    """
    if not partition:
        return "true", {}
    conditions, params = [], {"scope": _json_text(partition)}
    for position, key in enumerate(partition):
        params["key%d" % position] = _pointer(str(key))
        conditions.append('json_extract("partition", $key%d) = json_extract($scope::JSON, $key%d)' % (
            position, position))
    return " AND ".join(conditions), params


def _path_text(path):
    """("transform", "steps", 0, "select") -> "transform.steps[0].select"."""
    text = ""
    for part in path:
        text += "[%d]" % part if isinstance(part, int) else ("." if text else "") + str(part)
    return text


def table_schema(columns):
    """The JSON Schema of records with these (name, DuckDB type) columns."""
    return {"type": "object", "properties": dict((name, _schema(kind)) for name, kind in columns)}


def param_type(definition):
    """The DuckDB type of a config input (its spec.config entry) as a `$name` parameter, or None if it has none."""
    if not isinstance(definition, dict):
        return None
    if definition.get("type") == "list":
        item = PARAMETER_TYPES.get(definition.get("items") or "string")
        return item + "[]" if item else None
    return PARAMETER_TYPES.get(definition.get("type"))


def _sample(kind):
    """A value of a parameter type (PARAMETER_TYPES, or a list of one), to compile a plain cast with."""
    return [_sample(kind[:-2])] if kind.endswith("[]") else _SAMPLES[kind]


# * ------------------------------------------
# * DuckDB types: JSON Schema and plain values
# * ------------------------------------------

def _split(text):
    """Splits a type's arguments at the commas outside parentheses, brackets and quotes ("names", 'enum values')."""
    parts, depth, quote, current = [], 0, None, ""
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None  # a doubled quote inside a name or value closes and reopens it
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0 and quote is None:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _field(text):
    """(name, type) of a STRUCT field: `"b c" INTEGER[]` or `raw JSON`."""
    if text.startswith('"'):
        end = 1
        while end < len(text):
            if text[end] == '"' and text[end + 1:end + 2] != '"':
                break
            end += 2 if text[end] == '"' else 1
        return text[1:end].replace('""', '"'), text[end + 1:].strip()
    name, _, kind = text.partition(" ")
    return name, kind.strip()


def _unnamed(fields):
    """Whether STRUCT fields are bare types, as from row(1, 'x'): its values are tuples."""
    return all(" " not in field.split("(")[0] or _ARRAYS.sub("", field.upper()) in _MULTIWORD_TYPES
               for field in fields)


def _unnamed_struct(kind):
    """Whether a type has an unnamed STRUCT (row(1, 'x') or (1, 'x')) at any depth: DuckDB's tables cannot hold one."""
    shape = _shape(kind)
    if shape[0] == "list":
        return _unnamed_struct(shape[1])
    if shape[0] == "map":
        return _unnamed_struct(shape[1]) or _unnamed_struct(shape[2])
    if shape[0] in ("struct", "union"):
        if shape[0] == "struct" and _unnamed(shape[1]):
            return True
        return any(_unnamed_struct(_field(field)[1]) for field in shape[1])
    return False


def _shape(kind):
    """("list", item) | ("struct", fields) | ("map", key, value) | ("union", members) | ("json",) | ("scalar", KIND)."""
    kind = kind.strip()
    upper = kind.upper()
    if upper.endswith("]") and "[" in kind:  # LIST (INTEGER[]) and ARRAY (INTEGER[3])
        return ("list", kind[:kind.rfind("[")])
    for name in ("STRUCT", "MAP", "UNION"):
        if upper.startswith(name + "(") and upper.endswith(")"):
            arguments = _split(kind[len(name) + 1:-1])
            if name == "MAP":
                return ("map",) + tuple((arguments + ["", ""])[:2])
            return (name.lower(), tuple(arguments))
    return ("json",) if upper == "JSON" else ("scalar", upper)


def _schema(kind):
    """The JSON Schema of a DuckDB column type (all columns are nullable)."""
    shape = _shape(kind)
    if shape[0] == "list" or shape[0] == "struct" and _unnamed(shape[1]):
        return {"type": ["null", "array"]}
    if shape[0] in ("struct", "map"):
        return {"type": ["null", "object"]}
    if shape[0] in ("json", "union"):
        return {}
    kind = shape[1]
    if kind == "BOOLEAN":
        return {"type": ["null", "boolean"]}
    if kind in _INTEGERS:
        return {"type": ["null", "integer"]}
    if kind in ("FLOAT", "DOUBLE", "REAL", "INTERVAL") or kind.startswith("DECIMAL"):  # intervals: seconds
        return {"type": ["null", "number"]}
    if kind == "DATE":
        return {"type": ["null", "string"], "format": "date"}
    if kind.startswith("TIMESTAMP"):
        return {"type": ["null", "string"], "format": "date-time"}
    if kind.startswith("TIME"):
        return {"type": ["null", "string"], "format": "time"}
    return {"type": ["null", "string"]}


def _plain(value):
    """
    A DuckDB value as plain data, ready for JSON (NaN and infinities are null: JSON has neither). DECIMAL values stay
    exact decimal.Decimal values, which outputs write as exact numbers (templates.to_json, to_text), and integers
    (HUGEINT too) are Python's exact ints.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return dict((str(key), _plain(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _decode(value, kind):
    """_plain(value), with the JSON values inside it (at any depth, per the column's type) decoded to data."""
    if value is None or "JSON" not in kind.upper():
        return _plain(value)
    shape = _shape(kind)
    if shape[0] == "json":
        return _plain(json.loads(value)) if isinstance(value, str) else _plain(value)
    if shape[0] == "list" and isinstance(value, (list, tuple)):
        return [_decode(item, shape[1]) for item in value]
    if shape[0] == "struct" and isinstance(value, dict):
        kinds = dict(_field(field) for field in shape[1])
        return dict((str(key), _decode(item, kinds.get(key, ""))) for key, item in value.items())
    if shape[0] == "struct" and isinstance(value, (list, tuple)):
        return [_decode(item, field) for item, field in zip(value, shape[1])]
    if shape[0] == "map" and isinstance(value, dict):
        return dict((str(key), _decode(item, shape[2])) for key, item in value.items())
    return _plain(value)


# * -------
# * sandbox
# * -------

def _references(tree):
    """
    The tables, CTEs, table functions and functions a serialized query uses (lowercase, as DuckDB compares them), and
    its parameters (each use, as written).
    """
    found = {"tables": set(), "ctes": set(), "table_functions": set(), "functions": set(), "parameters": []}

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "BASE_TABLE" and "table_name" in node:
                qualified = bool(node.get("schema_name") or node.get("catalog_name"))
                found["tables"].add(("." if qualified else "") + str(node["table_name"]).lower())
            elif node.get("type") == "TABLE_FUNCTION":
                found["table_functions"].add(str((node.get("function") or {}).get("function_name")).lower())
            if node.get("class") == "FUNCTION" and node.get("function_name"):
                found["functions"].add(str(node["function_name"]).lower())
            if node.get("class") == "PARAMETER":
                found["parameters"].append(str(node.get("identifier")))
            cte_map = node.get("cte_map")
            if isinstance(cte_map, dict):
                found["ctes"].update(str(entry.get("key")).lower() for entry in cte_map.get("map") or [])
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(tree)
    return found


def _nodes(tree):
    """Every node (mapping) of a serialized query."""
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def _is_list(node, types):
    """Whether an expression of a serialized query is a list for sure: a list parameter or literal, or a cast to one."""
    if not isinstance(node, dict):
        return False
    if node.get("class") == "PARAMETER":
        return (types.get(node.get("identifier")) or "").endswith("]")
    if node.get("class") == "CAST":
        return (node.get("cast_type") or {}).get("id") in ("LIST", "ARRAY")
    return node.get("type") == "ARRAY_CONSTRUCTOR" or str(node.get("function_name")).lower() == "list_value"


def _compared_lists(tree, types):
    """
    The list parameters (`types`: name -> DuckDB type) that a serialized query compares as one value with something
    that is not a list, `x IN ($ids)` or `x = $ids`: DuckDB casts x to a list, which compiles against empty tables
    but fails on every row of a run.
    """
    found = set()
    for node in _nodes(tree):
        if node.get("class") == "COMPARISON":
            pairs = [(node.get("left"), node.get("right"))]
        elif node.get("class") == "OPERATOR" and node.get("type") in ("COMPARE_IN", "COMPARE_NOT_IN"):
            children = node.get("children") or [None]
            pairs = [(children[0], item) for item in children[1:]]
        else:
            continue
        for left, right in pairs:
            for operand, other in ((left, right), (right, left)):
                if isinstance(operand, dict) and operand.get("class") == "PARAMETER" and \
                        _is_list(operand, types) and not _is_list(other, types):
                    found.add(operand.get("identifier"))
    return sorted(found)


def _quantified(tree):
    """Whether a `$name` parameter is in the list of an ANY or ALL comparison, `x = ANY($ids)`, of a parsed query."""
    for node in _nodes(tree):
        if node.get("class") != "SUBQUERY" or node.get("subquery_type") != "ANY":
            continue
        select = (node.get("subquery") or {}).get("node") or {}
        items = select.get("select_list") or []
        # DuckDB reads `x = ANY(list)` as `x = ANY(SELECT unnest(list))`, with no FROM
        if (select.get("from_table") or {}).get("type") == "EMPTY" and len(items) == 1 and \
                str(items[0].get("function_name")).lower() == "unnest" and \
                any(item.get("class") == "PARAMETER" for item in _nodes(items[0])):
            return True
    return False


class Sandbox(object):
    """
    The DuckDB of one run, locked down: in memory, spilling to a private temporary folder (removed by close()), with
    the tables of the stream that runs (its requests and steps).
    """

    def __init__(self, timeout=TIMEOUT, max_rows=MAX_ROWS):
        import duckdb
        self.duckdb = duckdb
        self.timeout = timeout
        self.max_rows = max_rows
        self.folder = tempfile.mkdtemp(prefix="streamwright-sql-")
        try:
            # no files (DuckDB itself spills to the private folder), network or extensions, and no reading the
            # Python objects of the calling code (replacement scans)
            config = dict(LIMITS, enable_external_access=False, autoinstall_known_extensions=False,
                          autoload_known_extensions=False, python_enable_replacements=False,
                          temp_directory=self.folder)
            self.connection = duckdb.connect(":memory:", config=config)
        except Exception:
            shutil.rmtree(self.folder, ignore_errors=True)
            raise
        self.denied = set(DENIED_FUNCTIONS) | set(row[0] for row in self.connection.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'macro' "
            "AND regexp_matches(macro_definition, '\\b(select|from)\\b', 'i')").fetchall())
        # the built-in views a query reaches without a schema (pg_settings, duckdb_tables, ...): no WITH query,
        # request or step can take their names, so a name the query may read is never one of them instead
        self.relations = set(row[0].lower() for row in self.connection.execute(
            "SELECT view_name FROM duckdb_views() WHERE schema_name IN ('main', 'pg_catalog') "
            "UNION SELECT table_name FROM duckdb_tables() WHERE schema_name IN ('main', 'pg_catalog')").fetchall())
        self.connection.execute("SET TimeZone = 'UTC'")  # timestamps with time zones are written in UTC
        self.connection.execute("SET lock_configuration = true")

    def close(self):
        try:
            self.connection.close()
        finally:
            shutil.rmtree(self.folder, ignore_errors=True)

    def _timed(self, work, timeout=None):
        """
        work(), interrupted after `timeout` seconds (DuckDB stops between batches of rows). The interrupt repeats
        until the work ends: one sent between two statements would be lost, and the next statement not stopped.
        """
        timeout = timeout or self.timeout
        done, expired = threading.Event(), []

        def watchdog():
            if done.wait(timeout):
                return
            expired.append(True)
            while not done.is_set():
                self.connection.interrupt()
                done.wait(0.05)
        thread = threading.Thread(target=watchdog, name="streamwright-sql-timeout")
        thread.daemon = True
        thread.start()
        try:
            return work()
        except self.duckdb.Error as exc:
            if isinstance(exc, self.duckdb.InterruptException) or expired:
                raise SqlError("the query took longer than %gs" % timeout)
            raise SqlError(_message(exc))
        finally:
            done.set()
            thread.join()  # no interrupt can reach the next query

    def _execute(self, sql, parameters=None):
        return self.connection.execute(sql, parameters) if parameters else self.connection.execute(sql)

    def parse(self, sql, tables):
        """
        (text, tables read, parameters) of a step's query: exactly one SELECT, reading only `tables` (and its own
        CTEs), the allowed table functions and no function that reads or changes settings. The tables read are
        lowercase, as DuckDB compares names; the parameters are the names of its `$name` parameters, once per use.
        """
        if not isinstance(sql, str) or not sql.strip():
            raise SqlError("`select` must be a SQL query")
        try:
            statements = self.duckdb.extract_statements(sql)
        except self.duckdb.Error as exc:
            raise SqlError(_message(exc))
        kinds = [statement.type.name for statement in statements]
        if kinds != ["SELECT"]:
            # a PIVOT without its values listed first creates a type for them
            hint = " (list the values of a PIVOT: ON column IN ('a', 'b'))" if "PIVOT" in sql.upper() else ""
            raise SqlError("`select` must be one SELECT statement, found: %s%s" % (", ".join(kinds) or "none", hint))
        query = statements[0].query.strip().rstrip(";").strip()
        used = _references(self.tree(query))
        tables = set(name.lower() for name in tables)
        # WITH queries have plain names that no table or view has, so a name the query reads is one of its WITH
        # queries (where that is in scope), a table it can read, or nothing (DuckDB then reports it)
        unnamed = sorted(name for name in used["ctes"] if not _NAME.match(name))
        if unnamed:
            raise SqlError("WITH queries need plain names (letters, digits and _), not %s" % ", ".join(unnamed))
        shadowing = sorted(used["ctes"] & tables)
        if shadowing:
            raise SqlError("WITH queries cannot be named %s: the query can read tables with these names; rename them"
                           % ", ".join(shadowing))
        shadowing = sorted(used["ctes"] & self.relations)
        if shadowing:
            raise SqlError("WITH queries cannot be named %s: DuckDB has tables or views with these names; rename "
                           "them" % ", ".join(shadowing))
        allowed = tables | used["ctes"]
        unknown = sorted(name for name in used["tables"] if name.startswith(".") or name not in allowed)
        if unknown:
            raise SqlError("the query can only read %s, not %s" % (", ".join(sorted(tables)) or "no tables", ", ".join(
                name.lstrip(".") + (" (with a schema)" if name.startswith(".") else "") for name in unknown)))
        functions = sorted(used["table_functions"] - set(TABLE_FUNCTIONS))
        if functions:
            raise SqlError("table function(s) %s are not allowed (allowed: %s)" % (", ".join(functions),
                                                                                   ", ".join(TABLE_FUNCTIONS)))
        denied = sorted(used["functions"] & self.denied)
        if denied:
            raise SqlError("function(s) %s are not allowed" % ", ".join(denied))
        if any(not _NAME.match(name) for name in used["parameters"]):
            raise SqlError("parameters are config inputs named in the query, `$name`: not ? or $1")
        return query, used["tables"] - used["ctes"], used["parameters"]

    def tree(self, query):
        """The parse tree of a query, as DuckDB serializes it (json_serialize_sql)."""
        tree = json.loads(self.connection.execute("SELECT json_serialize_sql($query)", {"query": query}).fetchone()[0])
        if tree.get("error"):
            raise SqlError("%s (write the query as a SELECT)" % tree.get("error_message", "the query cannot be read"))
        return tree

    def typed(self, query, parameters, types, scalar=True):
        """
        The query with each use of a `$name` parameter given its type (`types`: name -> DuckDB type): a scalar
        subquery, (SELECT CAST($name AS BIGINT)), whose type DuckDB keeps whatever the value (null too); with
        `scalar` false, a plain cast, (CAST($name AS BIGINT)). Right after IN, either goes in a cast: `x IN $ids` is
        DuckDB's list membership, contains($ids, x), and `x IN (...)` a list of values. Only these are added: the
        values stay bound parameters. `parameters` are the parameters DuckDB's parser found (parse); its tokenizer
        must find the same.
        """
        if not parameters:
            return query
        data = query.encode("utf-8")  # the tokenizer's positions are UTF-8 byte offsets
        found, pieces, last, previous = [], [], 0, None
        for start, kind in self.duckdb.tokenize(query):
            before, previous = previous, (start, kind)
            if kind != self.duckdb.token_type.operator or data[start:start + 1] != b"$":
                continue
            match = _PARAMETER.match(data, start)
            name = match.group(1).decode("ascii") if match is not None else None
            if name not in types:
                continue
            found.append(name)
            text = (_SCALAR if scalar else _CAST) % (match.group(0).decode("ascii"), types[name])
            if before is not None and before[1] == self.duckdb.token_type.keyword and _IN.match(data, before[0]):
                text = _BARE % (text, types[name])
            pieces += [data[last:start], text.encode("utf-8")]
            last = match.end()
        if sorted(found) != sorted(parameters):
            raise SqlError("cannot tell the `$name` parameters of the query apart: write each as `$name`")
        return (b"".join(pieces) + data[last:]).decode("utf-8")

    def describe(self, query, parameters=None):
        """The (name, type) of the query's result columns, from empty tables: no data needed."""
        rows = self._timed(lambda: self._execute("DESCRIBE " + query, parameters).fetchall())
        return [(row[0], row[1]) for row in rows]

    def _conform(self, name, columns):
        """
        Gives the table `name` these (name, type) columns if DuckDB typed it otherwise: a step whose parameters are
        plain casts gets an INTEGER column where a null value folded an expression into an untyped NULL.
        """
        if [(row[0], row[1]) for row in self.connection.execute("DESCRIBE %s" % _quoted(name)).fetchall()] == list(
                columns):
            return
        self.connection.execute('DROP TABLE IF EXISTS "streamwright conform"')
        self.connection.execute('CREATE TABLE "streamwright conform" AS SELECT %s FROM %s' % (", ".join(
            "CAST(%s AS %s) AS %s" % (_identifier(column), kind, _identifier(column)) for column, kind in columns),
            _quoted(name)))
        self.connection.execute("DROP TABLE %s" % _quoted(name))
        self.connection.execute('ALTER TABLE "streamwright conform" RENAME TO %s' % _quoted(name))

    def materialize(self, name, query, parameters=None, columns=None, timeout=TRANSFORM_TIMEOUT):
        """Runs a step's query once, into the table `name` (replaced); with `columns`, of these types (_conform)."""
        def work():
            self.connection.execute("DROP TABLE IF EXISTS %s" % _quoted(name))
            self.connection.sql(query, params=parameters or None).create(_checked(name))
            if columns is not None:
                self._conform(name, columns)
        self._timed(work, timeout)

    def run_page(self, request, records, scope, steps, timeout=None):
        """
        Page mode: one page of `records` becomes the table of the stream's request, then the steps run in order, each
        into its table (`steps`: (name, query, parameters, columns), see materialize) and making at most max_rows
        rows; all within the time limit. Errors name the step.
        """
        running = []

        def work():
            self.connection.execute("DELETE FROM %s" % _quoted(request))
            self._insert(request, records, scope)
            for name, query, parameters, columns in steps:
                running[:] = [name]
                self.connection.execute("DROP TABLE IF EXISTS %s" % _quoted(name))
                self.connection.sql(query, params=parameters or None).create(_checked(name))
                if columns is not None:
                    self._conform(name, columns)
                if self.connection.execute("SELECT count(*) FROM %s" % _quoted(name)).fetchone()[0] > self.max_rows:
                    raise SqlError("the query made more than %d rows from one page" % self.max_rows)
        try:
            self._timed(work, timeout)
        except SqlError as exc:
            raise SqlError("step %r: %s" % (running[0], exc) if running else str(exc))

    # the stream's tables ---------------------------------------------------------

    def create_empty(self, name, columns):
        """Creates an empty table with known columns (replacing a table of that name)."""
        self.drop_table(name)
        try:
            self.connection.execute("CREATE TABLE %s (%s)" % (
                _quoted(name), ", ".join("%s %s" % (_identifier(column), kind) for column, kind in columns)))
        except self.duckdb.Error as exc:
            raise SqlError(_message(exc))

    def create_raw(self, name):
        """The empty table of a request (COLUMNS), named after it."""
        self.create_empty(name, COLUMNS)

    def _insert(self, name, records, scope):
        """Adds one page of records to the request table `name`; scope: partition, config, window, today."""
        window = scope.get("window") or (None, None)
        self.connection.execute(
            "INSERT INTO %s SELECT value, $partition::JSON, $config::JSON, $start, $end, $today "
            "FROM json_each($records::JSON)" % _quoted(name),
            {"records": _json_text(records), "partition": _json_text(scope.get("partition") or {}),
             "config": _json_text(scope.get("config") or {}), "start": window[0], "end": window[1],
             "today": scope.get("today")})

    def add_raw(self, name, records, scope):
        """Adds one page of records to the request table `name` (see create_raw)."""
        self._timed(lambda: self._insert(name, records, scope))

    def drop_table(self, name):
        """Drops a table of the stream if it exists."""
        try:
            self.connection.execute("DROP TABLE IF EXISTS %s" % _quoted(name))
        except self.duckdb.Error as exc:
            raise SqlError(_message(exc))

    def delete_partition(self, name, partition):
        """Removes the rows that the request table `name` holds for a stream partition (see _partition_condition)."""
        condition, params = _partition_condition(partition)
        self._timed(lambda: self.connection.execute("DELETE FROM %s WHERE %s" % (_quoted(name), condition), params),
                    TRANSFORM_TIMEOUT)

    def key_problem(self, name, keys, timeout=TRANSFORM_TIMEOUT):
        """Why the rows of table `name` are not unique on `keys`, or None."""
        table, columns = _quoted(name), ", ".join(_quoted(key) for key in keys)

        def work():
            nulls = self.connection.execute("SELECT %s FROM %s" % (", ".join(
                "count(*) FILTER (WHERE %s IS NULL)" % _quoted(key) for key in keys), table)).fetchone()
            empty = ["%r in %d row(s)" % (key, count) for key, count in zip(keys, nulls) if count]
            if empty:
                return "primary_key column(s) are null: %s" % ", ".join(empty)
            repeated = self.connection.execute(
                "SELECT count(*) OVER () AS keys, %s, count(*) AS n FROM %s GROUP BY ALL HAVING count(*) > 1 "
                "ORDER BY n DESC, %s LIMIT 1" % (columns, table, columns)).fetchone()
            if repeated:
                example = to_json([_plain(value) for value in repeated[1:-1]], default=str)
                return ("primary_key (%s) is not unique: %d key(s) appear more than once, e.g. %s %d times (a join "
                        "may repeat rows)" % (", ".join(keys), repeated[0], example, repeated[-1]))
            return None
        return self._timed(work, timeout)

    def table_columns(self, name):
        """The (name, type) columns of table `name`."""
        try:
            return [(row[0], row[1]) for row in self.connection.execute("DESCRIBE %s" % _quoted(name)).fetchall()]
        except self.duckdb.Error as exc:
            raise SqlError(_message(exc))

    def table_records(self, name, columns, keys=()):
        """The rows of table `name`, with these columns, as plain records (read in batches, ordered by `keys`)."""
        names, kinds = [column for column, _ in columns], [kind for _, kind in columns]
        order = " ORDER BY %s" % ", ".join(_quoted(key) for key in keys) if keys else ""
        result = self.connection.execute("SELECT * FROM %s%s" % (_quoted(name), order))
        while True:
            batch = result.fetchmany(10000)
            if not batch:
                return
            for row in batch:
                yield dict((column, _decode(value, kind)) for column, value, kind in zip(names, row, kinds))

    def raw_rows(self, name):
        """
        (partition, record) of each row of the request table `name`, as data, in the order they were read. A
        generator: use it up before the next query.
        """
        try:
            result = self.connection.execute("SELECT partition, record FROM %s" % _quoted(name))
            while True:
                batch = result.fetchmany(10000)
                if not batch:
                    return
                for partition, record in batch:
                    yield json.loads(partition) if partition else {}, None if record is None else json.loads(record)
        except self.duckdb.Error as exc:
            raise SqlError(_message(exc))

    def distinct_rows(self, name, fields):
        """
        The distinct rows of these columns of table `name` without SQL nulls, sorted, as data: JSON values are
        decoded (per the columns' types), so a JSON text is its text, not a quoted one.
        """
        kinds = dict((column.lower(), kind) for column, kind in self.table_columns(name))
        columns = ", ".join(_quoted(field) for field in fields)
        where = " AND ".join("%s IS NOT NULL" % _quoted(field) for field in fields)
        types = [kinds.get(field.lower(), "") for field in fields]

        def work():
            rows = self.connection.execute("SELECT DISTINCT %s FROM %s WHERE %s ORDER BY %s" % (
                columns, _quoted(name), where, columns)).fetchall()
            return [tuple(_decode(value, kind) for value, kind in zip(row, types)) for row in rows]
        return self._timed(work, TRANSFORM_TIMEOUT)


# * ------------------------------------------------------------------------------------------
# * a source's plan: each stream's requests, steps and exports, checked before any request
# * ------------------------------------------------------------------------------------------

def _entries(value):
    """(position, item) of the mappings in a list (anything else has none: streamwright-validate reports it)."""
    if not isinstance(value, list):
        return []
    return [(position, item) for position, item in enumerate(value) if isinstance(item, dict)]


def _named(name):
    """Whether a stream, request, step or export name can name a table (streamwright-validate reports the others)."""
    return isinstance(name, str) and _NAME.match(name) is not None


def partition_fields(item):
    """(fields read, partition names) of a partition item: `field` and `name`, or `fields` (named after them)."""
    if isinstance(item.get("fields"), list):
        return tuple(item["fields"]), tuple(item["fields"])
    return (item.get("field"),), (item.get("name"),)


def exports_of(stream):
    """A stream's export names (those that can be names: streamwright-validate reports the others)."""
    export = stream.get("export") if isinstance(stream, dict) else None
    return [name for name in export if _named(name)] if isinstance(export, dict) else []


def incremental_exports(source):
    """
    The exports of the streams with `incremental`: a run writes a window of their data, so outputs append them when
    they have no primary_key (the others replace their tables).
    """
    return set(export for stream in source.get("streams") or [] if isinstance(stream, dict) and
               stream.get("incremental") for export in exports_of(stream))


class StreamPlan(object):
    """
    One stream of a SourcePlan:

    - mode: its `transform.mode`; requests / steps: (position, item) of its named requests and steps
    - queries / parameters / columns[step]: the step's query (its `$name` parameters typed), the config inputs it
      binds and its columns; conform[step]: the columns a run gives its table (when its parameters are plain casts);
      reads[step]: the tables it reads (lowercase)
    - order: ("request", item) and ("step", step), in the order a run runs them
    - exports: (export, spec, step) of its exports; parents: (position, item, stream) of its `from_stream` partitions
    """

    def __init__(self, name, index, stream):
        self.name, self.index, self.stream = name, index, stream
        transform = stream.get("transform") if isinstance(stream.get("transform"), dict) else {}
        self.mode = transform.get("mode")
        self.requests = [(position, item) for position, item in _entries(stream.get("requests"))
                         if _named(item.get("name"))]
        self.steps = [(position, step) for position, step in _entries(transform.get("steps"))
                      if _named(step.get("name"))]
        self.queries, self.parameters, self.columns, self.reads, self.conform = {}, {}, {}, {}, {}
        self.order, self.exports, self.parents = [], [], []

    def export_columns(self, export):
        """The columns of an export (of its step), or None if they are not known (its step has problems)."""
        for name, _, step in self.exports:
            if name == export:
                return self.columns.get(step)
        return None


class SourcePlan(object):
    """
    A source's streams, checked with DuckDB before any request (streamwright validate and streamwright run): each stream's steps
    compiled in list order against empty tables of its requests and earlier steps, with `$name` parameters bound to
    typed placeholders; request and step names that DuckDB has; export keys and `cursor_field`; the sources and
    fields of request partitions; the order of a run-mode stream's requests and steps; `from_stream` parents, their
    one export and its columns; and cycles within streams and across `from_stream`.

    - streams[name]: its StreamPlan; export_owner[export]: its stream; order: the stream names, parents first
    - types[name]: each config input's DuckDB type as a `$name` parameter (None: it cannot be one)
    - problems: (path in the source, message), with paths such as ("streams", 2, "transform", "steps", 0, "select"),
      so streamwright validate reports file and line
    """

    def __init__(self, sandbox, source):
        spec = source.get("spec") if isinstance(source.get("spec"), dict) else {}
        config = spec.get("config") if isinstance(spec.get("config"), dict) else {}
        self.types = dict((name, param_type(definition)) for name, definition in config.items())
        self.streams, self.names, self.export_owner, self.problems = {}, {}, {}, []
        for index, stream in enumerate(source.get("streams") or []):
            if isinstance(stream, dict) and _named(stream.get("name")) and stream["name"].lower() not in self.names:
                self.names[stream["name"].lower()] = stream["name"]  # names ignore case, as streamwright-validate's do
                self.streams[stream["name"]] = StreamPlan(stream["name"], index, stream)
        self._check_export_names()
        for plan in self.streams.values():
            self._compile(sandbox, plan)
        self._parents()
        self.order = self._order()

    def _problem(self, path, message):
        self.problems.append((path, message))

    # selection ---------------------------------------------------------------------

    def closure(self, names):
        """The streams that run for these stream or export names: them and their `from_stream` parents, in order."""
        needed = set()

        def visit(name):
            name = self.export_owner.get(name, name)
            if name in needed or name not in self.streams:
                return
            needed.add(name)
            for _, _, parent in self.streams[name].parents:
                visit(parent)
        for name in names:
            visit(name)
        return [name for name in self.order if name in needed]

    def messages(self, streams=None):
        """The problems as text, "stream 'x': transform.steps[0].select: ..." (only those of `streams`, if given)."""
        names = dict((plan.index, plan.name) for plan in self.streams.values() if streams is None or
                     plan.name in streams)
        found = []
        for path, message in self.problems:
            if path[1] in names:
                where = _path_text(path[2:])
                found.append("stream %r: %s%s" % (names[path[1]], where + ": " if where else "", message))
        return found

    # checks --------------------------------------------------------------------------

    def _check_export_names(self):
        """Every stream has exports; export names are unique in the source and are not another stream's name."""
        taken = {}
        for plan in self.streams.values():
            path = ("streams", plan.index, "export")
            if not isinstance(plan.stream.get("export"), dict) or not plan.stream["export"]:
                self._problem(path, "a stream needs at least one export: `export: {NAME: {step: STEP}}`")
                continue
            for name in exports_of(plan.stream):
                lower = name.lower()
                if lower in taken:
                    self._problem(path + (name,), "export %r is also an export of stream %r: export names are unique "
                                                  "in a source (names ignore case)" % (name, taken[lower]))
                elif lower in self.names and self.names[lower] != plan.name:
                    self._problem(path + (name,), "export %r has the name of stream %r: an export can have its own "
                                                  "stream's name, not another's" % (name, self.names[lower]))
                else:
                    taken[lower] = plan.name
                    self.export_owner[name] = plan.name

    def _compile(self, sandbox, plan):
        stream, path = plan.stream, ("streams", plan.index)
        if not plan.requests:  # (none, or none with a name: streamwright-validate reports those)
            self._problem(path + ("requests",), "a stream needs `requests`, a list of named requests: a stream reads "
                                                "only its own requests")
        if not isinstance(stream.get("transform"), dict):
            self._problem(path + ("transform",), "a stream needs `transform: {mode: page|run, steps: [...]}`")
            return
        if plan.mode not in MODES:
            self._problem(path + ("transform", "mode"), "`transform.mode` must be page or run")
        if not plan.steps:
            self._problem(path + ("transform", "steps"), "`transform.steps` must be a list of named steps")
        usable = self._check_names(sandbox, plan)
        self._compile_steps(sandbox, plan, usable)
        self._check_exports(plan)
        self._check_requests(plan)
        if plan.mode == "run":
            plan.order = self._run_order(plan)
        else:
            plan.order = [("request", item) for _, item in plan.requests[:1]] + [("step", step)
                                                                                 for _, step in plan.steps]

    def _check_names(self, sandbox, plan):
        """
        Request and step names are the stream's tables: unique (ignoring case) and not DuckDB's. Returns lowercase
        name -> "request" / "step" of the names that can be tables.
        """
        usable, seen = {}, set()
        for kind, entries, where in (("request", plan.requests, ("requests",)),
                                     ("step", plan.steps, ("transform", "steps"))):
            for position, item in entries:
                lower, path = item["name"].lower(), ("streams", plan.index) + where + (position, "name")
                if lower in seen:
                    self._problem(path, "%r is the name of another request or step of this stream: they are its "
                                        "tables (names ignore case)" % item["name"])
                elif lower in sandbox.relations:
                    self._problem(path, _BUILTIN % item["name"])
                else:
                    usable[lower] = kind
                seen.add(lower)
        return usable

    def _make(self, sandbox, name, columns, made, path, what):
        """An empty table for compiling, dropped when its stream is compiled; False (a problem) if DuckDB cannot."""
        try:
            sandbox.create_empty(name, columns)
        except SqlError as exc:
            self._problem(path, "DuckDB cannot make the table of %s: %s" % (what, exc))
            return False
        made.append(name)
        return True

    def _compile_steps(self, sandbox, plan, usable):
        """Each step compiled in list order, against empty tables of the requests and earlier steps it reads."""
        names = [item["name"] for _, item in plan.requests] + [step["name"] for _, step in plan.steps]
        positions = dict((step["name"].lower(), at) for at, (_, step) in enumerate(plan.steps))
        made, ready = [], set()  # the tables made for this stream; the lowercase names a step can read
        try:
            for position, item in plan.requests:
                lower = item["name"].lower()
                if usable.get(lower) == "request" and self._make(
                        sandbox, item["name"], COLUMNS, made, ("streams", plan.index, "requests", position, "name"),
                        "request %r" % item["name"]):
                    ready.add(lower)
            for at, (position, step) in enumerate(plan.steps):
                name, path = step["name"], ("streams", plan.index, "transform", "steps", position, "select")
                try:
                    query, reads, parameters = sandbox.parse(step.get("select"), names)
                except SqlError as exc:
                    self._problem(path, str(exc))
                    continue
                plan.reads[name] = reads
                if name.lower() in reads:
                    self._problem(path, "step %r reads itself: %s" % (name, _READS))
                    continue
                later = [plan.steps[positions[read]][1]["name"] for read in sorted(reads)
                         if positions.get(read, -1) > at]
                if later:
                    self._problem(path, "step %r reads later step(s) %s: %s" % (name, ", ".join(later), _READS))
                    continue
                bound = self._bind(sandbox, query, parameters, path)
                if bound is None:
                    continue
                plan.parameters[name] = sorted(set(parameters))
                if not all(read in ready for read in reads):
                    continue  # it reads a request or step with problems, reported where they are
                try:
                    query, columns, plain = self._describe(sandbox, bound, plan.parameters[name])
                except SqlError as exc:
                    self._problem(path, str(exc))
                    continue
                problem = _column_problem([column for column, _ in columns])
                if problem:  # every step's columns can be output fields
                    self._problem(path, problem)
                    continue
                unnamed = [column for column, kind in columns if _unnamed_struct(kind)]
                if unnamed:  # every step is a table
                    self._problem(path, "column(s) %s hold unnamed structs (row(...) or (a, b)), which a step's "
                                        "table cannot store: name their fields, {'a': ..., 'b': ...}" % ", ".join(
                                            unnamed))
                    continue
                plan.queries[name], plan.columns[name] = query, columns
                plan.conform[name] = columns if plain else None
                if usable.get(name.lower()) == "step" and self._make(sandbox, name, columns, made, path,
                                                                     "step %r" % name):
                    ready.add(name.lower())
        finally:
            for table in made:
                try:
                    sandbox.drop_table(table)
                except SqlError:
                    pass  # (the next stream's tables replace it)

    def _bind(self, sandbox, query, parameters, path):
        """
        The query with its `$name` parameters typed by their config inputs, as (scalar subqueries, plain casts,
        whether one is in the list of an ANY or ALL): see Sandbox.typed. None if a parameter is not a config input, or
        a list is compared as one value (problems reported).
        """
        problems = []
        for name in sorted(set(parameters)):
            if name not in self.types:
                problems.append("`$%s` is not a config input (declare it in spec.config)" % name)
            elif self.types[name] is None:
                problems.append("`$%s`: the config input's type cannot be a parameter" % name)
        lowered = {}
        for name in sorted(set(parameters)):
            lowered.setdefault(name.lower(), []).append(name)
        problems += ["%s are one parameter to DuckDB (names ignore case): use one of them" % " and ".join(
            "`$%s`" % name for name in same) for same in lowered.values() if len(same) > 1]
        if problems:
            self.problems += [(path, problem) for problem in problems]
            return None
        try:
            tree = sandbox.tree(query) if parameters else {}
            compared = _compared_lists(tree, self.types)
            if compared:
                self.problems += [(path, _LIST_COMPARED % {"name": name, "type": self.types[name]})
                                  for name in compared]
                return None
            return (sandbox.typed(query, parameters, self.types),
                    sandbox.typed(query, parameters, self.types, scalar=False), _quantified(tree))
        except SqlError as exc:
            self._problem(path, str(exc))
            return None

    def _describe(self, sandbox, bound, parameters):
        """
        (query, columns, plain) of a step: its parameters as scalar subqueries, compiled with null placeholders (the
        types are the same whatever the values); or, only where DuckDB takes no subquery (_NO_SUBQUERY, and the lists
        of ANY and ALL), as plain casts, compiled with sample values (or nulls, if DuckDB rejects those), whose table
        a run gives these columns. Any other error is the query's.
        """
        scalar, plain, quantified = bound
        nulls = dict((name, None) for name in parameters)
        try:
            return scalar, sandbox.describe(scalar, nulls), False
        except SqlError as exc:
            if not parameters or not (_NO_SUBQUERY.search(str(exc)) or quantified and _QUANTIFIED in str(exc)):
                raise
        try:
            return plain, sandbox.describe(plain, dict((name, _sample(self.types[name])) for name in parameters)), True
        except SqlError:
            return plain, sandbox.describe(plain, nulls), True

    def _check_exports(self, plan):
        """Each export writes a step of its stream; its keys are the step's columns, and `cursor_field` an export's."""
        stream, path = plan.stream, ("streams", plan.index, "export")
        steps = dict((step["name"].lower(), step["name"]) for _, step in plan.steps)
        export = stream.get("export") if isinstance(stream.get("export"), dict) else {}
        known = {}  # export -> its column names
        for name in exports_of(stream):
            spec = export[name]
            if not isinstance(spec, dict) or not isinstance(spec.get("step"), str):
                self._problem(path + (name,), "export %r needs `step`: the step it writes" % name)
                continue
            step = steps.get(spec["step"].lower())
            if step is None:
                self._problem(path + (name, "step"), "%r is not a step of this stream (steps: %s)" % (
                    spec["step"], ", ".join(step for _, step in sorted(steps.items())) or "none"))
                continue
            plan.exports.append((name, spec, step))
            columns = plan.columns.get(step)
            if columns is None:
                continue  # its step has problems, reported where they are
            names = known[name] = [column for column, _ in columns]
            keys = spec.get("primary_key")
            for position, key in enumerate(keys if isinstance(keys, list) else []):
                if key not in names:
                    self._problem(path + (name, "primary_key", position), "primary_key %r is not a column of step %r "
                                                                          "(columns: %s)" % (key, step,
                                                                                             ", ".join(names)))
        incremental = stream.get("incremental")
        cursor = incremental.get("cursor_field") if isinstance(incremental, dict) else None
        if cursor is not None and known and len(known) == len(plan.exports) and not any(
                cursor in names for names in known.values()):
            self._problem(("streams", plan.index, "incremental", "cursor_field"),
                          "cursor_field %r is not a column of an export (columns: %s)" % (cursor, "; ".join(
                              "%s: %s" % (export, ", ".join(names)) for export, names in known.items())))

    def _check_requests(self, plan):
        """
        Page mode reads one request without partitions. A request partition comes `from:` an earlier request (its
        fields are dotted paths in its records) or a step, whose columns its fields are.
        """
        stream, path = plan.stream, ("streams", plan.index, "requests")
        items = stream.get("requests") if isinstance(stream.get("requests"), list) else []
        if plan.mode == "page" and len(items) > 1:
            self._problem(path, "`transform.mode: page` reads one request: with %d requests, use `mode: run`" %
                          len(items))
        steps = dict((step["name"].lower(), step["name"]) for _, step in plan.steps)
        earlier = set()
        for position, item in plan.requests:
            parts = item.get("partitions")
            if parts and plan.mode == "page":
                self._problem(path + (position, "partitions"), "request partitions need `transform.mode: run`")
            for k, part in _entries(parts):
                source = part.get("from")
                if not isinstance(source, str) or source.lower() in earlier:
                    continue
                part_path = path + (position, "partitions", k)
                step = steps.get(source.lower())
                if step is None:
                    self._problem(part_path + ("from",), "%r is not an earlier request or a step of this stream" %
                                  source)
                    continue
                columns = plan.columns.get(step)
                if columns is None:
                    continue
                names = [column for column, _ in columns]
                several = isinstance(part.get("fields"), list)
                for f, field in enumerate(partition_fields(part)[0]):
                    if field not in names:
                        self._problem(part_path + (("fields", f) if several else ("field",)),
                                      "%r is not a column of step %r (columns: %s)" % (field, step, ", ".join(names)))
            earlier.add(item["name"].lower())

    def _run_order(self, plan):
        """
        The order a run-mode stream runs its requests and steps in: a request after the request or step its
        partitions come `from:`, a step after the requests and earlier steps it reads; requests first, then list
        order, break ties. A request partitioned from a step that needs the request first is a cycle (reported).
        """
        found = {}
        for kind, entries in (("request", plan.requests), ("step", plan.steps)):
            for position, item in entries:
                found.setdefault(item["name"].lower(), (kind, position, item))
        nodes = [node for node, (_, _, item) in found.items()]  # requests first, then steps, in list order
        positions = dict((step["name"].lower(), at) for at, (_, step) in enumerate(plan.steps))
        needs = {}
        for node in nodes:
            kind, _, item = found[node]
            if kind == "request":
                needs[node] = []
                for _, part in _entries(item.get("partitions")):
                    source = part["from"].lower() if isinstance(part.get("from"), str) else None
                    if source in found and source != node and source not in needs[node]:
                        needs[node].append(source)
            else:
                needs[node] = [read for read in sorted(plan.reads.get(item["name"], ())) if read in found and
                               read != node and (found[read][0] == "request" or
                                                 positions.get(read, -1) < positions[node])]
        order, done, pending = [], set(), list(nodes)
        while pending:
            ready = next((node for node in pending if all(need in done for need in needs[node])), None)
            if ready is None:
                break
            pending.remove(ready)
            done.add(ready)
            order.append((found[ready][0], found[ready][2]))
        cycles = []
        for start in pending:
            if found[start][0] != "request":
                continue
            trail = [start]  # every node left needs one that is left too: follow them to a cycle
            while True:
                following = next(need for need in needs[trail[-1]] if need in pending)
                if following in trail:
                    ring = trail[trail.index(following):]
                    break
                trail.append(following)
            if set(ring) in cycles:
                continue
            cycles.append(set(ring))
            first = next(at for at, node in enumerate(ring) if found[node][0] == "request")
            ring = ring[first:] + ring[:first]  # starts at a request: partitioned `from:` the next one
            _, position, item = found[ring[0]]
            source = ring[1] if len(ring) > 1 else ring[0]
            part = next((k for k, part in _entries(item.get("partitions"))
                         if isinstance(part.get("from"), str) and part["from"].lower() == source), 0)
            self._problem(("streams", plan.index, "requests", position, "partitions", part, "from"),
                          "requests and steps in a cycle: %s (a request partitioned `from:` a step runs after it, and "
                          "a step after the requests it reads)" % " -> ".join(
                              found[node][2]["name"] for node in ring + ring[:1]))
        return order

    def _parents(self):
        """`from_stream` parents: another stream of the source with exactly one export, whose columns the fields are."""
        for plan in self.streams.values():
            for k, item in _entries(plan.stream.get("partitions")):
                parent = item.get("from_stream")
                if not isinstance(parent, str):
                    continue
                path = ("streams", plan.index, "partitions", k)
                name = self.names.get(parent.lower())  # names ignore case, as streamwright-validate compares them
                if name is None:
                    self._problem(path + ("from_stream",), "%r is not a stream of this source" % parent)
                    continue
                if name == plan.name:
                    self._problem(path + ("from_stream",), "a stream cannot take its partitions from itself")
                    continue
                plan.parents.append((k, item, name))
                exports = exports_of(self.streams[name].stream)
                if len(exports) != 1:
                    self._problem(path + ("from_stream",), "`from_stream` takes values from the export of stream %r, "
                                                           "which has %d exports: it needs exactly one" % (
                                                               name, len(exports)))
                    continue
                columns = self.streams[name].export_columns(exports[0])
                if columns is None:
                    continue  # its step has problems, reported where they are
                names = [column for column, _ in columns]
                several = isinstance(item.get("fields"), list)
                for f, field in enumerate(partition_fields(item)[0]):
                    if field not in names:
                        self._problem(path + (("fields", f) if several else ("field",)),
                                      "%r is not a column of export %r of stream %r (columns: %s)" % (
                                          field, exports[0], name, ", ".join(names)))

    def _order(self):
        """The stream names, each after its `from_stream` parents (ties keep the source order); cycles reported."""
        ordered, visiting, visited = [], [], set()

        def visit(name):
            if name in visited:
                return
            visiting.append(name)
            plan = self.streams[name]
            for k, _, parent in plan.parents:
                if parent in visiting:
                    cycle = visiting[visiting.index(parent):] + [parent]
                    self._problem(("streams", plan.index, "partitions", k, "from_stream"),
                                  "streams in a cycle: %s (a stream runs after its `from_stream` parents)" %
                                  " -> ".join(cycle))
                    continue
                visit(parent)
            visiting.pop()
            visited.add(name)
            ordered.append(name)
        for name in self.streams:
            visit(name)
        return ordered


def plan_source(source):
    """A source's SourcePlan (its problems, and what runs for a selection, in which order), made in its own DuckDB."""
    sandbox = Sandbox()
    try:
        return SourcePlan(sandbox, source)
    finally:
        sandbox.close()


def check_source(source):
    """
    DuckDB checks of a source's streams before any request (SourcePlan): findings are (path, message) with paths
    into the source, such as ("streams", 2, "transform", "steps", 0, "select"), so streamwright validate reports file and
    line.
    """
    if not any(isinstance(stream, dict) for stream in source.get("streams") or []):
        return []
    return plan_source(source).problems
