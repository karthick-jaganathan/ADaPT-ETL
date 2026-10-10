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
The `mysql` connector: read-only SELECT queries on a MySQL database through DuckDB's MySQL extension.
"""

from streamwright.core.runtime import logs
from streamwright.core.runtime.components import Connector, ConnectorError
from streamwright.connectors.mysql.reader import (
    Database, ConnectError, QueryError, check_query, table_query,
    structured_conninfo, dsn_secrets, where_problems, identifier_problem
)

__all__ = ["MySQLConnector", "SERVICES", "DEFAULT_SERVICE"]

SERVICES = ("database",)
DEFAULT_SERVICE = "database"


def connector_error(exc):
    return ConnectorError(f"mysql: {exc}", retryable=False)


class MySQLConnector(Connector):
    """
    The `mysql` connector: executes read-only queries and table extracts against MySQL.
    """
    name = "mysql"
    category = "databases"
    summary = "MySQL tables, read-only"
    auth_required = ()
    auth_optional = ("dsn", "host", "port", "database", "user", "password", "sslmode", "options", "attach_type")

    def check_request(self, request):
        service = request.get("service") or DEFAULT_SERVICE
        if service not in SERVICES:
            return [f"mysql: unknown service {service!r} (services: {', '.join(SERVICES)})"]

        method = request.get("method")
        if method not in ("query", "table"):
            return [f"mysql: unknown method {method!r} (methods: query, table)"]

        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["mysql: `arguments` must be a mapping"]

        if method == "query":
            query = arguments.get("query")
            params = arguments.get("params") or {}
            if not query:
                return ["mysql: `query` in arguments is required"]
            _, problems = check_query(query, params)
            return problems

        if method == "table":
            table = arguments.get("table")
            if not table:
                return ["mysql: `table` in arguments is required"]
            prob = identifier_problem(table)
            if prob:
                return [f"mysql: table name {prob}"]
            return where_problems(arguments.get("where"))

        return []

    def connect(self, auth, context):
        dsn = auth.get("dsn")
        attach_type = auth.get("attach_type", "mysql")

        if dsn:
            conn_str = str(dsn).strip()
            for secret_val in dsn_secrets(conn_str):
                context.secret(secret_val)
        else:
            host = auth.get("host")
            database = auth.get("database")
            user = auth.get("user")
            if not host or not database or not user:
                raise ConnectorError("mysql: auth requires either `dsn` or `host`, `database`, and `user`")
            password = auth.get("password")
            if password:
                context.secret(password)
            conn_str = structured_conninfo(
                host=host,
                database=database,
                user=user,
                port=auth.get("port"),
                password=password,
                sslmode=auth.get("sslmode"),
                options=auth.get("options")
            )

        try:
            db = Database(conn_str, attach_type=attach_type)
            return db
        except (ConnectError, Exception) as exc:
            raise ConnectorError(f"mysql: cannot connect to database: {exc}")

    def request(self, client, request, context):
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))

        method = request.get("method")
        arguments = request.get("arguments") or {}

        if method == "query":
            query = arguments["query"]
            params = arguments.get("params") or {}
            for page in client.pages(query, params):
                yield page

        elif method == "table":
            schema = arguments.get("schema")
            table = arguments["table"]
            columns = arguments.get("columns")
            where = arguments.get("where")
            query, params = table_query(schema, table, columns, where)
            for page in client.pages(query, params):
                yield page

    def error(self, exc):
        if isinstance(exc, (ConnectError, QueryError)):
            return ConnectorError(f"mysql: {exc}", retryable=False)
        return None
