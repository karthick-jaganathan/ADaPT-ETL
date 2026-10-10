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

import datetime
from unittest.mock import MagicMock, patch
import pytest
from bson import ObjectId
from pymongo.errors import ServerSelectionTimeoutError, OperationFailure

from streamwright.core.runtime.testing import MemoryOutput, page_stream
from streamwright.core.runtime.components import ConnectorError
from streamwright.core.engine.runner import SourceRunner
from streamwright.connectors.mongodb.connector import MongoDBConnector, _serialize_val


def test_missing_auth():
    connector = MongoDBConnector()
    class DummyContext:
        def secret(self, val):
            pass
    with pytest.raises(ConnectorError, match="auth requires either `uri` or `host` and `database`"):
        connector.connect({}, DummyContext())


def test_serialization():
    oid = ObjectId("507f1f77bcf86cd799439011")
    dt = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    raw = {
        "_id": oid,
        "name": "Widget",
        "created_at": dt,
        "nested": {"binary": b"hello"}
    }
    serialized = _serialize_val(raw)
    assert serialized["_id"] == "507f1f77bcf86cd799439011"
    assert serialized["created_at"] == "2026-01-01T12:00:00+00:00"
    assert serialized["nested"]["binary"] == "aGVsbG8="


def test_check_request():
    connector = MongoDBConnector()
    assert connector.check_request({"method": "invalid"}) != []
    assert connector.check_request({"method": "find"}) != []  # missing collection
    assert connector.check_request({"method": "find", "arguments": {"collection": "users", "filter": "not-a-dict"}}) != []
    assert connector.check_request({"method": "find", "arguments": {"collection": "users", "filter": {"status": "active"}}}) == []
    assert connector.check_request({"method": "aggregate", "arguments": {"collection": "orders", "pipeline": "not-a-list"}}) != []
    assert connector.check_request({"method": "aggregate", "arguments": {"collection": "orders", "pipeline": []}}) == []


def test_mock_find_request():
    connector = MongoDBConnector()
    mock_cursor = [
        {"_id": ObjectId("507f1f77bcf86cd799439011"), "name": "Item A", "price": 10.5},
        {"_id": ObjectId("507f1f77bcf86cd799439012"), "name": "Item B", "price": 20.0}
    ]
    mock_coll = MagicMock()
    mock_coll.find.return_value = mock_cursor

    mock_db = MagicMock()
    mock_db.__getitem__.return_value = mock_coll

    mock_client = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_client._default_database = "test_db"

    request = {
        "service": "database",
        "method": "find",
        "arguments": {
            "collection": "items",
            "filter": {"price": {"$gt": 5}}
        }
    }
    pages = list(connector.request(mock_client, request, None))
    assert len(pages) == 1
    assert len(pages[0]) == 2
    assert pages[0][0]["_id"] == "507f1f77bcf86cd799439011"
    assert pages[0][0]["name"] == "Item A"


def test_mock_aggregate_request():
    connector = MongoDBConnector()
    mock_cursor = [
        {"_id": "category_1", "total_sales": 1500}
    ]
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = mock_cursor

    mock_db = MagicMock()
    mock_db.__getitem__.return_value = mock_coll

    mock_client = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_client._default_database = "test_db"

    request = {
        "service": "database",
        "method": "aggregate",
        "arguments": {
            "collection": "sales",
            "pipeline": [{"$group": {"_id": "$category", "total_sales": {"$sum": "$amount"}}}]
        }
    }
    pages = list(connector.request(mock_client, request, None))
    assert len(pages) == 1
    assert pages[0][0] == {"_id": "category_1", "total_sales": 1500}


def test_sourcerunner_with_mock():
    mock_cursor = [
        {"_id": ObjectId("507f1f77bcf86cd799439011"), "name": "Product X"}
    ]
    mock_coll = MagicMock()
    mock_coll.find.return_value = mock_cursor

    mock_db = MagicMock()
    mock_db.__getitem__.return_value = mock_coll

    mock_client = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    mock_client._default_database = "store"

    with patch.object(MongoDBConnector, "connect", return_value=mock_client):
        auth = {"provider": "mongodb", "uri": "mongodb://localhost:27017/store"}
        request = {
            "service": "database",
            "method": "find",
            "sdk": "mongodb",
            "arguments": {
                "collection": "products"
            }
        }
        source = {
            "kind": "source",
            "name": "mongo_test",
            "auth": auth,
            "streams": [page_stream("test_stream", request, "SELECT * FROM records")]
        }
        output = MemoryOutput()
        SourceRunner(source, {}, auth, output=output).run()
        assert len(output.records) == 1
        assert output.records[0][1]["record"]["name"] == "Product X"


def test_error_translation():
    connector = MongoDBConnector()
    timeout_err = connector.error(ServerSelectionTimeoutError("No server available"))
    assert timeout_err.retryable is True

    op_err = connector.error(OperationFailure("Authentication failed"))
    assert op_err.retryable is False

    assert connector.error(KeyError("unrelated")) is None
