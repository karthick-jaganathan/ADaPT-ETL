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

import os
import shutil
import tempfile
import pyarrow as pa
from deltalake import write_deltalake

import pytest
from streamwright.core.runtime.testing import MemoryOutput, page_stream
from streamwright.core.runtime.components import ConnectorError
from streamwright.core.engine.runner import SourceRunner
from streamwright.connectors.deltalake.connector import DeltaLakeConnector
from streamwright.connectors.deltalake.reader import DeltaDatabase, check_query, scan_query


def create_sample_delta_table(path):
    table = pa.table({
        "id": [1, 2, 3],
        "city": ["New York", "London", "Tokyo"],
        "population": [8300000, 8900000, 14000000]
    })
    write_deltalake(path, table)


def test_check_request():
    connector = DeltaLakeConnector()
    assert connector.check_request({"method": "invalid"}) != []
    assert connector.check_request({"method": "scan"}) != []  # missing table_path
    assert connector.check_request({"method": "scan", "arguments": {"table_path": "/path/to/table"}}) == []
    assert connector.check_request({"method": "query"}) != []  # missing query
    assert connector.check_request({"method": "query", "arguments": {"query": "SELECT * FROM delta_scan('t')"}}) == []


def test_query_validation():
    _, problems = check_query("DELETE FROM delta_scan('t')")
    assert any("write keyword 'DELETE'" in p for p in problems)

    _, problems = check_query("SELECT * FROM delta_scan('t') WHERE id = {{ config.id }}")
    assert any("cannot hold references" in p for p in problems)


def test_scan_query_builder():
    q, params = scan_query("/data/events", columns=["id", "city"], where=[{"column": "population", "op": ">", "value": 10000000}], limit=10)
    assert q == "SELECT \"id\", \"city\" FROM delta_scan('/data/events') WHERE \"population\" > $p_0 LIMIT 10"
    assert params == {"p_0": 10000000}


def test_delta_database_scan():
    with tempfile.TemporaryDirectory() as tmpdir:
        table_path = os.path.join(tmpdir, "sample_delta")
        create_sample_delta_table(table_path)

        db = DeltaDatabase()
        q, params = scan_query(table_path, columns=["id", "city"], where=[{"column": "id", "op": "=", "value": 2}])
        pages = list(db.pages(q, params))
        assert len(pages) == 1
        assert pages[0] == [{"id": 2, "city": "London"}]
        db.close()


def test_delta_sourcerunner_scan():
    with tempfile.TemporaryDirectory() as tmpdir:
        table_path = os.path.join(tmpdir, "sample_delta")
        create_sample_delta_table(table_path)

        auth = {"provider": "deltalake"}
        request = {
            "service": "tables",
            "method": "scan",
            "sdk": "deltalake",
            "arguments": {
                "table_path": table_path,
                "columns": ["id", "city", "population"],
                "limit": 2
            }
        }
        source = {
            "kind": "source",
            "name": "delta_demo",
            "auth": auth,
            "streams": [page_stream("cities", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 2
        assert output.records[0][1]["record"]["id"] == 1
        assert output.records[0][1]["record"]["city"] == "New York"


def test_delta_sourcerunner_query():
    with tempfile.TemporaryDirectory() as tmpdir:
        table_path = os.path.join(tmpdir, "sample_delta")
        create_sample_delta_table(table_path)

        auth = {"provider": "deltalake"}
        request = {
            "service": "tables",
            "method": "query",
            "sdk": "deltalake",
            "arguments": {
                "query": f"SELECT city, population FROM delta_scan('{table_path}') WHERE population > $min_pop",
                "params": {"min_pop": 10000000}
            }
        }
        source = {
            "kind": "source",
            "name": "delta_query_demo",
            "auth": auth,
            "streams": [page_stream("large_cities", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 1
        assert output.records[0][1]["record"] == {"city": "Tokyo", "population": 14000000}


def test_delta_sourcerunner_query_with_delta_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        table_path = os.path.join(tmpdir, "sample_delta")
        create_sample_delta_table(table_path)

        auth = {"provider": "deltalake"}
        request = {
            "service": "tables",
            "method": "query",
            "sdk": "deltalake",
            "arguments": {
                "table_uri": table_path,
                "query": "SELECT city, population FROM delta_table WHERE population > $min_pop",
                "params": {"min_pop": 10000000}
            }
        }
        source = {
            "kind": "source",
            "name": "delta_query_view_demo",
            "auth": auth,
            "streams": [page_stream("large_cities", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 1
        assert output.records[0][1]["record"] == {"city": "Tokyo", "population": 14000000}


def test_local_storage_handler_allowed_roots():
    from streamwright.connectors.deltalake.storage import LocalStorageHandler
    with tempfile.TemporaryDirectory() as tmpdir:
        allowed = os.path.join(tmpdir, "allowed_area")
        os.makedirs(allowed, exist_ok=True)
        handler = LocalStorageHandler(allowed_roots=[allowed])

        inside_path = os.path.join(allowed, "delta_tbl")
        outside_path = os.path.join(tmpdir, "forbidden_tbl")

        assert handler.check_path(inside_path) is None
        assert handler.check_path(outside_path) is not None
        assert "outside allowed roots" in handler.check_path(outside_path)
        # Cloud URIs are skipped by local path checker
        assert handler.check_path("s3://bucket/table") is None


def test_build_storage_handlers():
    from streamwright.connectors.deltalake.storage import (
        build_storage_handlers, S3StorageHandler, GCSStorageHandler, LocalStorageHandler
    )
    class DummyContext:
        def __init__(self):
            self.secrets = []
        def secret(self, val):
            self.secrets.append(val)

    ctx = DummyContext()
    auth = {
        "s3": {"key_id": "my_key", "secret": "my_secret", "region": "us-west-2"},
        "gcs": {"key_file": "/path/to/key.json", "project_id": "proj_1"},
        "local": {"roots": ["/data/delta"]}
    }
    handlers = build_storage_handlers(auth, ctx)
    assert any(isinstance(h, S3StorageHandler) for h in handlers)
    assert any(isinstance(h, GCSStorageHandler) for h in handlers)
    assert any(isinstance(h, LocalStorageHandler) for h in handlers)
    assert "my_key" in ctx.secrets
    assert "my_secret" in ctx.secrets
    assert "/path/to/key.json" in ctx.secrets
