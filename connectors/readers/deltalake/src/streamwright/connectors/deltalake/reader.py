# /*************************************************************************
# * Copyright 2026 Karthick Jaganathan
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
Reading Delta Lake tables as records using DuckDB's delta extension.
"""

import decimal
import json
import re
import shutil
import tempfile
import weakref

from streamwright.core.net.downloads import PAGE_SIZE
from streamwright.core.outputs.staging import sql_string

__all__ = ["QueryError", "ConnectError", "DeltaDatabase", "PAGE_SIZE", "check_query", "scan_query", "decode_record"]

PAGE_SIZE = 1000
WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "COPY", "CREATE", "DROP", "ALTER", "TRUNCATE")
_WRITE = re.compile(r"\b(%s)\b" % "|".join(WRITE_KEYWORDS), re.IGNORECASE)
OPERATORS = ("=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "IS NULL", "IS NOT NULL", "LIKE")
_NO_VALUE = ("IS NULL", "IS NOT NULL")


class QueryError(Exception):
    pass


class ConnectError(Exception):
    pass


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def decode_record(text):
    return None if text is None else json.loads(text, parse_float=decimal.Decimal)


def record_query(query):
    return f"SELECT to_json(t) AS record FROM (\n{query}\n) t"


def check_query(text, params=None):
    if not isinstance(text, str) or not text.strip():
        return text, ["`query` must be a SELECT statement (a non-empty text)"]
    if "{{" in text:
        return text, ["the query cannot hold references ({{ ... }}): pass values in `params`"]
    clean = re.sub(r"--[^\n]*", "", text)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL).strip()
    match = _WRITE.search(clean)
    if match:
        return text, [f"the query holds write keyword {match.group(1)!r}"]
    return text.strip().rstrip(";"), []


def scan_query(table_path, columns=None, where=None, limit=None):
    cols = ", ".join(quote_identifier(c) for c in columns) if columns else "*"
    path_str = sql_string(table_path)
    clauses = []
    params = {}
    for i, cond in enumerate(where or []):
        col_name = quote_identifier(cond["column"])
        op = cond.get("op", "=").upper()
        if op in _NO_VALUE:
            clauses.append(f"{col_name} {op}")
        else:
            param_key = f"p_{i}"
            params[param_key] = cond.get("value")
            clauses.append(f"{col_name} {op} ${param_key}")

    where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    query = f"SELECT {cols} FROM delta_scan({path_str}){where_clause}{limit_clause}"
    return query, params


def _cleanup(con, folder):
    try:
        con.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class DeltaDatabase(object):
    """
    DuckDB instance managing Delta table queries and scans.
    Completely decoupled from storage backends via StorageHandler.
    """

    def __init__(self, storage_handlers=None, page_size=PAGE_SIZE):
        import duckdb
        from streamwright.connectors.deltalake.storage import LocalStorageHandler

        self.page_size = page_size
        self.storage_handlers = list(storage_handlers) if storage_handlers is not None else [LocalStorageHandler()]
        self.folder = tempfile.mkdtemp(prefix="streamwright-delta-")
        try:
            config = {
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "temp_directory": self.folder,
            }
            self.connection = duckdb.connect(":memory:", config=config)
        except Exception:
            shutil.rmtree(self.folder, ignore_errors=True)
            raise

        self._finalizer = weakref.finalize(self, _cleanup, self.connection, self.folder)

        try:
            self.connection.execute("SET TimeZone = 'UTC'")
            # Load delta extension
            try:
                installed = self.connection.execute(
                    "SELECT installed FROM duckdb_extensions() WHERE extension_name = 'delta'"
                ).fetchone()
                if not (installed and installed[0]):
                    self.connection.execute("INSTALL delta")
                self.connection.execute("LOAD delta")
            except duckdb.Error as exc:
                raise ConnectError(f"cannot load DuckDB's delta extension: {exc}")

            # Configure each storage backend handler
            for handler in self.storage_handlers:
                handler.configure(self.connection)

        except Exception:
            self.close()
            raise

    def check_path(self, path):
        """Validates path against all configured storage handlers (e.g. allowed roots)."""
        for handler in self.storage_handlers:
            prob = handler.check_path(path)
            if prob:
                return prob
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._finalizer()

    @property
    def closed(self):
        return not self._finalizer.alive

    def pages(self, query, parameters=None, page_size=None):
        if self.closed:
            raise QueryError("the delta database connection is closed")
        statement, problems = check_query(query, parameters or {})
        if problems:
            raise QueryError("; ".join(problems))
        size = page_size or self.page_size
        result = self.connection.execute(record_query(statement), dict(parameters or {}))
        while True:
            rows = result.fetchmany(size)
            if not rows:
                return
            yield [decode_record(row[0]) for row in rows]
