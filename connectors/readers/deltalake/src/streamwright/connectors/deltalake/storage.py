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
Storage handlers for Delta Lake tables across local filesystem, Amazon S3,
Google Cloud Storage, and Azure Blob storage.
"""

from abc import ABC, abstractmethod
import os

from streamwright.core.outputs.staging import sql_string

__all__ = [
    "StorageHandler",
    "LocalStorageHandler",
    "S3StorageHandler",
    "GCSStorageHandler",
    "AzureStorageHandler",
    "build_storage_handlers",
]


class StorageHandler(ABC):
    """Abstract base class for storage handlers."""

    @abstractmethod
    def configure(self, connection):
        """Configure the DuckDB connection for this storage backend."""
        pass

    def check_path(self, path):
        """Optional hook to validate path access for this storage backend."""
        return None


class LocalStorageHandler(StorageHandler):
    """
    Local filesystem storage handler.
    Operates natively in DuckDB without requiring network extensions or cloud credentials.
    """

    def __init__(self, allowed_roots=None):
        self.allowed_roots = [
            os.path.abspath(os.path.expanduser(r)) for r in (allowed_roots or [])
        ]

    def configure(self, connection):
        # Local paths work natively with DuckDB and delta_scan
        pass

    def check_path(self, path):
        if not self.allowed_roots:
            return None
        # Only check local paths (skip cloud URIs)
        if "://" in path:
            return None
        abs_path = os.path.abspath(os.path.expanduser(path))
        for root in self.allowed_roots:
            if abs_path == root or abs_path.startswith(root.rstrip(os.sep) + os.sep):
                return None
        return f"path {path!r} is outside allowed roots: {', '.join(self.allowed_roots)}"


class S3StorageHandler(StorageHandler):
    """
    Amazon S3 (and S3-compatible object storage, e.g. MinIO, Cloudflare R2, LocalStack).
    """

    def __init__(
        self,
        key_id=None,
        secret=None,
        session_token=None,
        region=None,
        endpoint=None,
        url_style=None,
        use_ssl=None,
    ):
        self.key_id = key_id
        self.secret = secret
        self.session_token = session_token
        self.region = region
        self.endpoint = endpoint
        self.url_style = url_style
        self.use_ssl = use_ssl

    def configure(self, connection):
        connection.execute("INSTALL httpfs")
        connection.execute("LOAD httpfs")

        # Create DuckDB temporary S3 secret for DeltaKernel
        secret_parts = ["TYPE s3"]
        if self.key_id:
            secret_parts.append(f"KEY_ID {sql_string(self.key_id)}")
            connection.execute(f"SET s3_access_key_id = {sql_string(self.key_id)}")
        if self.secret:
            secret_parts.append(f"SECRET {sql_string(self.secret)}")
            connection.execute(f"SET s3_secret_access_key = {sql_string(self.secret)}")
        if self.session_token:
            secret_parts.append(f"SESSION_TOKEN {sql_string(self.session_token)}")
            connection.execute(f"SET s3_session_token = {sql_string(self.session_token)}")
        if self.region:
            secret_parts.append(f"REGION {sql_string(self.region)}")
            connection.execute(f"SET s3_region = {sql_string(self.region)}")
        if self.endpoint:
            secret_parts.append(f"ENDPOINT {sql_string(self.endpoint)}")
            connection.execute(f"SET s3_endpoint = {sql_string(self.endpoint)}")
        if self.url_style:
            secret_parts.append(f"URL_STYLE {sql_string(self.url_style)}")
            connection.execute(f"SET s3_url_style = {sql_string(self.url_style)}")
        if self.use_ssl is not None:
            val = "true" if self.use_ssl is True or str(self.use_ssl).lower() == "true" else "false"
            secret_parts.append(f"USE_SSL {val}")
            connection.execute(f"SET s3_use_ssl = {val}")

        if len(secret_parts) > 1:
            try:
                connection.execute(f"CREATE TEMPORARY SECRET delta_s3 ({', '.join(secret_parts)})")
            except Exception:
                pass


class GCSStorageHandler(StorageHandler):
    """
    Google Cloud Storage handler.
    """

    def __init__(self, key_file=None, project_id=None):
        self.key_file = key_file
        self.project_id = project_id

    def configure(self, connection):
        connection.execute("INSTALL httpfs")
        connection.execute("LOAD httpfs")

        if self.key_file:
            connection.execute(f"SET gcs_key_file = {sql_string(self.key_file)}")
        if self.project_id:
            connection.execute(f"SET gcs_project_id = {sql_string(self.project_id)}")


class AzureStorageHandler(StorageHandler):
    """
    Azure Blob Storage / ADLS Gen2 handler.
    """

    def __init__(self, connection_string=None, account_name=None, account_key=None):
        self.connection_string = connection_string
        self.account_name = account_name
        self.account_key = account_key

    def configure(self, connection):
        try:
            connection.execute("INSTALL azure")
            connection.execute("LOAD azure")
        except Exception:
            pass

        secret_parts = ["TYPE azure"]
        if self.connection_string:
            secret_parts.append(f"CONNECTION_STRING {sql_string(self.connection_string)}")
            connection.execute(f"SET azure_storage_connection_string = {sql_string(self.connection_string)}")
        if self.account_name:
            secret_parts.append(f"ACCOUNT_NAME {sql_string(self.account_name)}")
        if self.account_key:
            secret_parts.append(f"ACCOUNT_KEY {sql_string(self.account_key)}")

        if len(secret_parts) > 1:
            try:
                connection.execute(f"CREATE TEMPORARY SECRET delta_azure ({', '.join(secret_parts)})")
            except Exception:
                pass


def build_storage_handlers(auth, context=None):
    """
    Decomposes `auth` to discover configured storage backends (Local, S3, GCS, Azure),
    registers credentials with `context.secret(...)`, and returns a list of StorageHandler instances.
    """
    handlers = []

    # 1. S3 configuration
    s3_dict = auth.get("s3") if isinstance(auth.get("s3"), dict) else {}
    key_id = auth.get("aws_access_key_id") or s3_dict.get("key_id") or s3_dict.get("access_key_id")
    secret = auth.get("aws_secret_access_key") or s3_dict.get("secret") or s3_dict.get("secret_access_key")
    token = auth.get("aws_session_token") or s3_dict.get("session_token")
    region = auth.get("aws_region") or s3_dict.get("region")
    endpoint = auth.get("s3_endpoint") or s3_dict.get("endpoint")
    url_style = auth.get("s3_url_style") or s3_dict.get("url_style")
    use_ssl = auth.get("s3_use_ssl", s3_dict.get("use_ssl"))

    if key_id or secret or token or region or endpoint:
        if context:
            if key_id:
                context.secret(str(key_id))
            if secret:
                context.secret(str(secret))
            if token:
                context.secret(str(token))
        handlers.append(S3StorageHandler(
            key_id=str(key_id) if key_id else None,
            secret=str(secret) if secret else None,
            session_token=str(token) if token else None,
            region=str(region) if region else None,
            endpoint=str(endpoint) if endpoint else None,
            url_style=str(url_style) if url_style else None,
            use_ssl=use_ssl,
        ))

    # 2. GCS configuration
    gcs_dict = auth.get("gcs") if isinstance(auth.get("gcs"), dict) else {}
    gcs_key = auth.get("gcs_key_file") or gcs_dict.get("key_file") or gcs_dict.get("service_account_key")
    project_id = auth.get("gcs_project_id") or gcs_dict.get("project_id")

    if gcs_key or project_id:
        if context and gcs_key:
            context.secret(str(gcs_key))
        handlers.append(GCSStorageHandler(
            key_file=str(gcs_key) if gcs_key else None,
            project_id=str(project_id) if project_id else None,
        ))

    # 3. Azure configuration
    azure_dict = auth.get("azure") if isinstance(auth.get("azure"), dict) else {}
    conn_str = auth.get("azure_connection_string") or azure_dict.get("connection_string")
    account_name = auth.get("azure_account_name") or azure_dict.get("account_name")
    account_key = auth.get("azure_account_key") or azure_dict.get("account_key")
    if conn_str or (account_name and account_key):
        if context:
            if conn_str:
                context.secret(str(conn_str))
            if account_key:
                context.secret(str(account_key))
        handlers.append(AzureStorageHandler(
            connection_string=str(conn_str) if conn_str else None,
            account_name=str(account_name) if account_name else None,
            account_key=str(account_key) if account_key else None,
        ))

    # 4. Local filesystem configuration
    local_dict = auth.get("local") if isinstance(auth.get("local"), dict) else {}
    allowed_roots = auth.get("roots") or local_dict.get("roots") or auth.get("allowed_roots")
    # Always include LocalStorageHandler (either sandboxed with allowed_roots or open for local files)
    handlers.append(LocalStorageHandler(allowed_roots=allowed_roots))

    return handlers
