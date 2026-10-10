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
Reading objects in Azure Blob Storage as records, on a DuckDB connection of the `azure_blob` connector's own
(never the run's transform sandbox), through DuckDB's azure extension.

- allowed_roots(): the URL prefixes objects can be read from (the auth block's `roots`), each `azure://container/prefix/`.
- resolve_files() / match_files(): the objects a rendered `path` names - a URL (or a key relative to the first root),
  a glob (`*`, `[ab]`, `**`), or a folder and a `match` regex - checked BEFORE anything is read: the URL, and every
  object a listing returns, must be inside a root (the same scheme, the same container and the root's key prefix), and
  must not hold anything DuckDB would read as more than an object's key (AccessError otherwise):
  - no `?`: DuckDB reads a URL's query parameters as connection settings that override configured ones; `?` is therefore
    never a glob character here (only `*` and `[` are);
  - no `%` (no percent-encoded `?`, `.`, `/` or `\\`), no `#`, no backslash, no control character;
  - no `..`, `.` or empty (`//`) folder; no user@ and no port in the container.
- Reader: a DuckDB connection that can only read inside the roots (DuckDB's allowed_directories, external access off,
  configuration locked), with azure loaded (installed on demand) and the auth block's credentials and settings set
  as a temporary DuckDB secret scoped to the roots, as bound parameters (never in a path or a query's text). Every
  read and listing re-checks its URL. Reader.pages() yields the rows of one object as lists of records (fetchmany:
  memory stays bounded however large the object is): csv/tsv (read_csv), json/jsonl (read_json_objects: each object
  exactly as written) or parquet (read_parquet), each row one JSON object (`to_json(t)`).

Records are JSON: decimals stay exact (decimal.Decimal), dates and timestamps are text, and csv/tsv values are the
file's text (all_varchar, empty cells null) unless the `columns` option types them: steps cast, e.g.
`(record->>'amount')::DECIMAL(12,2)`.
"""

import decimal
import json
import re
import shutil
import tempfile
import weakref
from urllib.parse import quote, quote_plus

from streamwright.core.net.downloads import PAGE_SIZE
from streamwright.core.outputs.staging import sql_string


__all__ = ["AccessError", "ReadError", "MissingError", "Reader", "PAGE_SIZE", "LIMITS", "FORMATS", "OPTIONS",
           "COMPRESSIONS", "PROVIDER", "SCHEME", "SCHEMES", "SECRET_TYPE", "CREDENTIALS", "SETTINGS",
           "GLOB_CHARACTERS", "format_of", "option_problems", "regex_problem", "path_problem", "url_problem",
           "setting_problem", "is_url", "is_glob", "url_root", "inside", "allowed_roots", "resolve_files",
           "match_files", "file_query", "secret_query", "decode_record"]

PROVIDER = "azure_blob"  # the connector (and its auth block's provider)
SCHEME = "azure"  # the primary URL scheme: azure://container/key
SCHEMES = ("azure", "abfss")  # supported Azure schemes
SECRET_TYPE = "azure"  # DuckDB's secret type
SECRET_NAME = "streamwright_azure_blob"
# Credentials: `{{ secrets.* }}` references only, masked in every message.
CREDENTIALS = {"connection_string": "CONNECTION_STRING", "account_key": "ACCOUNT_KEY", "client_secret": "CLIENT_SECRET"}
# Settings: literal values or references (not credentials).
SETTINGS = {"account_name": "ACCOUNT_NAME", "endpoint": "ENDPOINT", "tenant_id": "TENANT_ID",
            "client_id": "CLIENT_ID", "use_ssl": "USE_SSL"}

LIMITS = {"memory_limit": "1GB", "threads": 1}
FORMATS = ("csv", "tsv", "json", "jsonl", "parquet")
COMPRESSIONS = ("auto", "none", "gzip", "zstd")
# Not `?`: in an object storage URL DuckDB reads it as the start of a query (connection settings).
GLOB_CHARACTERS = "*["
# file name endings -> format (a compression ending first is ignored: a.csv.gz is csv)
_EXTENSIONS = {".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
               ".parquet": "parquet", ".pq": "parquet"}
_COMPRESSED = (".gz", ".gzip", ".zst", ".zstd")
# DuckDB's names for read_json_objects' compression (read_csv takes ours as they are)
_JSON_COMPRESSIONS = {"auto": "auto_detect", "none": "uncompressed", "gzip": "gzip", "zstd": "zstd"}
_CSV_NAMES = {"delimiter": "delim"}  # option -> read_csv's parameter
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\([0-9 ,]+\))?(\[\])?$")  # a column type, e.g. DECIMAL(12,2)
_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_CONTAINER = re.compile(r"^[a-z0-9][a-z0-9._\-]*$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENDPOINT = re.compile(r"^[A-Za-z0-9._\-]+(:[0-9]+)?$")
# DuckDB's messages for a URL that names no object
_NOT_FOUND = re.compile(r"\bHTTP 404\b|No files found that match the pattern|\bBlobNotFound\b|\bResourceNotFound\b|\bNoSuchKey\b", re.IGNORECASE)


class AccessError(Exception):
    """A read the allowed roots do not allow, refused before anything is read."""


class ReadError(Exception):
    """An object that cannot be read: its format, options or contents; a request that cannot be served."""


class MissingError(ReadError):
    """A URL that names no object (object storage is only asked when the object is read)."""


# * --------------------
# * formats and options
# * --------------------

def _boolean(value):
    return None if isinstance(value, bool) else "expected true or false"


def _text(value):
    return None if isinstance(value, str) and value != "" else "expected a non-empty text"


def _count(value):
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return None if ok else "expected a non-negative integer"


def _columns(value):
    """Why a `columns` option is wrong: must be a {name: DuckDB type} mapping, types checked with _TYPE."""
    if not isinstance(value, dict) or not value:
        return "expected a mapping of column names to SQL types, e.g. {id: BIGINT, name: VARCHAR}"
    for name, kind in value.items():
        if not isinstance(name, str) or not name.strip():
            return "column names must be non-empty texts"
        if not isinstance(kind, str) or not _TYPE.match(kind.strip()):
            return "column %r: %r is not a recognized SQL type" % (name, kind)
    return None


def _one_of(*allowed):
    def check(value):
        return None if value in allowed else "expected one of: %s" % ", ".join(map(str, allowed))
    return check


_CSV_OPTIONS = {
    "header": _boolean, "delim": _text, "delimiter": _text, "quote": _text, "escape": _text,
    "skip": _count, "nullstr": _text, "compression": _one_of(*COMPRESSIONS), "columns": _columns,
    "filename": _boolean,
}
_JSONL_OPTIONS = {"compression": _one_of(*COMPRESSIONS), "filename": _boolean}
OPTIONS = {"csv": _CSV_OPTIONS, "tsv": _CSV_OPTIONS,
           "json": dict(_JSONL_OPTIONS, format=_one_of("auto", "array", "newline_delimited")),
           "jsonl": _JSONL_OPTIONS, "parquet": {"filename": _boolean}}


def format_of(path):
    """An object's format from its name (a.csv, a.jsonl.gz), or None."""
    name = str(path).rsplit("/", 1)[-1].lower()
    for ending in _COMPRESSED:
        if name.endswith(ending):
            name = name[:-len(ending)]
            break
    dot = name.rfind(".")
    return _EXTENSIONS.get(name[dot:]) if dot > 0 else None


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


def setting_problem(key, value):
    """Why a literal (or rendered) setting of the auth block is wrong."""
    if key == "use_ssl":
        ok = isinstance(value, bool) or isinstance(value, str) and value.strip().lower() in ("true", "false")
        return None if ok else "expected true or false"
    if not isinstance(value, str) or not value.strip():
        return "expected a non-empty text"
    if key == "endpoint" and not _ENDPOINT.match(value) and not _URL.match(value):
        return "expected a host and an optional port, or a valid URL, e.g. 127.0.0.1:10000"
    return None


# * -----
# * URLs
# * -----

def is_url(path):
    """Whether a path is a URL (scheme://...)."""
    return isinstance(path, str) and bool(_URL.match(path))


def is_glob(text):
    """Whether a path or key is a glob: `*` (`**` for any folders) or `[ab]` - never `?` (see GLOB_CHARACTERS)."""
    return any(character in text for character in GLOB_CHARACTERS)


def _parts(url):
    """(scheme in lower case, container, key: the rest after the container's `/`) of a URL."""
    scheme = _URL.match(url).group(1)
    container, _, key = url[len(scheme) + 3:].partition("/")
    return scheme.lower(), container, key


def _text_problem(text):
    """
    Why a URL or key holds more than an object's name, or None: a `?` (a query: DuckDB reads its parameters as
    connection settings), a `%` (percent-encoding could hide one, or a dot, slash or backslash),
    a fragment, a backslash or a control character.
    """
    if "?" in text:
        return ("has a `?`: object storage URLs take no query - DuckDB would read its parameters as connection "
                "settings; `?` is not a glob character here")
    if "%" in text:
        return "has a `%`: percent-encoded characters (e.g. %3f, %2e, %2f, %5c) are refused"
    if "#" in text:
        return "has a fragment (#)"
    if "\\" in text:
        return "has a backslash"
    if _CONTROL.search(text):
        return "has a control character"
    return None


def _key_problem(key):
    """Why the key part of a URL (or a key relative to a root) cannot be read, or None: `..`, `.` or `//` in it."""
    segments = key.split("/")
    if ".." in segments:
        return "goes up a folder (..)"
    if "." in segments:
        return "has a `.` folder"
    if "" in segments[:-1]:
        return "has an empty folder (//)"
    return None


def url_problem(url, root=False):
    """
    Why a (rendered or not) URL cannot be read - or, `root`, be a root - or None: another scheme than azure:// or abfss://,
    no container, credentials (user@) or a port in the container, a container name that is not valid, a `?`, `%`, `#`,
    backslash or control character anywhere, a `..`, `.` or empty folder; a root that is a glob.
    """
    if not is_url(url):
        return "is not an %s:// URL" % SCHEME
    scheme, container, key = _parts(url)
    if scheme not in SCHEMES:
        return "uses the %s:// scheme: the %s connector reads %s URLs only" % (
            scheme, PROVIDER, " or ".join(s + "://" for s in SCHEMES))
    problem = _text_problem(url)
    if problem:
        return problem
    if "@" in container:
        return "has credentials in it (user@container): set them in the auth block, from secrets"
    if not container:
        return "names no container"
    if "{{" not in container:
        if ":" in container:
            return "has a port: name the container only (an endpoint is set in the auth block)"
        if not _CONTAINER.match(container):
            return "has a container name that is not valid: %r" % container
    problem = _key_problem(key)
    if problem:
        return problem
    if root and is_glob(key):
        return "is a glob: a root is a folder (a URL prefix)"
    return None


def path_problem(path):
    """
    Why a (rendered or not) `path` cannot be read, or None: empty, a URL url_problem() refuses, or a key relative to
    the first root that is absolute, another scheme's URL, or holds what url_problem() refuses in a key.
    """
    if not isinstance(path, str) or not path.strip():
        return "is empty"
    if is_url(path):
        return url_problem(path)
    if "://" in path:
        return "is not an %s:// URL" % SCHEME
    if path.startswith("/"):
        return "is an absolute local path: name an %s:// URL, or a key relative to the first root" % SCHEME
    return _text_problem(path) or _key_problem(path)


def _normal(url):
    """A URL with its scheme in lower case."""
    scheme, container, key = _parts(url)
    return "%s://%s/%s" % (scheme, container, key)


def url_root(root):
    """A URL root as a prefix ending with `/`; AccessError for one that cannot be."""
    problem = url_problem(root, root=True)
    if problem:
        raise AccessError("roots: %r %s" % (root, problem))
    prefix = _normal(root)
    return prefix if prefix.endswith("/") else prefix + "/"


def inside(root, url):
    """
    Whether a URL is inside a root (url_root(): `azure://container/prefix/`): the same scheme and container, its key
    under the root's prefix.
    """
    return is_url(url) and _normal(url).startswith(root)


def allowed_roots(roots):
    """The URL prefixes objects can be read from, as url_root() gives them (duplicates once); AccessError otherwise."""
    allowed = []
    for root in roots or ():
        if not isinstance(root, str) or not root.strip():
            raise AccessError("roots must be %s:// URL prefixes (non-empty texts)" % SCHEME)
        prefix = url_root(root)
        if prefix not in allowed:
            allowed.append(prefix)
    if not allowed:
        raise AccessError("the connector needs at least one root prefix to read from")
    return tuple(allowed)


def _root_of(url, roots):
    """The root that allows `url`, or None."""
    for root in roots:
        if inside(root, url):
            return root
    return None


def _outside(url, roots):
    problem = url_problem(url)
    if problem:
        return AccessError("%r %s" % (url, problem))
    return AccessError("%s is outside the roots objects can be read from (%s)" % (url, ", ".join(roots)))


def _url(path, roots, folder=False):
    """A rendered path as a URL (the first root's prefix prepended when it has no scheme); AccessError otherwise."""
    problem = path_problem(path)
    if problem:
        raise AccessError("path %r %s" % (path, problem))
    if not is_url(path):
        path = roots[0] + path
    url = _normal(path)
    if folder and not url.endswith("/"):
        url += "/"
    root = _root_of(url, roots)
    if root is None:
        raise _outside(url, roots)
    return url


def _listed(path, found_urls, roots):
    """[(URL, root)] of the objects a listing returned, checked against roots (AccessError otherwise)."""
    files = []
    for found in found_urls:
        problem = url_problem(found)
        if problem:
            raise AccessError("path %r: the listed object %r %s" % (path, found, problem))
        found = _normal(found)
        root = _root_of(found, roots)
        if root is None:
            raise AccessError("path %r: the listed object %s is outside the roots objects can be read from (%s)" % (
                path, found, ", ".join(roots)))
        if not found.endswith("/") and (found, root) not in files:
            files.append((found, root))
    return files


def resolve_files(path, roots, lister=None):
    """
    [(URL, root)] of the objects a rendered path names, in name order: the URL itself (whether it names an object is
    known when it is read: MissingError), or each object a glob (`*`, `[ab]`, `**` for any folders) matches, which
    lister(url) lists; none when nothing matches. A key without a scheme is relative to the first root. Checked
    before anything is read, raising AccessError: the URL and every object the listing returns (url_problem(), and
    inside a root). A URL that names a folder is a ReadError.
    """
    url = _url(path, roots)
    if not is_glob(_parts(url)[2]):
        if url.endswith("/"):
            raise ReadError("path %r is a folder: name its objects, e.g. %s, or set `match`" % (path, path + "*.csv"))
        return [(url, _root_of(url, roots))]
    if lister is None:
        raise ReadError("path %r is a glob: listing %s needs the reader" % (path, url))
    return _listed(path, lister(url), roots)


def match_files(path, roots, pattern, recursive=False, lister=None):
    """
    [(URL, root)] of the objects under the folder a rendered `path` names (a URL or a key relative to the first root;
    a trailing `/` is optional) whose key relative to that folder (`/` between its sub-folders) fully matches the
    regex `pattern` (re.fullmatch), in the order of those relative keys; with `recursive`, the objects of its
    sub-folders too. lister(glob) lists them (`folder/*`, or `folder/**`). Checked before anything is read, raising
    AccessError: the folder and every object the listing returns (url_problem(), and inside a root). A `path` that is
    a glob, or a pattern that is not a valid regex, is a ReadError.
    """
    folder = _url(path, roots, folder=True)
    if is_glob(_parts(folder)[2]):
        raise ReadError("path %r is a glob: with `match`, `path` is the folder to look in" % path)
    problem = regex_problem(pattern)
    if problem:
        raise ReadError("`match` %r %s" % (pattern, problem))
    regex = re.compile(pattern)
    if lister is None:
        raise ReadError("path %r: listing %s needs the reader" % (path, folder))
    files = []
    for found, root in _listed(path, lister(folder + ("**" if recursive else "*")), roots):
        if not found.startswith(folder):
            continue
        relative = found[len(folder):]
        if not recursive and "/" in relative:
            continue
        if regex.fullmatch(relative):
            files.append((found, root))
    return files


# * ----------------
# * reading objects
# * ----------------

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


def file_query(url, file_format, options=None):
    """
    (query, parameters) that read one object of a format (not auto) with its options (without `filename`, which the
    Reader adds): each row a JSON text, a record. The URL is a bound parameter.
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
        return "SELECT to_json(t) FROM read_csv(?%s) t" % names, [[url]] + parameters
    if file_format in ("json", "jsonl"):
        values = {"format": "newline_delimited" if file_format == "jsonl" else options.pop("format", "auto")}
        for name, value in options.items():
            values[name] = _JSON_COMPRESSIONS[value] if name == "compression" else value
        names, parameters = _arguments(values)
        return "SELECT json FROM read_json_objects(?%s)" % names, [[url]] + parameters
    if file_format == "parquet":
        names, parameters = _arguments(dict({"hive_partitioning": False}, **options))
        return "SELECT to_json(t) FROM read_parquet(?%s) t" % names, [[url]] + parameters
    raise ReadError("unknown file format %r (formats: %s)" % (file_format, ", ".join(FORMATS)))


def _flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ReadError("use_ssl: expected true or false")


def secret_query(credentials, settings, scopes):
    """
    (query, parameters) that set rendered credentials and settings as a temporary DuckDB secret scoped to the URL
    roots `scopes`; every value is a bound parameter, never in the query's text.
    """
    unknown = sorted(str(key) for key in list(credentials) + list(settings)
                     if key not in CREDENTIALS and key not in SETTINGS)
    if unknown:
        raise ReadError("%s credentials do not take %s (keys: %s)" % (PROVIDER, ", ".join(unknown), ", ".join(
            list(CREDENTIALS) + list(SETTINGS))))

    conn_str = credentials.get("connection_string")
    if not conn_str and credentials.get("account_key") and settings.get("account_name"):
        use_ssl = _flag(settings.get("use_ssl", True))
        proto = "https" if use_ssl else "http"
        acc = settings["account_name"]
        key = credentials["account_key"]
        endpoint = settings.get("endpoint")
        if endpoint:
            conn_str = "DefaultEndpointsProtocol=%s;AccountName=%s;AccountKey=%s;BlobEndpoint=%s;" % (proto, acc, key, endpoint)
        else:
            conn_str = "DefaultEndpointsProtocol=%s;AccountName=%s;AccountKey=%s;EndpointSuffix=core.windows.net;" % (proto, acc, key)

    if conn_str:
        parts = ["TYPE %s" % SECRET_TYPE, "CONNECTION_STRING ?", "SCOPE ?"]
        parameters = [conn_str, list(scopes)]
        return "CREATE OR REPLACE TEMPORARY SECRET %s (%s)" % (SECRET_NAME, ", ".join(parts)), parameters

    if credentials.get("client_secret"):
        parts = ["TYPE %s" % SECRET_TYPE, "PROVIDER service_principal"]
        parameters = []
        for key, param_name in [("tenant_id", "TENANT_ID"), ("client_id", "CLIENT_ID"),
                                ("client_secret", "CLIENT_SECRET"), ("account_name", "ACCOUNT_NAME"),
                                ("endpoint", "ENDPOINT")]:
            val = credentials.get(key) if key in credentials else settings.get(key)
            if val is not None:
                parts.append("%s ?" % param_name)
                parameters.append(str(val))
        parts.append("SCOPE ?")
        parameters.append(list(scopes))
        return "CREATE OR REPLACE TEMPORARY SECRET %s (%s)" % (SECRET_NAME, ", ".join(parts)), parameters

    # Fallback with config provider
    parts = ["TYPE %s" % SECRET_TYPE]
    parameters = []
    if settings.get("account_name"):
        parts.append("ACCOUNT_NAME ?")
        parameters.append(str(settings["account_name"]))
    if settings.get("endpoint"):
        parts.append("ENDPOINT ?")
        parameters.append(str(settings["endpoint"]))
    parts.append("SCOPE ?")
    parameters.append(list(scopes))
    return "CREATE OR REPLACE TEMPORARY SECRET %s (%s)" % (SECRET_NAME, ", ".join(parts)), parameters


def masked_forms(values):
    """The texts to mask for credentials: each value as written, and URL-encoded (4 characters or more)."""
    forms = set()
    for value in values:
        if value is None or str(value) == "":
            continue
        text = str(value)
        forms.update(form for form in (text, text.strip(), quote(text, safe=""), quote_plus(text)) if len(form) >= 4)
    return sorted(forms, key=lambda form: (-len(form), form))


def load_azure(connection):
    """Loads DuckDB's azure extension, installing it first (once, from DuckDB's repository) when it is missing."""
    installed = connection.execute("SELECT installed FROM duckdb_extensions() WHERE extension_name = 'azure'"
                                   ).fetchone()
    if not (installed and installed[0]):
        connection.execute("INSTALL azure")
    connection.execute("LOAD azure")


def _cleanup(connection, folder):
    try:
        connection.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class Reader(object):
    """
    A DuckDB connection of the connector's own, in memory, spilling to a private temporary folder (removed by close(),
    or when the Reader is collected). It loads azure, sets `credentials` and `settings` (rendered values of the auth
    block) as a temporary secret scoped to `roots` (url_root() prefixes), and reads only inside them: DuckDB checks
    every access against them too (allowed_directories, external access off, configuration locked). Its messages
    mask the credentials.
    """

    def __init__(self, roots, credentials=None, settings=None, limits=None, page_size=PAGE_SIZE):
        import duckdb
        self.duckdb = duckdb
        self.roots = list(roots)
        self.page_size = page_size
        self.credentials = dict((key, value) for key, value in (credentials or {}).items() if value is not None)
        self.settings = dict((key, value) for key, value in (settings or {}).items() if value is not None)
        self.masked = masked_forms(self.credentials.values())
        self.folder = tempfile.mkdtemp(prefix="streamwright-%s-" % PROVIDER)
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
            self._object_storage()
            self.connection.execute("SET allowed_directories = [%s]" % ", ".join(
                sql_string(root) for root in self.roots))
            self.connection.execute("SET enable_external_access = false")
            self.connection.execute("SET lock_configuration = true")
        except Exception:
            self.close()
            raise

    def _object_storage(self):
        """azure extension, and the secret scoped to the roots."""
        try:
            load_azure(self.connection)
        except self.duckdb.Error as exc:
            raise ReadError("cannot load DuckDB's azure extension, which reads %s: %s" % (
                ", ".join(self.roots), self.mask(_message(exc))))
        query, parameters = secret_query(self.credentials, self.settings, self.roots)
        try:
            self.connection.execute(query, parameters)
        except self.duckdb.Error as exc:
            raise ReadError("cannot set the %s credentials: %s" % (PROVIDER, self.mask(_message(exc))))

    def mask(self, text):
        """`text` with every credential masked as ***."""
        text = str(text)
        for value in self.masked:
            text = text.replace(value, "***")
        return text

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._finalizer()

    @property
    def closed(self):
        return not self._finalizer.alive

    def check(self, url):
        """AccessError unless `url` (a URL or a glob over one) is one url_problem() accepts, inside a root."""
        problem = url_problem(url) if isinstance(url, str) else "is not an %s:// URL" % SCHEME
        if problem:
            raise AccessError("%r %s" % (url, problem))
        if _root_of(url, self.roots) is None:
            raise _outside(url, self.roots)

    def resolve(self, path):
        """[(URL, root)] of the objects a rendered path names (resolve_files); globs are listed by DuckDB."""
        return resolve_files(path, self.roots, lister=self._list)

    def match(self, path, pattern, recursive=False):
        """[(URL, root)] of the objects under a folder whose relative key matches a regex (match_files)."""
        return match_files(path, self.roots, pattern, recursive, lister=self._list)

    def _list(self, url):
        """The objects a glob over object storage matches (none when nothing does)."""
        if self.closed:
            raise ReadError("the reader is closed")
        self.check(url)
        try:
            return [row[0] for row in self.connection.execute("SELECT file FROM glob(?)", [url]).fetchall()]
        except self.duckdb.Error as exc:
            message = self.mask(_message(exc))
            if _NOT_FOUND.search(message):
                return []
            raise ReadError("cannot list %s: %s" % (url, message))

    def pages(self, url, file_format, options=None, page_size=None, filename=None):
        """
        The rows of one object (a URL, see resolve) as lists of at most page_size records; `filename`: the text the
        `filename` option adds to each record. The URL is checked again first (AccessError). DuckDB errors are
        ReadErrors (credentials masked); a URL that names no object is a MissingError.
        """
        if self.closed:
            raise ReadError("the reader is closed")
        self.check(url)
        if is_glob(_parts(url)[2]):
            raise AccessError("%r is a glob: read the objects resolve() lists" % url)
        problems = option_problems(file_format, options)
        if problems:
            raise ReadError("%s: %s" % (url, "; ".join(problems)))
        query, parameters = file_query(url, file_format, options)
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
                            record["filename"] = filename if filename is not None else url
                yield page
        except self.duckdb.Error as exc:
            message = self.mask(_message(exc))
            if _NOT_FOUND.search(message):
                raise MissingError("no object at %s: %s" % (url, message))
            raise ReadError("cannot read %s as %s: %s" % (url, file_format, message))
