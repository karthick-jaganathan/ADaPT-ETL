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
The `mongodb` connector: document extraction from MongoDB collections and aggregation pipelines.

Connects to MongoDB using `pymongo` in read-only mode, extracting BSON documents serialized safely to JSON.
Supports find queries with filter/projection/sort and aggregation pipeline stages.

### 1. `source.yaml` Contract
```yaml
kind: source
name: mongodb_source
spec:
  secrets:
    mongo_uri: {type: string, required: true}

auth:
  provider: mongodb
  uri: "{{ secrets.mongo_uri }}"        # MongoDB connection string URI from secrets

# Alternative structured auth:
# auth:
#   provider: mongodb
#   host: "cluster0.mongodb.net"
#   port: 27017
#   database: "production"
#   username: "analytics_ro"
#   password: "{{ secrets.mongo_password }}"
#   auth_source: "admin"
```

### 2. `streams/<stream>.yaml` Contract
```yaml
requests:
  # Method 1: Filtered find query
  - name: read_customers
    sdk: mongodb
    service: database
    method: find
    arguments:
      collection: "customers"
      filter:
        status: "active"
        updated_at: {"$gte": "{{ window.start }}"}
      projection:
        _id: 1
        name: 1
        email: 1
        tier: 1
      sort: [["_id", 1]]
      batch_size: 1000

  # Method 2: Aggregation pipeline
  # - name: customer_metrics
  #   sdk: mongodb
  #   service: database
  #   method: aggregate
  #   arguments:
  #     collection: "orders"
  #     pipeline:
  #       - {"$match": {"created_at": {"$gte": "{{ window.start }}"}}}
  #       - {"$group": {"_id": "$customer_id", "total_spend": {"$sum": "$amount"}}}
  #     batch_size: 1000

transform:
  - name: final_customers
    select: |
      SELECT 
        _id AS customer_id,
        name,
        email,
        tier
      FROM read_customers

export:
  customers:
    step: final_customers
    primary_key: [customer_id]
```

### 3. Authentication & Security
- `provider`: `mongodb`
- Credentials: `uri` or structured `password` must be `{{ secrets.* }}` references. Credentials are never written in source configs and are redacted from all logs.
- Security: Connects using read-only operations (`find`, `aggregate`). Mutations are not supported.

### 4. Transform & Data Shaping
- Emits records as JSON dictionaries with BSON types automatically serialized (e.g. `ObjectId` to string, `Decimal128` to float, dates to ISO-8601).
- In `transform` steps, request results are available as relational tables in DuckDB SQL.

### 5. Execution & Behavior
- **Transport**: `sdk` (`pymongo`).
- **Services & Methods**:
  - `database.find`: Arguments: `collection` (string, required), `filter` (dict), `projection` (dict/list), `sort` (list of [field, dir]), `batch_size` (int).
  - `database.aggregate`: Arguments: `collection` (string, required), `pipeline` (list of dicts), `batch_size` (int).
- **Streaming**: Yields documents in cursor batches (default 1,000 documents).
"""

import base64
import datetime
from urllib.parse import quote_plus, urlsplit, unquote

from streamwright.core.runtime.components import Connector, ConnectorError, ConnectorSpec

__all__ = ["MongoDBConnector"]

DEFAULT_PORT = 27017
DEFAULT_BATCH_SIZE = 1000


def _serialize_val(val):
    if val is None:
        return None
    type_name = type(val).__name__
    if type_name == "ObjectId":
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.isoformat()
    if type_name == "Decimal128":
        return float(str(val))
    if isinstance(val, bytes):
        return base64.b64encode(val).decode("ascii")
    if isinstance(val, dict):
        return {k: _serialize_val(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize_val(x) for x in val]
    return val


def _extract_uri_password(uri):
    try:
        parts = urlsplit(uri)
        if parts.password:
            return [parts.password, unquote(parts.password)]
    except Exception:
        pass
    return []


class MongoDBConnector(Connector):
    """
    The `mongodb` connector: read-only extraction from MongoDB collections.
    """
    spec = ConnectorSpec(
        name="mongodb",
        title="MongoDB",
        category="databases",
        transport="sdk",
        package="pymongo",
    )
    auth_required = ()
    auth_optional = ("uri", "host", "port", "database", "username", "password", "auth_source", "options")

    def check_request(self, request):
        method = request.get("method")
        if method not in ("find", "aggregate"):
            return [f"mongodb: unknown method {method!r} (methods: find, aggregate)"]

        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["mongodb: `arguments` must be a mapping"]

        if not arguments.get("collection"):
            return ["mongodb: `collection` in arguments is required"]

        if method == "find":
            if "filter" in arguments and not isinstance(arguments["filter"], dict):
                return ["mongodb: `filter` in arguments must be a mapping"]
            if "projection" in arguments and not isinstance(arguments["projection"], (dict, list)):
                return ["mongodb: `projection` in arguments must be a mapping or list"]

        if method == "aggregate":
            if "pipeline" in arguments and not isinstance(arguments["pipeline"], list):
                return ["mongodb: `pipeline` in arguments must be a list"]

        return []

    def connect(self, auth, context):
        from pymongo import MongoClient

        uri = auth.get("uri")
        database_name = auth.get("database")

        if uri:
            uri_str = str(uri).strip()
            for pw in _extract_uri_password(uri_str):
                context.secret(pw)
            client = MongoClient(uri_str, serverSelectionTimeoutMS=10000)
            if not database_name:
                try:
                    path = urlsplit(uri_str).path.lstrip("/")
                    if path:
                        database_name = path
                except Exception:
                    pass
        else:
            host = auth.get("host")
            if not host:
                raise ConnectorError("mongodb: auth requires either `uri` or `host` and `database`")
            if not database_name:
                raise ConnectorError("mongodb: auth requires `database`")

            port = auth.get("port") or DEFAULT_PORT
            username = auth.get("username")
            password = auth.get("password")
            auth_source = auth.get("auth_source") or "admin"

            if password:
                context.secret(str(password))

            if username and password:
                encoded_user = quote_plus(str(username))
                encoded_pass = quote_plus(str(password))
                conn_uri = f"mongodb://{encoded_user}:{encoded_pass}@{host}:{port}/{database_name}?authSource={auth_source}"
            else:
                conn_uri = f"mongodb://{host}:{port}/{database_name}"

            client = MongoClient(conn_uri, serverSelectionTimeoutMS=10000)

        client._default_database = database_name
        return client

    def request(self, client, request, context):
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))

        method = request["method"]
        arguments = request.get("arguments") or {}
        collection_name = arguments["collection"]
        db_name = arguments.get("database") or getattr(client, "_default_database", None)

        if not db_name:
            raise ConnectorError("mongodb: database name must be specified in auth or request arguments")

        db = client[db_name]
        coll = db[collection_name]
        try:
            batch_size = int(arguments.get("batch_size") or DEFAULT_BATCH_SIZE)
        except (ValueError, TypeError):
            batch_size = DEFAULT_BATCH_SIZE
        if batch_size <= 0:
            batch_size = DEFAULT_BATCH_SIZE

        if method == "find":
            filter_doc = arguments.get("filter") or {}
            projection = arguments.get("projection")
            sort = arguments.get("sort")
            limit = arguments.get("limit")

            cursor = coll.find(filter_doc, projection=projection)
            if sort:
                cursor = cursor.sort(sort)
            if limit:
                try:
                    cursor = cursor.limit(int(limit))
                except (ValueError, TypeError):
                    pass

        elif method == "aggregate":
            pipeline = arguments.get("pipeline") or []
            cursor = coll.aggregate(pipeline)

        batch = []
        for doc in cursor:
            batch.append(_serialize_val(doc))
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def error(self, exc):
        from pymongo.errors import PyMongoError, ServerSelectionTimeoutError, NetworkTimeout
        if isinstance(exc, (ServerSelectionTimeoutError, NetworkTimeout)):
            return ConnectorError(f"mongodb: timeout connecting to server: {exc}", retryable=True)
        if isinstance(exc, PyMongoError):
            return ConnectorError(f"mongodb: {exc}", retryable=False)
        return None
