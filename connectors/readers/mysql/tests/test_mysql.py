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
import tempfile
import duckdb
import pytest
from streamwright.core.runtime.testing import MemoryOutput, page_stream
from streamwright.core.runtime.components import ConnectorError
from streamwright.core.engine.runner import SourceRunner
from streamwright.connectors.mysql.connector import MySQLConnector
from streamwright.connectors.mysql.reader import check_query, table_query, Database


def create_standin_db(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE customers (id BIGINT, name VARCHAR, email VARCHAR, status VARCHAR)")
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", [
        (1, "Alice", "alice@example.com", "active"),
        (2, "Bob", "bob@example.com", "inactive"),
        (3, "Charlie", "charlie@example.com", "active")
    ])
    con.close()


def test_missing_auth():
    connector = MySQLConnector()
    class DummyContext:
        def secret(self, val):
            pass
    with pytest.raises(ConnectorError, match="auth requires either `dsn` or `host`"):
        connector.connect({}, DummyContext())


def test_query_validation():
    _, problems = check_query("INSERT INTO customers VALUES (1, 'Eve', 'eve@example.com', 'active')")
    assert any("write keyword 'INSERT'" in p for p in problems)

    _, problems = check_query("DROP TABLE customers")
    assert any("write keyword 'DROP'" in p for p in problems)

    _, problems = check_query("SELECT * FROM customers WHERE id = {{ config.id }}")
    assert any("cannot hold references" in p for p in problems)

    _, problems = check_query("SELECT * FROM customers WHERE status = $status", {"status": "active"})
    assert problems == []


def test_table_query_builder():
    q, params = table_query(None, "customers", columns=["id", "name"], where=[{"column": "status", "op": "=", "value": "active"}])
    assert q == 'SELECT "id", "name" FROM "customers" WHERE "status" = $p_0'
    assert params == {"p_0": "active"}


def test_standin_query_execution():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.duckdb")
        create_standin_db(db_path)
        db = Database(db_path, attach_type="duckdb")
        pages = list(db.pages("SELECT id, name, status FROM customers WHERE status = $status ORDER BY id", {"status": "active"}))
        assert len(pages) == 1
        records = pages[0]
        assert len(records) == 2
        assert records[0] == {"id": 1, "name": "Alice", "status": "active"}
        assert records[1] == {"id": 3, "name": "Charlie", "status": "active"}
        db.close()


def test_standin_sourcerunner_query():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.duckdb")
        create_standin_db(db_path)

        auth = {
            "provider": "mysql",
            "dsn": db_path,
            "attach_type": "duckdb"
        }
        request = {
            "service": "database",
            "method": "query",
            "sdk": "mysql",
            "arguments": {
                "query": "SELECT id, name FROM customers WHERE id = 1"
            }
        }
        source = {
            "kind": "source",
            "name": "test_mysql_source",
            "auth": auth,
            "streams": [page_stream("test_stream", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 1
        assert output.records[0][1]["record"] == {"id": 1, "name": "Alice"}


def test_standin_sourcerunner_table():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.duckdb")
        create_standin_db(db_path)

        auth = {
            "provider": "mysql",
            "dsn": db_path,
            "attach_type": "duckdb"
        }
        request = {
            "service": "database",
            "method": "table",
            "sdk": "mysql",
            "arguments": {
                "table": "customers",
                "columns": ["id", "name", "status"],
                "where": [{"column": "status", "op": "=", "value": "inactive"}]
            }
        }
        source = {
            "kind": "source",
            "name": "test_mysql_source",
            "auth": auth,
            "streams": [page_stream("test_stream", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 1
        assert output.records[0][1]["record"] == {"id": 2, "name": "Bob", "status": "inactive"}
