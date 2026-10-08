"""
The gcs connector: URL roots and containment, the `?` query refusal, httpfs and the secret, credentials and redaction,
formats, pages, globs, `match`, on_missing, and runs through SourceRunner and the CLI.

Offline: Readers get a Recorder connection that records DuckDB's httpfs and secret statements instead of running them,
lists URLs from Storage.objects (as DuckDB's object-storage glob does) and reads them from local files, and forces
errors; the rest runs on a real DuckDB connection. Tests with DuckDB's real httpfs run when it is installed, against
local addresses only (127.0.0.1); one against real object storage only with STREAMWRIGHT_TEST_GCS set.
"""

import datetime
import decimal
import fnmatch
import json
import logging
import os
import socket
import threading
import time

import pytest
import yaml

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("streamwright.connectors.gcs.connector")

from streamwright.connectors.gcs import reader  # noqa: E402
from streamwright.connectors.gcs.connector import GcsConnector  # noqa: E402
from streamwright.connectors.gcs.reader import (AccessError, Reader, allowed_roots, match_files,  # noqa: E402
                                         resolve_files)
from streamwright.core import cli
from streamwright.core.runtime import components  # noqa: E402
from streamwright.core.net.http import Redactor  # noqa: E402
from streamwright.core.runtime.logs import RunMetrics  # noqa: E402
from streamwright.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402
from streamwright.core.engine.runner import SourceError, SourceRunner  # noqa: E402
from streamwright.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "sources", "readers", "gcs_demo")
KEY_ID = "GOOG1EEXAMPLEHMACKEY0042"
SECRET = "bGoa+V7g/yqDXvKRqq+JTFn4uQZbPiQJo4pf9RzJ"
CREDENTIAL_VALUES = (KEY_ID, SECRET)
AUTH = {"key_id": KEY_ID, "secret": SECRET}
ROOTS = ["gs://acme-data/in/", "gs://lake/raw"]
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
    """What Recorder connections see: whether httpfs is installed, objects (URL -> local file) and forced errors."""

    def __init__(self, connect):
        self.connect = connect  # the real duckdb.connect
        self.installed = True
        self.objects = {}
        self.errors = {}  # "INSTALL", "LOAD", "SECRET", "GLOB" or "READ" -> the exception the statement raises
        self.statements = []  # (statement, parameters) of every Reader connection, in order
        self.listed = []  # the globs listed
        self._plain = None

    def plain(self):
        if self._plain is None:
            self._plain = self.connect()
            self._plain.execute("SET TimeZone = 'UTC'")
        return self._plain

    def sql(self):
        return [statement for statement, _ in self.statements]

    def reads(self):
        return [statement for statement in self.sql() if any(name in statement for name in _READS)]

    def serve(self, folder, prefix):
        """Serves every file under a local folder as an object: prefix + its path relative to the folder."""
        for current, _, names in os.walk(str(folder)):
            for name in names:
                path = os.path.join(current, name)
                self.objects[prefix + os.path.relpath(path, str(folder)).replace(os.sep, "/")] = path


class Recorder(object):
    """
    A Reader's DuckDB connection: a real one, except that httpfs and secret statements are recorded, not run, globs
    over URLs list Storage.objects, and reads of URLs read their local files (on a connection without the Reader's
    limits: the Reader's own allows URLs only).
    """

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
        if "duckdb_extensions()" in sql and "'httpfs'" in sql:
            return Result([(self.storage.installed,)])
        for start, kind in (("INSTALL httpfs", "INSTALL"), ("LOAD httpfs", "LOAD"),
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
                raise duckdb.HTTPException("HTTP Error: HTTP GET error reading '%s' in region 'us-east-1' "
                                           "(HTTP 404 Not Found)" % first[0])
            return self.storage.plain().execute(query, [[self.storage.objects[first[0]]]] + list(parameters[1:]))
        return self.real.execute(query) if parameters is None else self.real.execute(query, parameters)

    def close(self):
        self.real.close()


@pytest.fixture
def storage(monkeypatch):
    """Storage for Readers made in the test: each gets a Recorder connection (other connections are DuckDB's own)."""
    state = Storage(duckdb.connect)

    def connect(*args, **kwargs):
        connection = state.connect(*args, **kwargs)
        reader_config = "streamwright-gcs-" in str((kwargs.get("config") or {}).get("temp_directory"))
        return Recorder(connection, state) if reader_config else connection
    monkeypatch.setattr(duckdb, "connect", connect)
    yield state
    if state._plain is not None:
        state._plain.close()


@pytest.fixture
def connector():
    component = GcsConnector(page_size=2)
    components.register(component)
    yield component
    components.unregister("gcs")


def context(connector, where=None, metrics=None):
    """A context whose redactor masks the run's secrets (as SourceRunner's does): the credentials of these tests."""
    result = ConnectorContext(connector, Redactor(CREDENTIAL_VALUES), metrics=metrics)
    result.where = where
    return result


def connect(connector, roots=ROOTS, ctx=None, **auth):
    return connector.connect(dict(AUTH, roots=list(roots), **auth), ctx or context(connector))


def call(arguments):
    return {"service": "object", "method": "read", "arguments": arguments}


def read(connector, arguments, roots=ROOTS, **auth):
    client = connect(connector, roots, **auth)
    try:
        return [record for page in connector.request(client, call(arguments), context(connector)) for record in page]
    finally:
        client.close()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def parquet(path, select):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("COPY (%s) TO '%s' (FORMAT parquet)" % (select, path))
    finally:
        connection.close()
    return str(path)


def setting(client, name):
    return client.connection.real.execute("SELECT current_setting(?)", [name]).fetchone()[0]


def sdk(name, **arguments):
    return {"name": name, "sdk": "gcs", "service": "object", "method": "read", "arguments": arguments}


def gcs_source(*streams, **top):
    return dict({"kind": "source", "name": "lake", "auth": {
        "provider": "gcs", "roots": ["gs://acme-data/in/"], "key_id": "{{ secrets.gcs_key_id }}",
        "secret": "{{ secrets.gcs_secret }}"}, "streams": list(streams)}, **top)


def run(source, output=None, config=None, state=None, metrics=None, today=None):
    output = output if output is not None else MemoryOutput()
    SourceRunner(source, config or {}, {"gcs_key_id": KEY_ID, "gcs_secret": SECRET}, state=state, output=output,
                 sleep=lambda seconds: None, allowed_connectors=["gcs"], metrics=metrics, today=today).run()
    return output


def records(output, name):
    return [record for stream, record in output.records if stream == name]


def nothing_read(storage):
    return storage.reads() == [] and storage.listed == []


# * ------------------------------
# * URL roots: what can be read
# * ------------------------------

def test_url_roots_are_prefixes_ending_with_a_slash():
    assert allowed_roots(["GS://acme-data", "gs://lake/raw", "gs://acme-data/"]) == [
        "gs://acme-data/", "gs://lake/raw/"]


@pytest.mark.parametrize("root,message", [
    ("gs://acme-data/in/*.csv", "is a glob: a root is a folder"),
    ("gs://acme-data/in/[ab]/", "is a glob: a root is a folder"),
    ("gs://user@acme-data/in/", "has credentials in it"),
    ("gs://acme-data:9000/in/", "has a port"),
    ("s3://acme-data/in/", "uses the s3:// scheme: the gcs connector reads gs:// URLs only"),
    ("gcs://acme-data/in/", "uses the gcs:// scheme: the gcs connector reads gs:// URLs only"),
    ("https://storage.googleapis.com/acme-data/in/", "uses the https:// scheme"),
    ("/data/in", "is not a gs:// URL"),
    ("gs:///in", "names no bucket"),
    ("gs://acme data/in", "has a bucket name that is not valid"),
    ("gs://acme-data/in/../other", "goes up a folder"),
    ("gs://acme-data/in/?s3_endpoint=evil.example.com", "has a `?`"),
    ("gs://acme-data/in%2f..%2fother/", "has a `%`"),
    ("gs://acme-data/in#x", "has a fragment"),
])
def test_url_roots_that_cannot_be_prefixes_are_refused(root, message):
    with pytest.raises(AccessError, match=message):
        allowed_roots([root])


@pytest.mark.parametrize("path,expected", [
    ("gs://acme-data/in/a.csv", ("gs://acme-data/in/a.csv", "gs://acme-data/in/")),
    ("GS://acme-data/in/a.csv", ("gs://acme-data/in/a.csv", "gs://acme-data/in/")),
    ("a.csv", ("gs://acme-data/in/a.csv", "gs://acme-data/in/")),  # relative to the first root
    ("2026/10/01.jsonl", ("gs://acme-data/in/2026/10/01.jsonl", "gs://acme-data/in/")),
    ("gs://lake/raw/day=2026-10-01/part-0.parquet", ("gs://lake/raw/day=2026-10-01/part-0.parquet", "gs://lake/raw/")),
    ("gs://acme-data/in/a b+c=d.csv", ("gs://acme-data/in/a b+c=d.csv", "gs://acme-data/in/")),
])
def test_urls_inside_a_root_are_accepted(path, expected):
    assert resolve_files(path, allowed_roots(ROOTS)) == [expected]


ESCAPES = [
    ("gs://acme-data/other/a.csv", "is outside the roots"),
    ("gs://acme-data/inside.csv", "is outside the roots"),  # a prefix of the root's folder name is not inside it
    ("gs://acme-data/in", "is outside the roots"),
    ("gs://acme-data-evil/in/a.csv", "is outside the roots"),  # a bucket the root's is a prefix of
    ("gs://acme/in/a.csv", "is outside the roots"),
    ("gs://ACME-DATA/in/a.csv", "is outside the roots"),  # buckets are compared as written
    ("gs://lake/rawdata/a.csv", "is outside the roots"),
    ("s3://acme-data/in/a.csv", "uses the s3:// scheme"),  # another scheme
    ("gcs://acme-data/in/a.csv", "uses the gcs:// scheme"),
    ("gs://acme-data:443/in/a.csv", "has a port"),
    ("gs://evil.example.com@acme-data/in/a.csv", "has credentials in it"),
    ("gs://acme-data/in/../secret.csv", "goes up a folder"),
    ("gs://acme-data/in/sub/../../secret.csv", "goes up a folder"),
    ("../secret.csv", "goes up a folder"),
    ("gs://acme-data/in/./a.csv", "has a `.` folder"),
    ("gs://acme-data/in//a.csv", "has an empty folder"),
    ("gs://acme-data/in/%2e%2e/secret.csv", "has a `%`"),
    ("gs://acme-data/in/..%2Fsecret.csv", "has a `%`"),
    ("gs://acme-data/in/a%5c..%5csecret.csv", "has a `%`"),
    ("gs://acme-data/in/a.csv%3fs3_endpoint=evil.example.com", "has a `%`"),
    ("gs://acme-data/in/a.csv%253fs3_endpoint=evil.example.com", "has a `%`"),
    ("gs://acme-data/in\\..\\a.csv", "has a backslash"),
    ("gs://acme-data/in/a.csv#x", "has a fragment"),
    ("gs://acme-data/in/a\n.csv", "has a control character"),
    ("gs://acme-data/in/a.csv?s3_endpoint=evil.example.com", "has a `?`"),
    ("gs://acme-data/in/a.csv?s3_access_key_id=AKIAOTHER&s3_secret_access_key=x", "has a `?`"),
    ("gs://acme-data/in/a.csv?s3_region=eu-west-1", "has a `?`"),
    ("gs://acme-data/in/?.csv", "has a `?`"),  # `?` is no glob character here
    ("a.csv?s3_endpoint=evil.example.com", "has a `?`"),  # relative keys too
    ("gs://acme-data/in/*.csv?s3_endpoint=evil.example.com", "has a `?`"),
    ("http://acme-data/in/a.csv", "is not a gs:// URL|uses the http:// scheme"),
    ("/etc/hosts", "is an absolute local path"),
    ("file:///etc/hosts", "uses the file:// scheme"),
    (["gs://acme-data/in/a.csv", "gs://acme-data/other/a.csv"], "is outside the roots"),
    (["gs://acme-data/in/a.csv", "gs://acme-data/in/a.csv?s3_endpoint=evil.example.com"], "has a `?`"),
]


@pytest.mark.parametrize("path,message", ESCAPES)
def test_urls_outside_every_root_are_refused_before_any_read(connector, storage, tmp_path, path, message):
    storage.objects = {"gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id\n1\n")}
    with pytest.raises(ConnectorError, match=message) as caught:
        read(connector, {"path": path})
    assert caught.value.code == "ACCESS_DENIED"
    assert nothing_read(storage)


@pytest.mark.parametrize("path,message", ESCAPES[:-2])
def test_the_reader_refuses_them_with_no_connection_at_all(path, message):
    with pytest.raises(AccessError, match=message):
        resolve_files(path, allowed_roots(ROOTS))  # no lister, no DuckDB: refused by the checks alone


@pytest.mark.parametrize("path,message", [
    ("gs://acme-data/in/a.csv?s3_endpoint=127.0.0.1:1", "has a `?`"),
    ("gs://acme-data/other/a.csv", "is outside the roots"),
    ("gs://acme-data/in/*.csv", "is a glob"),
])
def test_every_read_checks_its_url_again(storage, path, message):
    with Reader(allowed_roots(ROOTS), credentials=AUTH) as client:
        with pytest.raises(AccessError, match=message):
            list(client.pages(path, "csv"))
        if "glob" not in message:
            with pytest.raises(AccessError, match=message):
                client._list(path)
    assert nothing_read(storage)


# * ----------------------------------------------------------------
# * the query-parameter injection: ?s3_endpoint= never reaches DuckDB
# * ----------------------------------------------------------------

class Listener(object):
    """
    A local HTTP listener on 127.0.0.1 that records the requests made to it and answers 404 (nothing leaves the
    machine).
    """

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.requests = []
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while self.running:
            try:
                connection, _ = self.socket.accept()
            except (socket.timeout, OSError):
                continue
            try:
                connection.settimeout(2)
                self.requests.append(connection.recv(4096))
                connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                connection.close()

    def connected(self):
        time.sleep(0.3)
        return bool(self.requests)

    def close(self):
        self.running = False
        self.thread.join(2)
        self.socket.close()


@pytest.fixture
def listener():
    result = Listener()
    yield result
    result.close()


def test_a_query_redirecting_the_endpoint_is_refused_before_any_network_call(connector, storage, tmp_path, listener):
    """The reviewer's exploit: gs://<root>/x.csv?s3_endpoint=HOST would send a signed request to HOST."""
    attack = "gs://acme-data/in/x.csv?s3_endpoint=127.0.0.1:%d" % listener.port
    # refused by the checks alone: no Reader, no DuckDB
    with pytest.raises(AccessError, match="has a `\\?`: object storage URLs take no query"):
        resolve_files(attack, allowed_roots(ROOTS))
    assert reader.path_problem(attack).startswith("has a `?`")
    # refused by the connector, before any listing or read
    storage.objects = {"gs://acme-data/in/x.csv": write(tmp_path / "x.csv", "id\n1\n")}
    for path in (attack, attack.replace("?", "%3f"), attack.replace("?", "%3F"), "x.csv?s3_endpoint=127.0.0.1:1",
                 "gs://acme-data/in/*.csv?s3_endpoint=127.0.0.1:1"):
        with pytest.raises(ConnectorError) as caught:
            read(connector, {"path": path})
        assert caught.value.code == "ACCESS_DENIED"
    with pytest.raises(ConnectorError) as caught:
        read(connector, {"path": attack, "match": r".*"})
    assert caught.value.code == "ACCESS_DENIED"
    assert nothing_read(storage)
    assert not listener.connected()
    # and before a run starts: streamwright validate reports it
    assert connector.check_request(call({"path": attack})) == [
        "gcs: path %r has a `?`: object storage URLs take no query - DuckDB would read its parameters (e.g. "
        "?s3_endpoint=, ?s3_access_key_id=) as connection settings; `?` is not a glob character here" % attack]


def test_a_listed_object_with_a_query_is_refused_before_any_read(connector, storage, tmp_path):
    local = write(tmp_path / "a.csv", "id\n1\n")
    storage.objects = {"gs://acme-data/in/a.csv": local, "gs://acme-data/in/b.csv?s3_endpoint=127.0.0.1:1": local}
    for arguments in ({"path": "*.csv*"}, {"path": "gs://acme-data/in/", "match": r"a\.csv"}):
        with pytest.raises(ConnectorError,
                           match=r"the listed object 'gs://acme-data/in/b.csv\?s3_endpoint=127.0.0.1:1' "
                                 r"has a `\?`") as caught:
            read(connector, arguments)
        assert caught.value.code == "ACCESS_DENIED"
    assert storage.reads() == [sql for sql in storage.reads() if "glob(" in sql]  # listed, never read


@pytest.mark.parametrize("key,message", [
    ("gs://acme-data/in/a[1].csv", "has a glob character"),
    ("gs://acme-data/in/%2e%2e/a.csv", "has a `%`"),
    ("gs://acme-data/in/../a.csv", "goes up a folder"),
    ("gs://acme-data/other/a.csv", "is outside the roots"),
    ("gs://acme-data-evil/in/a.csv", "is outside the roots"),
])
def test_listed_objects_are_checked_like_paths(connector, storage, tmp_path, key, message):
    storage.objects = {"gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id\n1\n")}
    with pytest.raises(AccessError, match=message):
        resolve_files("gs://acme-data/in/*.csv", allowed_roots(ROOTS), lister=lambda url: [
            "gs://acme-data/in/a.csv", key])


def _httpfs_installed():
    connection = duckdb.connect()
    try:
        row = connection.execute("SELECT installed FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
                                 ).fetchone()
        return bool(row and row[0])
    finally:
        connection.close()


HTTPFS = pytest.mark.skipif(not _httpfs_installed(), reason="DuckDB's httpfs extension is not installed (no download "
                            "here)")


@HTTPFS
def test_with_duckdbs_own_httpfs_the_query_never_reaches_the_listener(connector, listener):
    """A real Reader (httpfs, the secret): refused before DuckDB is asked - only local addresses are involved."""
    attack = "gs://acme-data/in/x.csv?s3_endpoint=127.0.0.1:%d&s3_use_ssl=false" % listener.port
    client = connect(connector)
    try:
        for arguments in ({"path": attack}, {"path": attack, "match": ".*"}, {"path": "x.csv?s3_endpoint=127.0.0.1:%d"
                                                                                       % listener.port}):
            with pytest.raises(ConnectorError) as caught:
                list(connector.request(client, call(arguments), context(connector)))
            assert caught.value.code == "ACCESS_DENIED"
        with pytest.raises(AccessError, match="has a `\\?`"):
            list(client.pages(attack, "csv"))
        with pytest.raises(AccessError, match="has a `\\?`"):
            client._list(attack)
        assert not listener.connected()
    finally:
        client.close()
    # why: DuckDB itself honours the query - a bare connection with the same secret sends a signed request there
    bare = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    try:
        bare.execute("LOAD httpfs")
        bare.execute("CREATE SECRET probe (TYPE gcs, KEY_ID ?, SECRET ?, SCOPE ?)",
                     ["GOOG1EPROBE0042", "probe-secret", "gs://acme-data/in/"])
        with pytest.raises(duckdb.Error):
            bare.execute("SELECT * FROM read_csv(?)", [attack]).fetchall()
    finally:
        bare.close()
    assert listener.connected() and b"GOOG1EPROBE0042" in b"".join(listener.requests)


@HTTPFS
def test_duckdb_reads_only_under_the_url_roots_with_external_access_off():
    """Only refused reads: an allowed one would ask Google Cloud Storage - nothing leaves the machine here."""
    roots = allowed_roots(["gs://acme-data/in/"])
    with Reader(roots, credentials=AUTH) as client:
        connection = client.connection
        assert connection.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is False
        assert connection.execute("SELECT name, type, scope FROM duckdb_secrets()").fetchall() == [
            ("streamwright_gcs", "gcs", ["gs://acme-data/in/"])]
        assert SECRET not in connection.execute("SELECT secret_string FROM duckdb_secrets()").fetchone()[0]
        # DuckDB refuses it too, whatever the connector did
        with pytest.raises(duckdb.Error, match="Permission Error"):
            connection.execute("SELECT * FROM read_csv(?)", ["gs://acme-data/other/a.csv"]).fetchall()
        with pytest.raises(Exception):
            connection.execute("SET enable_external_access = true")


# * ------------------------------------
# * connecting: httpfs and the secret
# * ------------------------------------

def test_connect_loads_httpfs_and_sets_one_secret_scoped_to_the_roots_with_bound_parameters(connector, storage):
    storage.installed = False
    client = connect(connector)
    try:
        sql = storage.sql()
        assert sql.index("INSTALL httpfs") < sql.index("LOAD httpfs") < sql.index(
            "SET allowed_directories = ['gs://acme-data/in/', 'gs://lake/raw/']"
        ) < sql.index("SET enable_external_access = false") < sql.index("SET lock_configuration = true")
        secrets = [(statement, parameters) for statement, parameters in storage.statements if "SECRET" in statement]
        assert secrets == [(
            "CREATE OR REPLACE TEMPORARY SECRET streamwright_gcs (TYPE gcs, KEY_ID ?, SECRET ?, SCOPE ?)",
            [KEY_ID, SECRET, ["gs://acme-data/in/", "gs://lake/raw/"]])]
        assert sql.index("LOAD httpfs") < sql.index(secrets[0][0]) < sql.index("SET enable_external_access = false")
        # credentials only ever travel as bound parameters, never in a statement's text
        assert not [statement for statement in sql for value in CREDENTIAL_VALUES if value in statement]
        assert setting(client, "enable_external_access") is False
    finally:
        client.close()
    storage.statements[:] = []
    storage.installed = True
    connect(connector, roots=["gs://acme-data/in/"]).close()  # installed: loaded only
    assert "INSTALL httpfs" not in storage.sql() and "LOAD httpfs" in storage.sql()
    assert [parameters for statement, parameters in storage.statements if "SECRET" in statement] == [
        [KEY_ID, SECRET, ["gs://acme-data/in/"]]]


# * ------------------------------------
# * reading: formats, pages, records
# * ------------------------------------

def test_reads_keep_pages_records_exact_values_and_log_lines(connector, storage, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="streamwright.network")
    storage.objects = {
        "gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id,amount,day\n1,1.50,2026-10-01\n2,,2026-10-02\n"
                                                             "3,%s,2026-10-03\n" % BIG),
        "gs://acme-data/in/b.csv": write(tmp_path / "b.csv", "id,amount,day\n4,4,2026-10-04\n"),
        "gs://acme-data/in/c.jsonl": write(tmp_path / "c.jsonl", '{"id": 5, "v": %s, "tags": ["x"]}\n'
                                                                 '{"id": 6, "v": null}\n' % BIG),
        "gs://acme-data/in/d.tsv": write(tmp_path / "d.tsv", "id\tname\n7\ta,b\n"),
        "gs://acme-data/in/e.json": write(tmp_path / "e.json", '[{"id": 8}, {"id": 9, "nested": {"a": 1.25}}]'),
        "gs://lake/raw/p.parquet": parquet(tmp_path / "p.parquet",
                                           "SELECT 1::BIGINT AS id, %s::DECIMAL(38,6) AS amount, DATE '2026-10-01' "
                                           "AS day, TIMESTAMP '2026-10-01 12:30:00' AS at" % BIG)}
    client = connect(connector)
    try:
        pages = list(connector.request(client, call({"path": "gs://acme-data/in/*.csv", "options": {"filename": True}}),
                                       context(connector)))
        assert pages == [[{"id": "1", "amount": "1.50", "day": "2026-10-01", "filename": "a.csv"},
                          {"id": "2", "amount": None, "day": "2026-10-02", "filename": "a.csv"}],
                         [{"id": "3", "amount": BIG, "day": "2026-10-03", "filename": "a.csv"}],
                         [{"id": "4", "amount": "4", "day": "2026-10-04", "filename": "b.csv"}]]

        def records_of(arguments):
            return [record for page in connector.request(client, call(arguments), context(connector))
                    for record in page]
        assert records_of({"path": "c.jsonl"}) == [{"id": 5, "v": decimal.Decimal(BIG), "tags": ["x"]},
                                                   {"id": 6, "v": None}]
        assert records_of({"path": "d.tsv"}) == [{"id": "7", "name": "a,b"}]
        assert records_of({"path": "e.json"}) == [{"id": 8}, {"id": 9, "nested": {"a": decimal.Decimal("1.25")}}]
        assert records_of({"path": "gs://lake/raw/p.parquet"}) == [
            {"id": 1, "amount": decimal.Decimal(BIG), "day": "2026-10-01", "at": "2026-10-01 12:30:00"}]
        assert records_of({"path": "a.csv", "format": "csv", "options": {"columns": {
            "id": "BIGINT", "amount": "DECIMAL(38,6)", "day": "DATE"}, "header": True}})[2] == {
            "id": 3, "amount": decimal.Decimal(BIG), "day": "2026-10-03"}
    finally:
        client.close()
    lines = [record for record in caplog.records if record.name == "streamwright.network"]
    assert [line.getMessage().rsplit(",", 1)[0] for line in lines[:2]] == [
        "gcs read gs://acme-data/in/a.csv: 3 row(s)", "gcs read gs://acme-data/in/b.csv: 1 row(s)"]
    assert (lines[0].connector, lines[0].path, lines[0].records, lines[0].call) == (
        "gcs", "gs://acme-data/in/a.csv", 3, "object.read")


def test_an_unknown_extension_needs_a_format(connector, storage, tmp_path):
    storage.objects = {"gs://acme-data/in/a.txt": write(tmp_path / "a.txt", "id\n1\n")}
    with pytest.raises(ConnectorError, match="cannot tell the format of gs://acme-data/in/a.txt"):
        read(connector, {"path": "a.txt"})
    assert read(connector, {"path": "a.txt", "format": "csv"}) == [{"id": "1"}]


def test_a_url_naming_a_folder_is_an_error(connector, storage):
    with pytest.raises(ConnectorError, match="is a folder: name its objects") as caught:
        read(connector, {"path": "gs://acme-data/in/sub/"})
    assert caught.value.code == "READ_ERROR" and nothing_read(storage)


# * ------------------------------------
# * which objects: globs and `match`
# * ------------------------------------

@pytest.fixture
def lake(storage, tmp_path):
    """Objects under gs://acme-data/in/: a.csv, b.csv, notes.txt, _SUCCESS, sub/c.csv, sub/deep/d.csv, x/e.csv."""
    for key, number in (("a.csv", 1), ("b.csv", 2), ("sub/c.csv", 3), ("sub/deep/d.csv", 4), ("x/e.csv", 5)):
        write(tmp_path / "lake" / key, "id\n%d\n" % number)
    write(tmp_path / "lake" / "notes.txt", "not a csv\n")
    write(tmp_path / "lake" / "_SUCCESS", "")
    storage.serve(tmp_path / "lake", "gs://acme-data/in/")
    storage.objects["gs://acme-data/in/sub/"] = str(tmp_path / "lake" / "sub")  # a folder marker
    return storage


@pytest.mark.parametrize("path,ids", [
    ("*.csv", ["1", "2"]),
    ("gs://acme-data/in/[a]*.csv", ["1"]),
    ("sub/*.csv", ["3"]),
    ("**/*.csv", ["1", "2", "3", "4", "5"]),
    ("sub/**/*.csv", ["3", "4"]),
    ("*/*.csv", ["3", "5"]),
    ("nothing/*.csv", []),
])
def test_globs_list_objects_inside_the_root(connector, lake, path, ids):
    assert [record["id"] for record in read(connector, {"path": path, "format": "csv"})] == ids


@pytest.mark.parametrize("arguments,ids,listed", [
    ({"path": "gs://acme-data/in/", "match": r".*\.csv"}, ["1", "2"], "gs://acme-data/in/*"),
    ({"path": "gs://acme-data/in", "match": r"[ab]\.csv"}, ["1", "2"], "gs://acme-data/in/*"),
    ({"path": "sub", "match": r".*\.csv"}, ["3"], "gs://acme-data/in/sub/*"),
    ({"path": "gs://acme-data/in/", "match": r".*\.csv", "recursive": True}, ["1", "2", "3", "4", "5"],
     "gs://acme-data/in/**"),
    ({"path": "gs://acme-data/in/", "match": r"sub/.*\.csv", "recursive": True}, ["3", "4"], "gs://acme-data/in/**"),
    ({"path": "gs://acme-data/in/", "match": r"sub/[^/]*\.csv", "recursive": True}, ["3"], "gs://acme-data/in/**"),
    ({"path": "gs://acme-data/in/", "match": r"sub/.*\.csv"}, [], "gs://acme-data/in/*"),  # not recursive
    ({"path": "gs://acme-data/in/", "match": r"csv"}, [], "gs://acme-data/in/*"),  # fullmatch, not search
    ({"path": "gs://acme-data/in/", "match": r"(b|a)\.csv", "recursive": False}, ["1", "2"], "gs://acme-data/in/*"),
])
def test_match_selects_objects_whose_relative_key_fully_matches_in_key_order(connector, lake, arguments, ids, listed):
    arguments = dict(arguments, format="csv")
    assert [record["id"] for record in read(connector, arguments)] == ids
    assert lake.listed == [listed]


def test_match_reads_the_listed_objects_with_their_keys(connector, lake):
    assert read(connector, {"path": "gs://acme-data/in/sub/", "match": r".*", "recursive": True,
                            "options": {"filename": True}}) == [
        {"id": "3", "filename": "sub/c.csv"}, {"id": "4", "filename": "sub/deep/d.csv"}]


@pytest.mark.parametrize("arguments,message,code", [
    ({"path": "gs://acme-data/other/", "match": r".*"}, "is outside the roots", "ACCESS_DENIED"),
    ({"path": "gs://acme-data/in/../", "match": r".*"}, "goes up a folder", "ACCESS_DENIED"),
    ({"path": "gs://acme-data-evil/in/", "match": r".*"}, "is outside the roots", "ACCESS_DENIED"),
    ({"path": "gs://acme-data/in/?s3_endpoint=evil.example.com", "match": r".*"}, "has a `\\?`", "ACCESS_DENIED"),
    ({"path": "gs://acme-data/in/*/", "match": r".*"}, "is a glob: with `match`", "READ_ERROR"),
])
def test_match_folders_are_checked_before_any_listing(connector, lake, arguments, message, code):
    with pytest.raises(ConnectorError, match=message) as caught:
        list(connector.request(connect(connector), call(arguments), context(connector)))
    assert caught.value.code == code
    assert nothing_read(lake)


def test_match_with_a_lister_returning_objects_outside_the_root_is_refused():
    with pytest.raises(AccessError, match="the listed object gs://acme-data/other/a.csv is outside the roots"):
        match_files("gs://acme-data/in/", allowed_roots(ROOTS), r".*", lister=lambda url: [
            "gs://acme-data/in/a.csv", "gs://acme-data/other/a.csv"])
    # objects outside the folder (but inside a root) are not selected
    assert match_files("gs://acme-data/in/sub/", allowed_roots(ROOTS), r".*", lister=lambda url: [
        "gs://acme-data/in/a.csv", "gs://acme-data/in/sub/b.csv"]) == [("gs://acme-data/in/sub/b.csv",
                                                                        "gs://acme-data/in/")]


# * --------------
# * missing objects
# * --------------

def test_a_missing_object_is_skipped_with_a_warning_or_fails_with_on_missing_error(connector, storage, tmp_path,
                                                                                    caplog):
    storage.objects = {"gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id\n1\n")}
    assert read(connector, {"path": ["gs://acme-data/in/a.csv", "gs://acme-data/in/b.csv"]}) == [{"id": "1"}]
    assert "gcs: no object matches 'gs://acme-data/in/b.csv'; skipping it (on_missing: skip)" in caplog.text
    assert read(connector, {"path": "gs://acme-data/in/2026-*.csv"}) == []  # a glob that matches nothing
    assert read(connector, {"path": "gs://acme-data/in/", "match": r"z.*"}) == []  # nothing matches
    assert "gcs: no object matches 'gs://acme-data/in/'; skipping it" in caplog.text
    for arguments in ({"path": "b.csv"}, {"path": "*.parquet"}, {"path": "gs://acme-data/in/", "match": "z.*"}):
        with pytest.raises(ConnectorError, match="no object matches") as caught:
            read(connector, dict(arguments, on_missing="error"))
        assert caught.value.code == "NOT_FOUND"


# * ----------------------------------
# * checks: auth and requests
# * ----------------------------------

GOOD_AUTH = {"roots": ["gs://acme-data/in"], "key_id": "{{ secrets.gcs_key_id }}", "secret": "{{secrets.gcs_secret}}"}


@pytest.mark.parametrize("auth,expected", [
    (GOOD_AUTH, []),
    (dict(GOOD_AUTH, roots=["{{ config.bucket_root }}"]), []),
    (dict(GOOD_AUTH, roots="{{ config.roots }}"), []),
    (dict(GOOD_AUTH, roots=["gs://{{ config.bucket }}/exports/"]), []),
    ({"roots": ["gs://a/"]}, ["auth: provider 'gcs' needs 'key_id'", "auth: provider 'gcs' needs 'secret'"]),
    (dict(GOOD_AUTH, key_id="{{ config.k }}", secret="x{{ secrets.s }}"),
     ["auth: provider 'gcs': `key_id` must be one secret reference, e.g. \"{{ secrets.gcs_key_id }}\": credentials "
      "are never written in a source or its config",
      "auth: provider 'gcs': `secret` must be one secret reference, e.g. \"{{ secrets.gcs_secret }}\": credentials "
      "are never written in a source or its config"]),
    (dict(GOOD_AUTH, key_id="", secret=None),
     ["auth: provider 'gcs': `key_id` must be a secret reference, e.g. \"{{ secrets.gcs_key_id }}\"",
      "auth: provider 'gcs': `secret` must be a secret reference, e.g. \"{{ secrets.gcs_secret }}\""]),
    (dict(GOOD_AUTH, region="us-east-1", session_token="{{ secrets.t }}"),
     ["auth: provider 'gcs' does not support 'region' (supported: roots, key_id, secret)",
      "auth: provider 'gcs' does not support 'session_token' (supported: roots, key_id, secret)"]),
    (dict(GOOD_AUTH, roots=["gs://acme-data/in/*.csv"]),
     ["auth: provider 'gcs': `roots`: 'gs://acme-data/in/*.csv' is a glob: a root is a folder (a URL prefix)"]),
    (dict(GOOD_AUTH, roots=["gs://acme-data/in/?s3_endpoint=x"]),
     ["auth: provider 'gcs': `roots`: 'gs://acme-data/in/?s3_endpoint=x' has a `?`: object storage URLs take no "
      "query - DuckDB would read its parameters (e.g. ?s3_endpoint=, ?s3_access_key_id=) as connection settings; "
      "`?` is not a glob character here"]),
    (dict(GOOD_AUTH, roots=["gs://u:{{ secrets.pw }}@acme/"]),
     ["auth: provider 'gcs': `roots` cannot use secrets: roots are URL prefixes, not credentials"]),
    (dict(GOOD_AUTH, roots=["/data/in"]), ["auth: provider 'gcs': `roots`: '/data/in' is not a gs:// URL prefix"]),
    (dict(GOOD_AUTH, roots="gs://acme/"),
     ["auth: provider 'gcs': `roots` must be a list of gs:// URL prefixes, e.g. [\"gs://bucket/prefix/\"]"]),
    (dict(GOOD_AUTH, roots=[]), ["auth: provider 'gcs': `roots` must be a non-empty list of gs:// URL prefixes"]),
])
def test_auth_is_checked(connector, auth, expected):
    assert connector.check_auth(auth) == expected


@pytest.mark.parametrize("arguments,expected", [
    ({"path": "gs://acme-data/in/{{ window.start }}.csv"}, []),
    ({"path": "{{ partition.keys }}"}, []),
    ({"path": ["a.csv", "b/{{ partition.day }}.jsonl"], "format": "auto", "on_missing": "error"}, []),
    ({"path": "events/", "match": r"day=\d+/part-\d+\.parquet", "recursive": True, "format": "parquet"}, []),
    ({"path": "orders/*.csv", "options": {"header": True, "delimiter": ";", "filename": True}}, []),
    ({}, ["gcs: object.read needs `path`"]),
    ({"path": "a.csv", "query": "x"},
     ["gcs: object.read does not take `query` (arguments: path, format, options, on_missing, match, recursive)"]),
    ({"path": "a.csv", "format": "xlsx"},
     ["gcs: unknown `format` 'xlsx' (formats: auto, csv, tsv, json, jsonl, parquet)"]),
    ({"path": "a.csv", "format": "parquet", "options": {"header": True}},
     ["gcs: parquet files have no option 'header' (options: filename)"]),
    ({"path": "a.csv", "on_missing": "ignore"}, ["gcs: unknown `on_missing` policy 'ignore' (policies: skip, error)"]),
    ({"path": "a.csv?x=1"},
     ["gcs: path 'a.csv?x=1' has a `?`: object storage URLs take no query - DuckDB would read its parameters (e.g. "
      "?s3_endpoint=, ?s3_access_key_id=) as connection settings; `?` is not a glob character here"]),
    ({"path": "gs://acme-data/in/{{ window.start }}.csv?s3_region=x"},
     ["gcs: path 'gs://acme-data/in/{{ window.start }}.csv?s3_region=x' has a `?`: object storage URLs take no "
      "query - DuckDB would read its parameters (e.g. ?s3_endpoint=, ?s3_access_key_id=) as connection settings; "
      "`?` is not a glob character here"]),
    ({"path": "../a.csv"}, ["gcs: path '../a.csv' goes up a folder (..)"]),
    ({"path": "{{ secrets.gcs_secret }}"},
     ["gcs: arguments cannot use secrets (paths and options are not credentials: credentials go in the auth block)"]),
    ({"path": "events/", "match": "("}, ["gcs: `match` '(' is not a valid regex: missing ), unterminated subpattern "
                                         "at position 0"]),
    ({"path": "events/", "match": "{{ config.pattern }}"},
     ["gcs: `match` '{{ config.pattern }}' is a literal regex: it takes no references"]),
    ({"path": "events/", "match": ""}, ["gcs: `match` '' must be a non-empty text (a regex)"]),
    ({"path": "events/*", "match": ".*"}, ["gcs: path 'events/*' is a glob: with `match`, `path` is the folder to "
                                           "look in"]),
    ({"path": "events/", "match": ".*", "recursive": "yes"}, ["gcs: `recursive` must be true or false (a literal)"]),
    ({"path": "events/", "recursive": True}, ["gcs: `recursive` goes with `match` (the folder to look in is `path`)"]),
    ({"path": []}, ["gcs: `path` is an empty list"]),
    ({"path": 3}, ["gcs: `path` must be a gs:// URL (or a key relative to the first root), a glob or a list of "
                   "them"]),
])
def test_requests_are_checked(connector, arguments, expected):
    assert connector.check_request(call(arguments)) == expected


@pytest.mark.parametrize("request_,expected", [
    ({"service": "bucket", "method": "read"}, ["gcs: service 'bucket' is not supported (supported: object)"]),
    ({"service": "object", "method": "write", "arguments": {"path": "a"}},
     ["gcs: object.write is not supported (supported: read)"]),
    ({"method": "read", "arguments": {"path": "a.csv"}}, []),  # service defaults to object
    ({"method": "read", "arguments": ["a.csv"]}, ["gcs: `arguments` must be a mapping"]),
])
def test_services_and_methods_are_checked(connector, request_, expected):
    assert connector.check_request(request_) == expected


def test_a_source_validates_without_connecting_or_reading(connector, storage):
    source = gcs_source(page_stream("orders", sdk("raw", path="gs://acme-data/in/{{ partition.day }}/*.csv"),
                                   "SELECT record->>'id' AS id FROM raw"))
    assert components.check_source(source, ["gcs"]) == []
    assert storage.statements == []
    source["auth"]["secret"] = "{{ config.gcs_secret }}"
    source["streams"][0]["requests"][0]["arguments"]["path"] = "a.csv?s3_endpoint=x"
    problems = components.check_source(source, ["gcs"])
    assert problems[0] == ("auth: provider 'gcs': `secret` must be one secret reference, e.g. "
                           "\"{{ secrets.gcs_secret }}\": credentials are never written in a source or its config")
    assert problems[1].startswith("stream 'orders': requests[0]: gcs: path 'a.csv?s3_endpoint=x' has a `?`")
    assert storage.statements == []


# * ----------------------------------
# * credentials: secrets and redaction
# * ----------------------------------

def test_a_credential_written_in_the_source_is_refused_before_duckdb_sees_it(connector, storage):
    written = "wJalrXUtnFEMIwrittenInTheSource"
    ctx = context(connector)
    with pytest.raises(ConnectorError) as caught:
        connect(connector, ctx=ctx, key_id="GOOG1ELITERALKEY0042", secret=written)
    assert str(caught.value) == (
        "gcs: `key_id`, `secret` must come from secrets: write e.g. \"{{ secrets.gcs_key_id }}\" in the "
        "source's auth and pass the value as a secret (credentials are never written in a source)")
    assert storage.statements == []  # no DuckDB connection was opened
    assert ctx.redact(written) == "***" and ctx.redact("GOOG1ELITERALKEY0042") == "***"  # masked anyway


def test_credentials_are_masked_even_when_refused(connector, storage):
    ctx = ConnectorContext(connector, Redactor())  # a redactor that does not know them as the run's secrets
    with pytest.raises(ConnectorError, match="must come from secrets"):
        connect(connector, ctx=ctx)
    assert ctx.redact(" ".join(CREDENTIAL_VALUES)) == "*** ***"
    assert ctx.redact("gs://acme-data/in/") == "gs://acme-data/in/"
    assert storage.statements == []


def echo(*values):
    """A DuckDB error that echoes credentials (as some messages echo what they were given)."""
    return duckdb.IOException("IO Error: request failed (key_id=%s, secret=%s, extra=%s)" % (values + ("",) * 3)[:3])


def test_credentials_never_appear_in_connect_errors(connector, storage):
    storage.errors["SECRET"] = echo(KEY_ID, SECRET)
    with pytest.raises(ConnectorError) as caught:
        connect(connector)
    assert str(caught.value) == ("gcs: cannot set the gcs credentials: IO Error: request failed (key_id=***, "
                                 "secret=***, extra=)")
    storage.errors = {"LOAD": echo(KEY_ID, SECRET)}
    with pytest.raises(ConnectorError, match="cannot load DuckDB's httpfs extension") as caught:
        connect(connector)
    assert not [value for value in CREDENTIAL_VALUES if value in str(caught.value)]


def test_credentials_never_appear_in_read_or_list_errors(connector, storage, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    storage.objects = {"gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id\n1\n")}
    storage.errors["READ"] = echo(KEY_ID, SECRET, KEY_ID)
    with pytest.raises(ConnectorError) as caught:
        read(connector, {"path": "gs://acme-data/in/a.csv"})
    assert caught.value.code == "READ_ERROR"
    assert str(caught.value) == ("gcs: cannot read gs://acme-data/in/a.csv as csv: IO Error: request failed "
                                 "(key_id=***, secret=***, extra=***)")
    storage.errors = {"GLOB": echo(KEY_ID, SECRET)}
    with pytest.raises(ConnectorError, match=r"cannot list gs://acme-data/in/\*.csv: .*key_id=\*\*\*, secret=\*\*\*"):
        read(connector, {"path": "*.csv"})
    assert not [value for value in CREDENTIAL_VALUES if value in caplog.text]


def test_a_forced_error_never_shows_the_credentials_in_logs_errors_or_the_summary(connector, storage, tmp_path, caplog,
                                                                                  capsys, monkeypatch):
    """The CLI: secrets from the environment, a read error that echoes them, --summary written."""
    caplog.set_level(logging.DEBUG)
    lake = tmp_path / "lake"
    write(lake / "orders" / "east.csv", "order_id,quantity,amount,ordered_on\n1,2,3.50,2026-10-01\n")
    storage.serve(lake, "gs://acme-data/exports/")
    storage.errors["READ"] = echo(KEY_ID, SECRET)
    monkeypatch.setenv("STREAMWRIGHT_SECRET_GCS_KEY_ID", KEY_ID)
    monkeypatch.setenv("STREAMWRIGHT_SECRET_GCS_SECRET", SECRET)
    summary = tmp_path / "summary.json"
    assert cli.main(["run", EXAMPLE, "--allow-connector", "gcs", "--stream", "orders", "--output",
                     "jsonl:%s" % (tmp_path / "out"), "--summary", str(summary)]) != 0
    captured = capsys.readouterr()
    texts = [caplog.text, captured.out, captured.err, summary.read_text()]
    assert "key_id=***, secret=***" in caplog.text + captured.err
    assert json.loads(texts[-1])["status"] == "failed"
    for text in texts:
        assert not [value for value in CREDENTIAL_VALUES if value in text]
    assert cli.main(["connectors"]) == 0
    assert not [value for value in CREDENTIAL_VALUES if value in capsys.readouterr().out]


def test_a_run_never_logs_or_raises_the_credentials(connector, storage, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    storage.objects = {"gs://acme-data/in/a.csv": write(tmp_path / "a.csv", "id\n1\n")}
    source = gcs_source(page_stream("orders", sdk("raw", path="a.csv"),
                                   "SELECT (record->>'id')::BIGINT AS id FROM raw", primary_key=["id"]))
    assert [record for _, record in run(source).records] == [{"id": 1}]
    storage.errors["READ"] = echo(KEY_ID, SECRET)
    with pytest.raises(SourceError) as caught:
        run(source)
    assert "key_id=***, secret=***" in str(caught.value)
    secret_statements = [parameters for statement, parameters in storage.statements if "SECRET" in statement]
    assert secret_statements and all(KEY_ID in parameters and SECRET in parameters for parameters in secret_statements)
    for text in (str(caught.value), caplog.text):
        assert not [value for value in CREDENTIAL_VALUES if value in text]


def test_errors_map_to_connector_errors(connector):
    assert connector.error(duckdb.IOException("boom")).code == "READ_ERROR"
    assert connector.error(AccessError("no")).code == "ACCESS_DENIED"
    assert connector.error(reader.MissingError("gone")).code == "NOT_FOUND"
    assert connector.error(ValueError("x")) is None


# * --------------------
# * runs: SourceRunner
# * --------------------

def test_partitions_read_an_object_each_and_count_one_call_per_object(connector, storage, tmp_path):
    write(tmp_path / "lake" / "east.csv", "id,amount\n1,%s\n2,2.50\n" % BIG)
    write(tmp_path / "lake" / "west.csv", "id,amount\n3,7\n")
    storage.serve(tmp_path / "lake", "gs://acme-data/in/")
    stream = page_stream("orders", sdk("raw_orders", path="{{ partition.region }}.csv"),
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'amount')::DECIMAL(38,6) AS amount, "
                         "partition->>'region' AS region FROM raw_orders", primary_key=["id"],
                         partitions=[{"name": "region", "values": ["east", "west", "north"]}])
    metrics = RunMetrics()
    output = run(gcs_source(stream), metrics=metrics)
    assert records(output, "orders") == [{"id": 1, "amount": decimal.Decimal(BIG), "region": "east"},
                                         {"id": 2, "amount": decimal.Decimal("2.5"), "region": "east"},
                                         {"id": 3, "amount": decimal.Decimal("7"), "region": "west"}]
    stats = metrics.summary()["streams"][0]
    assert stats["records_read"] == 3 and stats["requests"] == {"raw_orders": 3}  # the missing one asked too


def test_windows_read_an_object_per_day_and_bookmarks_advance(connector, storage, tmp_path):
    for day in ("2026-10-01", "2026-10-03"):
        write(tmp_path / "lake" / "daily" / (day + ".jsonl"), '{"id": %d, "day": "%s"}\n' % (int(day[-1]), day))
    storage.serve(tmp_path / "lake", "gs://acme-data/in/")
    stream = page_stream("daily", sdk("raw_daily", path="daily/{{ window.start }}.jsonl"),
                         "SELECT (record->>'id')::BIGINT AS id, (record->>'day')::DATE AS day, window_start "
                         "FROM raw_daily", primary_key=["id"],
                         incremental={"cursor_field": "day", "start": "2026-10-01", "window": "1d"})
    output = run(gcs_source(stream), today=datetime.date(2026, 10, 5))  # the last complete day is 2026-10-04
    assert records(output, "daily") == [{"id": 1, "day": "2026-10-01", "window_start": "2026-10-01"},
                                        {"id": 3, "day": "2026-10-03", "window_start": "2026-10-03"}]
    assert output.states[-1]["bookmarks"]["daily"] == {"{}": "2026-10-04"}


def test_batches_of_keys_from_an_earlier_request_and_records_explode(connector, storage, tmp_path):
    write(tmp_path / "lake" / "files.csv", "key\nparts/a.jsonl\nparts/b.jsonl\nparts/c.jsonl\n")
    write(tmp_path / "lake" / "parts" / "a.jsonl", '{"id": 1, "tags": [{"tag": "a"}, {"tag": "b"}]}\n')
    write(tmp_path / "lake" / "parts" / "b.jsonl", '{"id": 2, "tags": [{"tag": "c"}]}\n')
    write(tmp_path / "lake" / "parts" / "c.jsonl", '{"id": 3, "tags": []}\n')
    storage.serve(tmp_path / "lake", "gs://acme-data/in/")
    parts = sdk("raw_parts", path="{{ partition.keys }}")
    parts.update(partitions=[{"name": "keys", "from": "raw_files", "field": "key", "batch_size": 2}],
                 records={"explode": "tags"})
    stream = {"name": "tags", "requests": [sdk("raw_files", path="files.csv"), parts],
              "transform": {"mode": "run", "steps": [{"name": "tags", "select": (
                  "SELECT (record->>'id')::BIGINT AS id, record->>'tag' AS tag, "
                  "json_array_length(partition->'keys') AS batch FROM raw_parts")}]},
              "export": {"tags": {"step": "tags", "primary_key": ["tag"]}}}
    metrics = RunMetrics()
    output = run(gcs_source(stream), metrics=metrics)
    assert records(output, "tags") == [{"id": 1, "tag": "a", "batch": 2}, {"id": 1, "tag": "b", "batch": 2},
                                       {"id": 2, "tag": "c", "batch": 2}]
    assert metrics.summary()["streams"][0]["requests"] == {"raw_files": 1, "raw_parts": 3}


# * -------------------------
# * the example and the CLI
# * -------------------------

def serve_example(storage, folder):
    """Objects for the gcs_demo example under its default root, gs://acme-data/exports/."""
    write(folder / "orders" / "east.csv", "order_id,quantity,amount,ordered_on\n1001,2,19.98,2026-10-01\n"
                                          "1002,1,%s,2026-10-02\n" % "99999999.99")
    write(folder / "orders" / "west.csv", "order_id,quantity,amount,ordered_on\n2001,5,5.00,2026-10-01\n")
    for day, start in (("2026-10-01", 1), ("2026-10-02", 3)):
        parquet(folder / "events" / ("day=" + day) / "part-0.parquet",
                "SELECT * FROM (VALUES (%d, 'view', 1.25::DECIMAL(12,2)), (%d, 'buy', 10.50::DECIMAL(12,2))) "
                "t(event_id, kind, value)" % (start, start + 1))
    write(folder / "events" / "day=2026-10-01" / "_SUCCESS", "")
    write(folder / "events" / "README.txt", "not an event")
    storage.serve(folder, "gs://acme-data/exports/")


def test_the_gcs_demo_runs_offline_with_the_stand_in(storage, tmp_path, monkeypatch):
    components.unregister("gcs")  # the installed entry point, as streamwright run loads it
    serve_example(storage, tmp_path / "lake")
    monkeypatch.setenv("STREAMWRIGHT_SECRET_GCS_KEY_ID", KEY_ID)
    monkeypatch.setenv("STREAMWRIGHT_SECRET_GCS_SECRET", SECRET)
    out = tmp_path / "out"
    assert cli.main(["run", EXAMPLE, "--allow-connector", "gcs", "--output", "jsonl:%s" % out]) == 0
    written = {}
    for name in os.listdir(str(out)):
        if name != "state.json":
            with open(str(out / name)) as stream:
                written[name.split(".")[0]] = [json.loads(line) for line in stream]
    assert sorted(written) == ["events", "orders"]
    assert [(record["order_id"], record["region"], record["amount"]) for record in written["orders"]] == [
        (1001, "east", 19.98), (1002, "east", 99999999.99), (2001, "west", 5)]
    assert written["events"] == [
        {"event_id": 1, "kind": "view", "value": 1.25, "day": "2026-10-01"},
        {"event_id": 2, "kind": "buy", "value": 10.5, "day": "2026-10-01"},
        {"event_id": 3, "kind": "view", "value": 1.25, "day": "2026-10-02"},
        {"event_id": 4, "kind": "buy", "value": 10.5, "day": "2026-10-02"}]
    secret = [parameters for statement, parameters in storage.statements if "SECRET" in statement][0]
    assert secret == [KEY_ID, SECRET, ["gs://acme-data/exports/"]]


def test_streamwright_validate_and_connectors(storage, capsys, tmp_path):
    assert cli.main(["validate", EXAMPLE]) == 0
    assert storage.statements == []  # validation never connects
    assert cli.main(["connectors"]) == 0
    assert "gcs" in capsys.readouterr().out.split()
    with open(os.path.join(EXAMPLE, "source.yaml")) as stream:
        document = yaml.safe_load(stream)
    with open(os.path.join(EXAMPLE, "streams", "orders.yaml")) as stream:
        orders = yaml.safe_load(stream)
    orders["requests"][0]["arguments"]["path"] = "orders/east.csv?s3_endpoint=evil.example.com"
    document["streams"] = [dict(orders, name="orders")]
    document["auth"]["secret"] = "wJalrXUtnFEMIwrittenInTheSource"  # (connect() refuses it: it is no secret)
    bad = tmp_path / "source.yaml"
    bad.write_text(yaml.safe_dump(document, sort_keys=False))
    assert cli.main(["validate", str(bad)]) == 1
    assert "has a `?`: object storage URLs take no query" in capsys.readouterr().out


# * --------------------------------------------
# * real object storage (STREAMWRIGHT_TEST_GCS only)
# * --------------------------------------------

@pytest.mark.skipif(not os.environ.get("STREAMWRIGHT_TEST_GCS"), reason="set STREAMWRIGHT_TEST_GCS=gs://bucket/prefix/ with "
                    "STREAMWRIGHT_TEST_GCS_KEY_ID, STREAMWRIGHT_TEST_GCS_SECRET (an HMAC key; and STREAMWRIGHT_TEST_GCS_PATH) to read "
                    "real object storage")
def test_real_object_storage(connector):
    auth = dict((key, os.environ["STREAMWRIGHT_TEST_GCS_" + key.upper()]) for key in (
        "key_id", "secret") if os.environ.get("STREAMWRIGHT_TEST_GCS_" + key.upper()))
    ctx = ConnectorContext(connector, Redactor([auth.get("key_id"), auth.get("secret")]))
    client = connector.connect(dict(auth, roots=[os.environ["STREAMWRIGHT_TEST_GCS"]]), ctx)
    try:
        records_read = [record for page in connector.request(client, call({
            "path": os.environ.get("STREAMWRIGHT_TEST_GCS_PATH", "*.csv")}), ctx) for record in page]
    finally:
        client.close()
    assert isinstance(records_read, list)
