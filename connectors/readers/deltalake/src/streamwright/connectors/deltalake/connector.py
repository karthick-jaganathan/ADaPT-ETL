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
The `deltalake` connector: read-only queries and table scans on Delta Lake tables.
"""

from streamwright.core.runtime.components import Connector, ConnectorError
from streamwright.connectors.deltalake.reader import DeltaDatabase, ConnectError, QueryError, check_query, scan_query

__all__ = ["DeltaLakeConnector"]

SERVICES = ("tables",)
DEFAULT_SERVICE = "tables"


class DeltaLakeConnector(Connector):
    """
    The `deltalake` connector: runs read-only scans and queries on Delta Lake tables.
    """
    name = "deltalake"
    category = "lakehouse"
    summary = "Delta Lake tables (DuckDB delta)"
    auth_required = ()
    auth_optional = (
        "s3", "gcs", "azure", "local", "roots",
        "aws_access_key_id", "aws_secret_access_key", "aws_region",
        "gcs_key_file", "gcs_project_id", "options"
    )

    def check_request(self, request):
        service = request.get("service") or DEFAULT_SERVICE
        if service not in SERVICES:
            return [f"deltalake: unknown service {service!r} (services: {', '.join(SERVICES)})"]

        method = request.get("method")
        if method not in ("scan", "query"):
            return [f"deltalake: unknown method {method!r} (methods: scan, query)"]

        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["deltalake: `arguments` must be a mapping"]

        if method == "scan":
            if not (arguments.get("table_path") or arguments.get("table_uri")):
                return ["deltalake: `table_path` in arguments is required"]

        if method == "query":
            query = arguments.get("query")
            if not query:
                return ["deltalake: `query` in arguments is required"]
            _, problems = check_query(query, arguments.get("params"))
            return problems

        return []

    def connect(self, auth, context):
        from streamwright.connectors.deltalake.storage import build_storage_handlers
        try:
            handlers = build_storage_handlers(auth, context)
            return DeltaDatabase(storage_handlers=handlers)
        except (ConnectError, Exception) as exc:
            raise ConnectorError(f"deltalake: cannot initialize delta engine: {exc}")

    def request(self, client, request, context):
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))

        method = request.get("method")
        arguments = request.get("arguments") or {}

        if method == "scan":
            table_path = arguments.get("table_path") or arguments.get("table_uri")
            prob = client.check_path(table_path)
            if prob:
                raise ConnectorError(f"deltalake: {prob}")
            columns = arguments.get("columns")
            where = arguments.get("where")
            limit = arguments.get("limit")
            query, params = scan_query(table_path, columns, where, limit)
            for page in client.pages(query, params):
                yield page

        elif method == "query":
            query = arguments["query"]
            params = arguments.get("params") or {}
            table_path = arguments.get("table_path") or arguments.get("table_uri")
            if table_path:
                prob = client.check_path(table_path)
                if prob:
                    raise ConnectorError(f"deltalake: {prob}")
                if "delta_table" in query:
                    from streamwright.core.outputs.staging import sql_string
                    client.connection.execute(f"CREATE OR REPLACE TEMPORARY VIEW delta_table AS SELECT * FROM delta_scan({sql_string(table_path)})")
            for page in client.pages(query, params):
                yield page

    def error(self, exc):
        if isinstance(exc, (ConnectError, QueryError)):
            return ConnectorError(f"deltalake: {exc}", retryable=False)
        return None

