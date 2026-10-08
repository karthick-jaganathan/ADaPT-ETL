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
Reading local files as records on a DuckDB connection of the `files` connector's own (never the run's transform
sandbox). Local files only: object storage is read by the s3 and gcs connectors.

- allowed_roots(): the folders files can be read from (the auth block's `roots`), each as its real path; a URL is
  refused.
- resolve_files(): the files a rendered `path` names (a path or a glob), checked BEFORE anything is read: no `..`, and
  every file inside a root once symbolic links are followed, with no glob character (*, ?, [) in its real path (DuckDB
  would read it as a glob, not by name) (AccessError otherwise). Relative paths are relative to the first root.
- match_files(): the files under a folder whose path relative to it fully matches a regex (the request's `match`),
  in its sub-folders too with `recursive`, checked the same way.
- Reader: a DuckDB connection that can only read inside the roots (DuckDB's allowed_directories, external access off,
  configuration locked); it loads no extension. Reader.pages() yields the rows of one file as lists of records
  (fetchmany: memory stays bounded however large the file is): csv/tsv (read_csv), json/jsonl (read_json_objects:
  each object exactly as written) or parquet (read_parquet), each row one JSON object (`to_json(t)`).

Records are JSON: decimals stay exact (decimal.Decimal), dates and timestamps are text, and csv/tsv values are the
file's text (all_varchar, empty cells null) unless the `columns` option types them: steps cast, e.g.
`(record->>'amount')::DECIMAL(12,2)`.
"""

import decimal
import glob
import json
import os
import re
import shutil
import tempfile
import weakref

from streamwright.core.net.downloads import PAGE_SIZE
from streamwright.core.outputs.staging import sql_string


__all__ = ["AccessError", "ReadError", "Reader", "PAGE_SIZE", "LIMITS", "FORMATS", "OPTIONS", "COMPRESSIONS",
           "format_of", "option_problems", "path_problem", "regex_problem", "is_url", "is_glob", "allowed_roots",
           "inside", "resolve_files", "match_files", "file_query", "decode_record"]

LIMITS = {"memory_limit": "1GB", "threads": 1}
FORMATS = ("csv", "tsv", "json", "jsonl", "parquet")
COMPRESSIONS = ("auto", "none", "gzip", "zstd")
GLOB_CHARACTERS = "*?["
OBJECT_STORAGE = "the files connector reads local files only (object storage: the s3 and gcs connectors)"
# file name endings -> format (a compression ending first is ignored: a.csv.gz is csv)
_EXTENSIONS = {".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
               ".parquet": "parquet", ".pq": "parquet"}
_COMPRESSED = (".gz", ".gzip", ".zst", ".zstd")
# DuckDB's names for read_json_objects' compression (read_csv takes ours as they are)
_JSON_COMPRESSIONS = {"auto": "auto_detect", "none": "uncompressed", "gzip": "gzip", "zstd": "zstd"}
_CSV_NAMES = {"delimiter": "delim"}  # option -> read_csv's parameter
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9 ,]+\))?(\[\])?$")  # a column type, e.g. DECIMAL(12,2)
_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")


class AccessError(Exception):
    """A read the allowed roots do not allow, refused before anything is read."""


class ReadError(Exception):
    """A file that cannot be read: its format, options or contents."""


# * --------------------
# * formats and options
# * --------------------

def _boolean(value):
    return None if isinstance(value, bool) else "expected true or false"


def _text(value):
    return None if isinstance(value, str) and value != "" else "expected a non-empty text"


def _count(value):
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return None if ok else "expected a whole number >= 0"


def _one_of(*choices):
    def check(value):
        return None if value in choices else "expected one of: %s" % ", ".join(choices)
    return check


def _texts(value):
    if isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(item, str) for item in value)):
        return None
    return "expected a text or a list of texts"


def _columns(value):
    if not isinstance(value, dict) or not value:
        return "expected a mapping of column names to DuckDB types, e.g. {id: BIGINT, amount: DECIMAL(12,2)}"
    wrong = [str(name) for name, kind in value.items() if not isinstance(kind, str) or not _TYPE.match(kind.strip())]
    return "not a column type: %s" % ", ".join(wrong) if wrong else None


_CSV_OPTIONS = {"header": _boolean, "delimiter": _text, "quote": _text, "escape": _text, "columns": _columns,
                "compression": _one_of(*COMPRESSIONS), "null_padding": _boolean, "ignore_errors": _boolean,
                "skip": _count, "nullstr": _texts, "all_varchar": _boolean, "dateformat": _text,
                "timestampformat": _text, "filename": _boolean}
_JSONL_OPTIONS = {"compression": _one_of(*COMPRESSIONS), "ignore_errors": _boolean, "filename": _boolean}
# format -> option -> check(value): a message, or None when the value is fine
OPTIONS = {"csv": _CSV_OPTIONS, "tsv": _CSV_OPTIONS,
           "json": dict(_JSONL_OPTIONS, format=_one_of("auto", "array", "newline_delimited")),
           "jsonl": _JSONL_OPTIONS, "parquet": {"filename": _boolean}}


def format_of(path):
    """A file's format from its name (a.csv, a.jsonl.gz), or None."""
    name = os.path.basename(str(path)).lower()
    for ending in _COMPRESSED:
        if name.endswith(ending):
            name = name[:-len(ending)]
            break
    return _EXTENSIONS.get(os.path.splitext(name)[1])


def option_problems(file_format, options):
    """
    Problems with the options of a format ("auto": options some format takes), as messages; values must be literal
    (not references).
    """
    if options is None:
        return []
    if not isinstance(options, dict):
        return ["`options` must be a mapping of options"]
    known = OPTIONS.get(file_format)
    problems = []
    for name, value in options.items():
        checks = [known.get(name)] if known is not None else [table[name] for table in OPTIONS.values()
                                                               if name in table]
        if not any(checks):
            names = sorted(known) if known is not None else sorted(set(n for table in OPTIONS.values() for n in table))
            problems.append("%s files have no option %r (options: %s)" % (
                file_format if known is not None else "these", name, ", ".join(names)))
            continue
        if isinstance(value, str) and "{{" in value or isinstance(value, dict) and any(
                isinstance(item, str) and "{{" in item for item in value.values()):
            problems.append("option %r: options take literal values, not references" % name)
            continue
        messages = [check(value) for check in checks]
        if all(messages):
            problems.append("option %r: %s" % (name, messages[0]))
    return problems


def is_url(path):
    """Whether a path is a URL (scheme://...), which this connector does not read."""
    return isinstance(path, str) and bool(_URL.match(path))


def is_glob(path):
    """Whether a path has glob characters (*, ?, [)."""
    return any(character in path for character in GLOB_CHARACTERS)


def path_problem(path):
    """Why a (rendered or not) file path cannot be read, or None: empty, a URL, or going up a folder (..)."""
    if not isinstance(path, str) or not path.strip():
        return "is empty"
    if "\x00" in path:
        return "has a NUL character"
    if is_url(path):
        return "is a URL: %s" % OBJECT_STORAGE
    if ".." in re.split(r"[/\\]", path):
        return "goes up a folder (..)"
    return None


def regex_problem(pattern):
    """Why a `match` regex cannot be used, or None: not a non-empty text, a reference, or not a valid regex."""
    if not isinstance(pattern, str) or not pattern:
        return "must be a non-empty text (a regex)"
    if "{{" in pattern:
        return "is a literal regex: it takes no references"
    try:
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as exc:
        return "is not a valid regex: %s" % exc
    return None


# * -----------------------------------------
# * which files: allowed roots, globs, regexes
# * -----------------------------------------

def allowed_roots(directories):
    """
    The folders files can be read from, each as its real path (duplicates once); AccessError for one that is not a
    local folder (a URL included).
    """
    roots = []
    for directory in directories or ():
        if not isinstance(directory, str) or not directory.strip():
            raise AccessError("roots: %r is not a folder" % (directory,))
        if is_url(directory):
            raise AccessError("roots: %r is a URL: roots are local folders; %s" % (directory, OBJECT_STORAGE))
        if not os.path.isdir(directory):
            raise AccessError("roots: %s: no such folder" % directory)
        root = os.path.realpath(directory)
        if root not in roots:
            roots.append(root)
    if not roots:
        raise AccessError("roots: no folder to read files from")
    return roots


def inside(root, path):
    """Whether `path` (a real, absolute path) is the folder `root` or inside it."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _fixed_part(path):
    """The part of a path before its first glob character, up to the last folder separator before it."""
    if not is_glob(path):
        return path
    first = min(path.index(character) for character in GLOB_CHARACTERS if character in path)
    return os.path.dirname(path[:first]) or (os.sep if path.startswith(os.sep) else ".")


def _root_of(path, roots):
    return next((root for root in roots if inside(root, path)), None)


def _outside(path, roots):
    return AccessError("path %r is outside the roots files can be read from (%s)" % (path, ", ".join(roots)))


def _glob_problem(real):
    """Why a file's real path cannot be read by name, or None: DuckDB reads a path with *, ? or [ as a glob."""
    if is_glob(real):
        return "has a glob character (*, ? or [) in its path: it cannot be read by name"
    return None


def _checked(path, match, roots, files):
    """
    Adds the file `match` (found for `path`) to `files` as (real path, root) once its real path is in a root;
    AccessError for a file whose real path has a glob character (DuckDB would read other files in its place).
    """
    real = os.path.realpath(match)
    root = _root_of(real, roots)
    if root is None:
        raise AccessError("path %r: %s leads outside the roots files can be read from (%s) through a symbolic "
                          "link" % (path, match, ", ".join(roots)))
    if os.path.isfile(real):  # (a broken link is no file)
        problem = _glob_problem(real)
        if problem:
            raise AccessError("path %r: the file %s %s" % (path, real, problem))
        if (real, root) not in files:
            files.append((real, root))
    return real


def resolve_files(path, roots):
    """
    [(real path, root)] of the files a rendered path names, in name order: the file, or each file a glob (*, ?, [ab],
    ** for any folders) matches; none when nothing matches. Relative paths are relative to the first root. Checked
    before anything is read, raising AccessError: no `..`, the path's fixed part inside a root and, once symbolic
    links are followed, every file too, with no glob character in its real path. A path that names a folder is a
    ReadError.
    """
    problem = path_problem(path)
    if problem:
        raise AccessError("path %r %s" % (path, problem))
    base = roots[0]
    full = path if os.path.isabs(path) else os.path.join(base, path)
    if _root_of(os.path.realpath(_fixed_part(full)), roots) is None:
        raise _outside(path, roots)
    pattern = is_glob(path)
    if pattern:
        written = path if os.path.isabs(path) else os.path.join(glob.escape(base), path)
        matches = sorted(glob.glob(written, recursive=True))
    else:
        matches = [full] if os.path.lexists(full) else []
    files = []
    for match in matches:
        real = _checked(path, match, roots, files)
        if os.path.isdir(real) and not pattern:
            raise ReadError("path %r is a folder: name its files, e.g. %s, or set `match`" % (
                path, os.path.join(path, "*.csv")))
    return files


def _walk_error(exc):
    raise ReadError("cannot list %s: %s" % (exc.filename, exc.strerror or exc))


def match_files(path, roots, pattern, recursive=False):
    """
    [(real path, root)] of the files in the folder a rendered `path` names whose path relative to that folder (its
    sub-folders separated by `/`) fully matches the regex `pattern` (re.fullmatch), in the order of those relative
    paths; with `recursive`, the files of its sub-folders too (symbolic links to folders are not followed). None when
    the folder does not exist or nothing matches. Relative paths are relative to the first root. Checked before
    anything is read, raising AccessError: no `..`, the folder inside a root and, once symbolic links are followed,
    every matching file too, with no glob character in its real path. A `path` that is a glob or a file, or a pattern
    that is not a valid regex, is a ReadError.
    """
    problem = path_problem(path)
    if problem:
        raise AccessError("path %r %s" % (path, problem))
    if is_glob(path):
        raise ReadError("path %r is a glob: with `match`, `path` is the folder to look in" % path)
    problem = regex_problem(pattern)
    if problem:
        raise ReadError("`match` %r %s" % (pattern, problem))
    regex = re.compile(pattern)
    full = path if os.path.isabs(path) else os.path.join(roots[0], path)
    folder = os.path.realpath(full)
    if _root_of(folder, roots) is None:
        raise _outside(path, roots)
    if not os.path.exists(folder):
        return []
    if not os.path.isdir(folder):
        raise ReadError("path %r is not a folder: with `match`, `path` is the folder to look in" % path)
    matches = []
    for current, folders, names in os.walk(folder, onerror=_walk_error):
        if not recursive:
            folders[:] = []
        for name in names:
            relative = os.path.relpath(os.path.join(current, name), folder).replace(os.sep, "/")
            if regex.fullmatch(relative):
                matches.append((relative, os.path.join(current, name)))
    files = []
    for _, match in sorted(matches):
        _checked(path, match, roots, files)
    return files


# * --------------
# * reading files
# * --------------

def decode_record(text):
    """A JSON text as a record, its numbers with fractions exact (decimal.Decimal)."""
    return None if text is None else json.loads(text, parse_float=decimal.Decimal)


def _message(exc):
    """DuckDB's message on one line, without its query excerpt."""
    text = str(exc).strip().split("\n\n")[0]
    return " ".join(line.strip() for line in text.splitlines() if line.strip()) or type(exc).__name__


def _arguments(values):
    """(SQL of named arguments `, name=?`, parameters) of a table function."""
    return "".join(", %s=?" % name for name in values), list(values.values())


def file_query(path, file_format, options=None):
    """
    (query, parameters) that read one file of a format (not auto) with its options (without `filename`, which the
    Reader adds): each row a JSON text, a record.
    """
    options = dict((name, value) for name, value in (options or {}).items() if name != "filename")
    if file_format in ("csv", "tsv"):
        values = {}
        if file_format == "tsv":
            values["delim"] = "\t"
        values["all_varchar"] = "columns" not in options
        values["hive_partitioning"] = False
        columns = options.pop("columns", None)
        for name, value in options.items():
            values[_CSV_NAMES.get(name, name)] = value
        names, parameters = _arguments(values)
        if columns:  # (a constant STRUCT, which DuckDB takes as no parameter; types are checked by option_problems)
            names += ", columns={%s}" % ", ".join("%s: %s" % (sql_string(name), sql_string(kind.strip()))
                                                  for name, kind in columns.items())
        return "SELECT to_json(t) FROM read_csv(?%s) t" % names, [[path]] + parameters
    if file_format in ("json", "jsonl"):
        values = {"format": "newline_delimited" if file_format == "jsonl" else options.pop("format", "auto")}
        for name, value in options.items():
            values[name] = _JSON_COMPRESSIONS[value] if name == "compression" else value
        names, parameters = _arguments(values)
        return "SELECT json FROM read_json_objects(?%s)" % names, [[path]] + parameters
    if file_format == "parquet":
        names, parameters = _arguments(dict({"hive_partitioning": False}, **options))
        return "SELECT to_json(t) FROM read_parquet(?%s) t" % names, [[path]] + parameters
    raise ReadError("unknown file format %r (formats: %s)" % (file_format, ", ".join(FORMATS)))


def _cleanup(connection, folder):
    try:
        connection.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Reader(object):
    """
    A DuckDB connection of the connector's own, in memory, spilling to a private temporary folder (removed by close(),
    or when the Reader is collected). It reads only inside `roots` (local folders, as real paths; DuckDB checks every
    file access, and refuses symbolic links that leave them), with external access off and its configuration locked;
    it loads no extension (no httpfs: it reads no URL).
    """

    def __init__(self, roots, limits=None, page_size=PAGE_SIZE):
        import duckdb
        self.duckdb = duckdb
        self.roots = list(roots)
        wrong = [root for root in self.roots if not isinstance(root, str) or is_url(root) or not os.path.isabs(root)]
        if wrong or not self.roots:
            raise AccessError("roots: %s: roots are local folders, as absolute paths (allowed_roots); %s" % (
                ", ".join(repr(root) for root in wrong) or "none", OBJECT_STORAGE))
        self.page_size = page_size
        self.folder = tempfile.mkdtemp(prefix="streamwright-files-")
        try:
            config = dict(limits or LIMITS, autoinstall_known_extensions=False, autoload_known_extensions=False,
                          python_enable_replacements=False, temp_directory=self.folder)
            self.connection = duckdb.connect(":memory:", config=config)
        except Exception:
            shutil.rmtree(self.folder, ignore_errors=True)
            raise
        self._finalizer = weakref.finalize(self, _cleanup, self.connection, self.folder)
        try:
            self.connection.execute("SET TimeZone = 'UTC'")
            # (DuckDB takes allowed_directories only while external access is still on - its default - before it is
            # turned off for good)
            self.connection.execute("SET allowed_directories = [%s]" % ", ".join(
                sql_string(root.rstrip(os.sep) + os.sep) for root in self.roots))
            self.connection.execute("SET enable_external_access = false")
            self.connection.execute("SET lock_configuration = true")
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._finalizer()

    @property
    def closed(self):
        return not self._finalizer.alive

    def resolve(self, path, match=None, recursive=False):
        """
        [(real path, root)] of the files a rendered path names (resolve_files) or, with `match`, the files of the
        folder `path` whose relative paths match it (match_files), inside this Reader's roots.
        """
        if match is not None:
            return match_files(path, self.roots, match, recursive=recursive)
        return resolve_files(path, self.roots)

    def pages(self, path, file_format, options=None, page_size=None, filename=None):
        """
        The rows of one file (a real path, see resolve) as lists of at most page_size records; `filename`: the text
        the `filename` option adds to each record. DuckDB errors are ReadErrors; a path with a glob character is an
        AccessError.
        """
        if self.closed:
            raise ReadError("the reader is closed")
        problem = _glob_problem(path)
        if problem:
            raise AccessError("%s %s" % (path, problem))
        problems = option_problems(file_format, options)
        if problems:
            raise ReadError("%s: %s" % (path, "; ".join(problems)))
        query, parameters = file_query(path, file_format, options)
        add_name = bool((options or {}).get("filename"))
        size = page_size or self.page_size
        try:
            result = self.connection.execute(query, parameters)
            while True:
                rows = result.fetchmany(size)
                if not rows:
                    return
                page = [decode_record(row[0]) for row in rows]
                if add_name:
                    for record in page:
                        if isinstance(record, dict):
                            record["filename"] = filename if filename is not None else path
                yield page
        except self.duckdb.Error as exc:
            raise ReadError("cannot read %s as %s: %s" % (path, file_format, _message(exc)))
