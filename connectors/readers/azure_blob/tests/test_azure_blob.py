"""
The azure_blob connector: URL roots and containment, the `?` query refusal, azure extension and the secret,
credentials and redaction, formats, pages, globs, `match`, on_missing, and runs through SourceRunner.

Offline: Readers get a Recorder connection that records DuckDB's azure and secret statements instead of running them,
lists URLs from Storage.objects (as DuckDB's object-storage glob does) and reads them from local files, and forces
errors; the rest runs on a real DuckDB connection.
"""

import datetime
import decimal
import fnmatch
import json
import logging
import os
import re
import socket
import threading
import time

import pytest
import yaml

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("streamwright.connectors.azure_blob.connector")

from streamwright.connectors.azure_blob import reader  # noqa: E402
from streamwright.connectors.azure_blob.connector import AzureBlobConnector  # noqa: E402
from streamwright.connectors.azure_blob.reader import (AccessError, Reader, allowed_roots, match_files,  # noqa: E402
                                                     resolve_files, secret_query, file_query, url_problem,
                                                     path_problem)
from streamwright.core import cli  # noqa: E402
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.logs import RunMetrics  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

CONN_STR = "DefaultEndpointsProtocol=https;AccountName=testacc;AccountKey=dGVzdGtleQ==;EndpointSuffix=core.windows.net;"
AUTH = {"connection_string": CONN_STR}
ROOTS = ["azure://acme-data/in/", "azure://lake/raw/"]
BIG = "12345678901234567890.123456"
_READS = ("read_csv", "read_json", "read_parquet", "glob(")


# * ----------------------------------------
# * object storage, offline: the Recorder
# * ----------------------------------------

class Result(object):
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows


def glob_match(url, pattern):
    """Whether DuckDB's object-storage glob `pattern` matches `url`: `*` and `[ab]` within a folder, `**` any."""
    def match(keys, patterns):
        if not patterns:
            return not keys
        if patterns[0] == "**":
            return any(match(keys[index:], patterns[1:]) for index in range(len(keys) + 1))
        return bool(keys) and fnmatch.fnmatchcase(keys[0], patterns[0]) and match(keys[1:], patterns[1:])
    return match(url.split("/"), pattern.split("/"))


class Storage(object):
    """What Recorder connections see: whether azure extension is installed, objects (URL -> local file) and forced errors."""

    def __init__(self, connect):
        self.connect = connect
        self.installed = True
        self.objects = {}
        self.errors = {}
        self.statements = []
        self.listed = []
        self._plain = None

    def plain(self):
        if self._plain is None:
            self._plain = self.connect()
            self._plain.execute("SET TimeZone = 'UTC'")
        return self._plain

    def sql(self):
        return [statement for statement, _ in self.statements]


class Recorder(object):
    """DuckDB stand-in connection: records statements, reads local files for azure:// URLs."""

    def __init__(self, real, storage):
        self.real = real
        self.storage = storage

    def _fail(self, kind):
        error = self.storage.errors.get(kind)
        if error is not None:
            raise error

    def execute(self, query, parameters=None):
        sql = " ".join(query.split())
        self.storage.statements.append((sql, parameters))
        if "duckdb_extensions()" in sql and "'azure'" in sql:
            return Result([(self.storage.installed,)])
        for start, kind in (("INSTALL azure", "INSTALL"), ("LOAD azure", "LOAD"),
                            ("CREATE OR REPLACE TEMPORARY SECRET", "SECRET")):
            if sql.startswith(start):
                self._fail(kind)
                return Result([])
        if "FROM glob(?)" in sql:
            self._fail("GLOB")
            self.storage.listed.append(parameters[0])
            return Result([(url,) for url in sorted(self.storage.objects) if glob_match(url, parameters[0])])
        first = parameters[0] if parameters else None
        if isinstance(first, list) and first and reader.is_url(first[0]):
            self._fail("READ")
            if first[0] not in self.storage.objects:
                raise duckdb.HTTPException("HTTP 404: %s" % first[0])
            return self.storage.plain().execute(query, [[self.storage.objects[first[0]]]] + list(parameters[1:]))
        return self.real.execute(query) if parameters is None else self.real.execute(query, parameters)

    def close(self):
        self.real.close()


@pytest.fixture
def storage(monkeypatch):
    state = Storage(duckdb.connect)

    def connect(*args, **kwargs):
        connection = state.connect(*args, **kwargs)
        reader_config = "streamwright-azure_blob-" in str((kwargs.get("config") or {}).get("temp_directory"))
        return Recorder(connection, state) if reader_config else connection
    monkeypatch.setattr(duckdb, "connect", connect)
    yield state
    if state._plain is not None:
        state._plain.close()


@pytest.fixture
def local_files(tmp_path):
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("id,name,amount\n1,Alice,10.50\n2,Bob,20.00\n", encoding="utf-8")
    jsonl_file = tmp_path / "events.jsonl"
    jsonl_file.write_text('{"event_id": 101, "kind": "click"}\n{"event_id": 102, "kind": "view"}\n', encoding="utf-8")
    return {"csv": str(csv_file), "jsonl": str(jsonl_file)}


# * -----------------
# * URL & Path Tests
# * -----------------

def test_allowed_roots():
    assert allowed_roots(["azure://container/in/"]) == ("azure://container/in/",)
    assert allowed_roots(["azure://container/in"]) == ("azure://container/in/",)
    assert allowed_roots(["azure://c1/p/", "azure://c1/p/"]) == ("azure://c1/p/",)

    with pytest.raises(AccessError):
        allowed_roots([])
    with pytest.raises(AccessError):
        allowed_roots(["s3://bucket/in/"])
    with pytest.raises(AccessError):
        allowed_roots(["azure://container/in/?query=1"])


def test_url_problem():
    assert url_problem("azure://mycontainer/path/file.csv") is None
    assert url_problem("abfss://mycontainer/path/file.csv") is None
    assert "scheme" in url_problem("s3://bucket/file.csv")
    assert "has a `?`" in url_problem("azure://c/file.csv?param=1")
    assert "has a `%`" in url_problem("azure://c/file%20name.csv")
    assert "goes up a folder" in url_problem("azure://c/path/../file.csv")
    assert "credentials" in url_problem("azure://user@c/file.csv")
    assert "port" in url_problem("azure://c:1234/file.csv")


def test_path_problem():
    assert path_problem("orders/*.csv") is None
    assert path_problem("azure://mycontainer/orders/*.csv") is None
    assert "empty" in path_problem("")
    assert "absolute" in path_problem("/local/path/file.csv")
    assert "scheme" in path_problem("https://example.com/file.csv")


def test_secret_query():
    # Connection string
    sql, params = secret_query({"connection_string": "Endpoint=test;"}, {}, ["azure://c/"])
    assert "CONNECTION_STRING ?" in sql
    assert params[0] == "Endpoint=test;"
    assert params[1] == ["azure://c/"]

    # Account name + account key
    sql, params = secret_query({"account_key": "mykey"}, {"account_name": "myacc", "use_ssl": True}, ["azure://c/"])
    assert "CONNECTION_STRING ?" in sql
    assert "AccountName=myacc" in params[0]
    assert "AccountKey=mykey" in params[0]

    # Service principal
    sql, params = secret_query({"client_secret": "mysecret"},
                               {"account_name": "myacc", "tenant_id": "tid", "client_id": "cid"},
                               ["azure://c/"])
    assert "PROVIDER service_principal" in sql
    assert "CLIENT_SECRET ?" in sql


# * ----------------------
# * Auth & Request Checks
# * ----------------------

def test_check_auth():
    connector = AzureBlobConnector()

    # Valid connection string
    assert not connector.check_auth({
        "roots": ["azure://c/in/"],
        "connection_string": "{{ secrets.azure_conn }}"
    })

    # Valid account key
    assert not connector.check_auth({
        "roots": ["azure://c/in/"],
        "account_name": "myacc",
        "account_key": "{{ secrets.azure_key }}"
    })

    # Missing roots
    assert any("roots" in p for p in connector.check_auth({
        "connection_string": "{{ secrets.azure_conn }}"
    }))

    # Config reference rejected for credential
    assert any("secret reference" in p for p in connector.check_auth({
        "roots": ["azure://c/in/"],
        "connection_string": "{{ config.azure_conn }}"
    }))

    # Incomplete credentials rejected
    assert any("needs credentials" in p for p in connector.check_auth({
        "roots": ["azure://c/in/"]
    }))


def test_check_request():
    connector = AzureBlobConnector()

    assert not connector.check_request({
        "service": "object",
        "method": "read",
        "arguments": {"path": "orders/*.csv", "format": "csv"}
    })

    # Unknown service / method
    assert any("not supported" in p for p in connector.check_request({
        "service": "invalid",
        "method": "read",
        "arguments": {"path": "orders/*.csv"}
    }))

    # Missing path argument
    assert any("needs `path`" in p for p in connector.check_request({
        "service": "object",
        "method": "read",
        "arguments": {}
    }))


# * -----------------------------
# * Offline Reader & Operations
# * -----------------------------

def test_offline_read_csv(storage, local_files):
    storage.objects["azure://acme-data/in/data.csv"] = local_files["csv"]
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})

    pages = list(reader_obj.pages("azure://acme-data/in/data.csv", "csv", {"header": True}))
    assert len(pages) == 1
    assert len(pages[0]) == 2
    assert pages[0][0]["name"] == "Alice"
    assert pages[0][1]["name"] == "Bob"


def test_offline_read_jsonl(storage, local_files):
    storage.objects["azure://acme-data/in/events.jsonl"] = local_files["jsonl"]
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})

    pages = list(reader_obj.pages("azure://acme-data/in/events.jsonl", "jsonl"))
    assert len(pages) == 1
    assert len(pages[0]) == 2
    assert pages[0][0]["event_id"] == 101
    assert pages[0][1]["event_id"] == 102


def test_offline_resolve_glob(storage, local_files):
    storage.objects["azure://acme-data/in/file1.csv"] = local_files["csv"]
    storage.objects["azure://acme-data/in/file2.csv"] = local_files["csv"]
    storage.objects["azure://acme-data/in/file3.txt"] = local_files["csv"]

    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})
    resolved = reader_obj.resolve("file*.csv")
    assert len(resolved) == 2
    assert resolved[0][0] == "azure://acme-data/in/file1.csv"
    assert resolved[1][0] == "azure://acme-data/in/file2.csv"


def test_offline_match_regex(storage, local_files):
    storage.objects["azure://acme-data/in/data_2026_01.csv"] = local_files["csv"]
    storage.objects["azure://acme-data/in/data_2026_02.csv"] = local_files["csv"]
    storage.objects["azure://acme-data/in/other.csv"] = local_files["csv"]

    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})
    matched = reader_obj.match("azure://acme-data/in/", r"data_\d{4}_\d{2}\.csv")
    assert len(matched) == 2


def test_credentials_redacted(storage):
    secret_val = "SECRET_CONN_STR_KEY_VAL_12345"
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": secret_val})
    masked = reader_obj.mask("Failed to connect with SECRET_CONN_STR_KEY_VAL_12345 error")
    assert secret_val not in masked
    assert "***" in masked


# * -----------------------------
# * SourceRunner Integration Test
# * -----------------------------

def test_e2e_source_runner(storage, local_files):
    storage.objects["azure://acme-data/in/orders/orders.csv"] = local_files["csv"]

    stream = page_stream(
        "orders",
        {"name": "raw_orders", "sdk": "azure_blob", "service": "object", "method": "read",
         "arguments": {"path": "orders/*.csv", "format": "csv", "options": {"header": True}}},
        select="SELECT (record->>'id')::INT AS id, record->>'name' AS name FROM raw_orders"
    )

    source = {
        "kind": "source",
        "name": "azure_test_source",
        "auth": {
            "provider": "azure_blob",
            "roots": ["azure://acme-data/in/"],
            "connection_string": "{{ secrets.azure_conn }}"
        },
        "streams": [stream]
    }

    output = MemoryOutput()
    runner = SourceRunner(source, {},
                          secrets={"azure_conn": CONN_STR},
                          output=output,
                          allowed_connectors={"azure_blob"})
    summary = runner.run()
    assert summary.get("records") == 2 or summary.get("rows") == 2 or summary.get("row_count") == 2 or len(output.records) == 2
    orders_records = [r for s, r in output.records if s == "orders"]
    assert len(orders_records) == 2
    assert orders_records[0]["name"] == "Alice"
    assert orders_records[1]["name"] == "Bob"


def test_offline_read_tsv(storage, tmp_path):
    tsv_file = tmp_path / "data.tsv"
    tsv_file.write_text("id\tval\n10\tx\n20\ty\n", encoding="utf-8")
    storage.objects["azure://acme-data/in/data.tsv"] = str(tsv_file)
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})

    pages = list(reader_obj.pages("azure://acme-data/in/data.tsv", "tsv", {"header": True}))
    assert len(pages) == 1
    assert len(pages[0]) == 2
    assert pages[0][0]["val"] == "x"


def test_offline_read_filename_option(storage, local_files):
    storage.objects["azure://acme-data/in/data.csv"] = local_files["csv"]
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})

    pages = list(reader_obj.pages("azure://acme-data/in/data.csv", "csv", {"header": True, "filename": True}, filename="custom_name.csv"))
    assert pages[0][0]["filename"] == "custom_name.csv"


def test_outside_root_raises_access_denied(storage):
    reader_obj = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})
    with pytest.raises(AccessError):
        reader_obj.check("azure://other-container/in/data.csv")


def test_missing_file_handling(storage):
    connector = AzureBlobConnector()
    client = Reader(["azure://acme-data/in/"], credentials={"connection_string": CONN_STR})

    class DummyContext(object):
        where = {}
        log = logging.getLogger("test")
        def redact(self, text):
            return text
        def call(self, fn):
            return fn()

    ctx = DummyContext()

    # on_missing error
    req_error = {
        "service": "object",
        "method": "read",
        "arguments": {
            "path": "nonexistent.csv",
            "format": "csv",
            "on_missing": "error"
        }
    }
    with pytest.raises(ConnectorError) as exc_info:
        list(connector.request(client, req_error, ctx))
    assert exc_info.value.code == "NOT_FOUND"

    # on_missing skip
    req_skip = {
        "service": "object",
        "method": "read",
        "arguments": {
            "path": "nonexistent.csv",
            "format": "csv",
            "on_missing": "skip"
        }
    }
    records = list(connector.request(client, req_skip, ctx))
    assert records == []


