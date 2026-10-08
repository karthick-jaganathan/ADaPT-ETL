"""The files connector is local-only: no URL roots or paths, no external access, no httpfs, no secrets."""

import os

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("streamwright.connectors.files.connector")

from streamwright.connectors.files import reader  # noqa: E402
from streamwright.connectors.files.connector import FilesConnector  # noqa: E402
from streamwright.connectors.files.reader import AccessError, Reader, allowed_roots  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402


class Recorder(object):
    """A DuckDB connection that records every statement it executes."""

    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, query, *args, **kwargs):
        self._statements.append(query)
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def recorded(monkeypatch):
    """(statements, configs): what every DuckDB connection opened from now on executes, and its config."""
    import duckdb
    statements, configs = [], []
    connect = duckdb.connect

    def recording(*args, **kwargs):
        configs.append(kwargs.get("config"))
        return Recorder(connect(*args, **kwargs), statements)
    monkeypatch.setattr(duckdb, "connect", recording)
    return statements, configs


def context(connector):
    return ConnectorContext(connector, Redactor())


def test_reading_never_enables_external_access_nor_loads_httpfs(recorded, tmp_path):
    statements, configs = recorded
    (tmp_path / "a.csv").write_text("id\n1\n")
    connector = FilesConnector()
    client = connector.connect({"roots": [str(tmp_path)]}, context(connector))
    try:
        pages = list(connector.request(client, {"method": "read", "arguments": {"path": "a.csv"}}, context(connector)))
        assert pages == [[{"id": "1"}]]
        assert configs == [dict(reader.LIMITS, autoinstall_known_extensions=False, autoload_known_extensions=False,
                                python_enable_replacements=False, temp_directory=client.folder)]
        setup = statements[:4]
        assert setup == ["SET TimeZone = 'UTC'",
                         "SET allowed_directories = ['%s']" % (os.path.realpath(str(tmp_path)) + os.sep),
                         "SET enable_external_access = false", "SET lock_configuration = true"]
        text = "\n".join(statements).lower()
        for word in ("httpfs", "install", "load ", "secret", "enable_external_access = true", "s3://", "gs://",
                     "http://", "https://"):
            assert word not in text, word
        connection = client.connection
        assert connection.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is False
        assert connection.execute("SELECT current_setting('lock_configuration')").fetchone()[0] is True
        # httpfs adds its s3_* settings when it is loaded (duckdb_extensions() itself needs the file system)
        assert connection.execute("SELECT count(*) FROM duckdb_settings() WHERE name LIKE 's3\\_%' ESCAPE '\\'"
                                  ).fetchone()[0] == 0
        with pytest.raises(Exception):
            connection.execute("SET enable_external_access = true")
    finally:
        client.close()


@pytest.mark.parametrize("roots", [
    ["s3://bucket/prefix/"],
    ["gs://lake/raw/"],
    ["https://files.example.com/pub/"],
    ["LOCAL", "s3://bucket/"],
])
def test_url_roots_are_refused_before_any_connection(recorded, tmp_path, roots):
    statements, configs = recorded
    roots = [str(tmp_path) if root == "LOCAL" else root for root in roots]
    connector = FilesConnector()
    assert any("is a URL: roots are local folders" in problem for problem in connector.check_auth({"roots": roots}))
    with pytest.raises(ConnectorError, match="is a URL: roots are local folders; the files connector reads local files "
                                             "only \\(object storage: the s3 and gcs connectors\\)"):
        connector.connect({"roots": roots}, context(connector))
    with pytest.raises(AccessError, match="is a URL"):
        allowed_roots(roots)
    with pytest.raises(AccessError, match="roots are local folders"):
        Reader(roots)
    assert statements == [] and configs == []


def test_the_reader_takes_only_absolute_local_roots(recorded):
    statements, configs = recorded
    with pytest.raises(AccessError, match="roots are local folders"):
        Reader(["relative/folder"])
    with pytest.raises(AccessError, match="none"):
        Reader([])
    assert configs == []


@pytest.mark.parametrize("arguments", [
    {"path": "s3://bucket/a.csv"},
    {"path": "gs://lake/*.parquet"},
    {"path": "https://files.example.com/a.csv"},
    {"path": ["a.csv", "s3://bucket/b.csv"]},
    {"path": "s3://bucket/", "match": r".*\.csv"},
])
def test_url_paths_are_refused_before_any_read(recorded, tmp_path, arguments):
    statements, _ = recorded
    (tmp_path / "a.csv").write_text("id\n1\n")
    connector = FilesConnector()
    assert any("is a URL" in problem for problem in connector.check_request({"method": "read", "arguments": arguments}))
    client = connector.connect({"roots": [str(tmp_path)]}, context(connector))
    try:
        with pytest.raises(ConnectorError, match="is a URL: the files connector reads local files only") as caught:
            list(connector.request(client, {"method": "read", "arguments": arguments}, context(connector)))
        assert caught.value.code == "ACCESS_DENIED"
        assert not [statement for statement in statements if "read_" in statement]
    finally:
        client.close()


def test_the_reader_module_has_no_object_storage():
    for name in ("load_httpfs", "secret_query", "CREDENTIALS", "URL_SCHEMES", "url_root", "MissingError"):
        assert not hasattr(reader, name), name
    assert FilesConnector.auth_optional == ()
