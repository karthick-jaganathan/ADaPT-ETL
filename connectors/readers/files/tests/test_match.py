"""The files connector's `match` (a regex over the files of a folder) and `recursive` arguments."""

import glob
import logging
import os

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("adapt.connectors.files.connector")

from adapt.connectors.files.connector import FilesConnector  # noqa: E402
from adapt.connectors.files.reader import AccessError, ReadError, Reader, match_files  # noqa: E402
from adapt.core.runtime import components  # noqa: E402
from adapt.core.net.http import Redactor  # noqa: E402
from adapt.core.runtime.components import ConnectorContext, ConnectorError  # noqa: E402
from adapt.core.engine.runner import SourceRunner  # noqa: E402
from adapt.core.runtime.testing import MemoryOutput, page_stream  # noqa: E402

DAILY = r"^orders_\d{4}-\d{2}-\d{2}\.csv$"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


@pytest.fixture
def connector():
    component = FilesConnector()
    components.register(component)
    yield component
    components.unregister("files")


@pytest.fixture
def tree(tmp_path):
    """A root with daily order files in data/, its sub-folders, and files the daily regex must not select."""
    root = tmp_path / "root"
    for name, number in (("orders_2026-10-02.csv", 2), ("orders_2026-10-01.csv", 1), ("orders_latest.csv", 9),
                         ("orders_2026-10-02.csv.bak", 8), ("2026/orders_2026-09-30.csv", 0),
                         ("2026/deep/orders_2026-09-29.csv", -1), ("2026/notes.txt", 7)):
        write(root / "data" / name, "id\n%d\n" % number)
    return root


def context(connector):
    result = ConnectorContext(connector, Redactor())
    result.where = None
    return result


def read(connector, root, arguments):
    client = connector.connect({"roots": [str(root)]}, context(connector))
    try:
        return [record for page in connector.request(client, {"service": "file", "method": "read",
                                                              "arguments": arguments}, context(connector))
                for record in page]
    finally:
        client.close()


def ids(records):
    return [int(record["id"]) for record in records]


def check(connector, **arguments):
    return connector.check_request({"service": "file", "method": "read", "arguments": arguments})


# * ---------
# * selecting
# * ---------

def test_match_reads_the_files_of_the_folder_whose_names_fully_match_sorted(connector, tree):
    assert ids(read(connector, tree, {"path": "data", "match": DAILY})) == [1, 2]
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_\d{4}-\d{2}-\d{2}\.csv"})) == [1, 2]
    assert ids(read(connector, tree, {"path": str(tree / "data"), "match": DAILY})) == [1, 2]  # absolute, in a root


def test_match_is_a_fullmatch_not_a_search(connector, tree):
    assert read(connector, tree, {"path": "data", "match": "orders_2026"}) == []  # a search would find 3 files
    assert read(connector, tree, {"path": "data", "match": r"2026-10-02\.csv"}) == []
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_.*\.csv", "format": "csv"})) == [1, 2, 9]
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_.*", "format": "csv"})) == [1, 2, 8, 9]


def test_match_selects_a_subset(connector, tree):
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_2026-10-0[2-9]\.csv"})) == [2]
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_(latest|2026-10-01)\.csv"})) == [1, 9]


def test_recursive_matches_paths_relative_to_the_folder_and_defaults_to_false(connector, tree):
    anywhere = r"(.*/)?orders_\d{4}-\d{2}-\d{2}\.csv"
    assert ids(read(connector, tree, {"path": "data", "match": anywhere})) == [1, 2]  # recursive: false by default
    assert ids(read(connector, tree, {"path": "data", "match": anywhere, "recursive": False})) == [1, 2]
    # sorted by the path relative to the folder: 2026/deep/..., 2026/orders_..., orders_...
    assert ids(read(connector, tree, {"path": "data", "match": anywhere, "recursive": True})) == [-1, 0, 1, 2]
    # the regex sees the sub-folders: a name-only regex does not select files in them
    assert ids(read(connector, tree, {"path": "data", "match": DAILY, "recursive": True})) == [1, 2]
    assert ids(read(connector, tree, {"path": "data", "match": r"2026/[^/]+\.csv", "recursive": True})) == [0]
    assert ids(read(connector, tree, {"path": "data/2026", "match": r".*\.csv", "recursive": True})) == [-1, 0]


def test_matched_files_are_read_like_a_file_list(connector, tree):
    records = read(connector, tree, {"path": "data", "match": DAILY, "options": {"filename": True}})
    assert records == [{"id": "1", "filename": os.path.join("data", "orders_2026-10-01.csv")},
                       {"id": "2", "filename": os.path.join("data", "orders_2026-10-02.csv")}]
    with Reader([os.path.realpath(str(tree))]) as client:
        assert client.resolve("data", match=DAILY) == [
            (os.path.realpath(str(tree / "data" / name)), os.path.realpath(str(tree)))
            for name in ("orders_2026-10-01.csv", "orders_2026-10-02.csv")]
    assert match_files(".", [os.path.realpath(str(tree))], r"data/orders_latest\.csv", recursive=True) == [
        (os.path.realpath(str(tree / "data" / "orders_latest.csv")), os.path.realpath(str(tree)))]


def test_a_stream_reads_the_matching_files(connector, tree):
    stream = page_stream("orders", {"name": "raw_orders", "sdk": "files", "method": "read",
                                    "arguments": {"path": "{{ config.folder }}", "match": DAILY}},
                         "SELECT (record->>'id')::BIGINT AS id FROM raw_orders", primary_key=["id"])
    source = {"kind": "source", "name": "files", "auth": {"provider": "files", "roots": ["{{ config.root }}"]},
              "streams": [stream]}
    assert components.check_source(source, ["files"]) == []
    output = MemoryOutput()
    SourceRunner(source, {"root": str(tree), "folder": "data"}, {}, output=output, sleep=lambda seconds: None,
                 allowed_connectors=["files"]).run()
    assert [record["id"] for _, record in output.records] == [1, 2]


# * -----------------------
# * roots: what can be read
# * -----------------------

@pytest.fixture
def spied(monkeypatch):
    reads = []
    pages = Reader.pages

    def spy(self, path, *args, **kwargs):
        reads.append(path)
        return pages(self, path, *args, **kwargs)
    monkeypatch.setattr(Reader, "pages", spy)
    return reads


def test_a_matched_symbolic_link_leaving_the_root_is_refused_before_any_read(connector, tree, tmp_path, spied):
    secret = write(tmp_path / "outside" / "secret.csv", "id\n666\n")
    os.symlink(secret, str(tree / "data" / "orders_2026-10-03.csv"))
    with pytest.raises(ConnectorError, match="leads outside the roots files can be read from .* through a symbolic "
                                             "link") as caught:
        read(connector, tree, {"path": "data", "match": DAILY})
    assert caught.value.code == "ACCESS_DENIED" and spied == []
    # a link the regex does not select is not refused (nor read)
    assert ids(read(connector, tree, {"path": "data", "match": r"orders_2026-10-0[12]\.csv"})) == [1, 2]
    assert [os.path.basename(path) for path in spied] == ["orders_2026-10-01.csv", "orders_2026-10-02.csv"]


def test_recursive_does_not_follow_links_to_folders(connector, tree, tmp_path):
    write(tmp_path / "outside" / "orders_2026-01-01.csv", "id\n666\n")
    os.symlink(str(tmp_path / "outside"), str(tree / "data" / "linked"))
    assert ids(read(connector, tree, {"path": "data", "match": r"(.*/)?orders_\d{4}-\d{2}-\d{2}\.csv",
                                      "recursive": True})) == [-1, 0, 1, 2]


@pytest.mark.parametrize("path,message", [
    ("../outside", "goes up a folder"),
    ("data/../..", "goes up a folder"),
    ("OUTSIDE", "is outside the roots"),
    ("linked", "is outside the roots"),
])
def test_a_folder_outside_the_roots_is_refused(connector, tree, tmp_path, spied, path, message):
    write(tmp_path / "outside" / "orders_2026-01-01.csv", "id\n666\n")
    os.symlink(str(tmp_path / "outside"), str(tree / "linked"))
    path = path.replace("OUTSIDE", str(tmp_path / "outside"))
    with pytest.raises(ConnectorError, match=message) as caught:
        read(connector, tree, {"path": path, "match": r".*\.csv"})
    assert caught.value.code == "ACCESS_DENIED" and spied == []


@pytest.mark.parametrize("name,sibling,regex,pattern", [
    ("orders_*.csv", "orders_payroll.csv", r"orders_\*\.csv", "orders_*.csv"),
    ("a[1].csv", "a1.csv", r"a\[1\]\.csv", "a*.csv"),
    ("b?.csv", "bx.csv", r"b\?\.csv", "b*.csv"),
])
def test_a_file_named_with_a_glob_character_is_refused_before_any_read(connector, tmp_path, spied, name, sibling,
                                                                         regex, pattern):
    # DuckDB reads each path it is given as a glob: the file would be read as its siblings
    root = tmp_path / "root"
    write(root / name, "id\n666\n")
    write(root / sibling, "id\n1\n")
    for arguments in ({"path": ".", "match": regex, "options": {"filename": True}},
                      {"path": pattern, "options": {"filename": True}},
                      {"path": glob.escape(name)}):  # (a user path is a glob: this one names the file itself)
        with pytest.raises(ConnectorError, match=r"has a glob character \(\*, \? or \[\) in its path: it cannot be "
                                                 r"read by name") as caught:
            read(connector, root, arguments)
        assert caught.value.code == "ACCESS_DENIED" and spied == []
    # its normally named sibling still reads, by match and by name
    assert read(connector, root, {"path": ".", "match": sibling.replace(".", r"\."),
                                  "options": {"filename": True}}) == [{"id": "1", "filename": sibling}]
    assert ids(read(connector, root, {"path": sibling})) == [1]
    assert [os.path.basename(path) for path in spied] == [sibling, sibling]


def test_a_file_in_a_folder_named_with_a_glob_character_is_refused(connector, tmp_path, spied):
    root = tmp_path / "root"
    write(root / "x[1]" / "orders.csv", "id\n666\n")
    write(root / "x1" / "orders.csv", "id\n1\n")
    with pytest.raises(ConnectorError, match="has a glob character") as caught:
        read(connector, root, {"path": ".", "match": r".*/orders\.csv", "recursive": True})
    assert caught.value.code == "ACCESS_DENIED" and spied == []
    assert ids(read(connector, root, {"path": "x1", "match": r"orders\.csv"})) == [1]


def test_a_glob_over_normally_named_files_still_reads_them(connector, tree):
    assert ids(read(connector, tree, {"path": "data/orders_*.csv"})) == [1, 2, 9]
    assert ids(read(connector, tree, {"path": "data/orders_2026-10-0[12].csv"})) == [1, 2]
    assert ids(read(connector, tree, {"path": "data/**/orders_2026-??-??.csv"})) == [-1, 0, 1, 2]


def test_the_reader_refuses_a_path_with_a_glob_character(tmp_path):
    write(tmp_path / "a1.csv", "id\n1\n")
    with Reader([str(tmp_path)]) as reader:
        with pytest.raises(AccessError, match="has a glob character"):
            list(reader.pages(str(tmp_path / "a[1].csv"), "csv"))
        assert [page for page in reader.pages(str(tmp_path / "a1.csv"), "csv")] == [[{"id": "1"}]]


# * --------------------------
# * missing folders and files
# * --------------------------

def test_nothing_matching_follows_on_missing(connector, tree, caplog):
    caplog.set_level(logging.WARNING)
    assert read(connector, tree, {"path": "data", "match": r"orders_1999-.*\.csv"}) == []
    assert "files: no file in 'data' matches 'orders_1999-.*\\\\.csv'; skipping it (on_missing: skip)" in caplog.text
    assert read(connector, tree, {"path": "nope", "match": DAILY}) == []  # a folder that does not exist
    with pytest.raises(ConnectorError, match="no file in 'nope' matches") as caught:
        read(connector, tree, {"path": "nope", "match": DAILY, "on_missing": "error"})
    assert caught.value.code == "NOT_FOUND"


def test_with_match_path_must_be_a_folder_at_run_time(connector, tree):
    with pytest.raises(ConnectorError, match="is not a folder: with `match`, `path` is the folder") as caught:
        read(connector, tree, {"path": "data/orders_latest.csv", "match": DAILY})
    assert caught.value.code == "READ_ERROR"
    with pytest.raises(ConnectorError, match="with `match`, `path` is a folder, not a glob"):
        read(connector, tree, {"path": "data/*", "match": DAILY})
    with pytest.raises(ConnectorError, match="with `match`, `path` is one folder, not a list"):
        read(connector, tree, {"path": ["data"], "match": DAILY})
    roots = [os.path.realpath(str(tree))]
    with pytest.raises(ReadError, match="is a glob"):
        match_files("data/*", roots, DAILY)
    with pytest.raises(ReadError, match="is not a valid regex"):
        match_files("data", roots, "(")
    with pytest.raises(AccessError, match="goes up a folder"):
        match_files("..", roots, DAILY)


# * ------
# * checks
# * ------

def test_valid_match_requests(connector):
    assert check(connector, path="data", match=DAILY) == []
    assert check(connector, path="data", match=DAILY, recursive=True, format="csv", on_missing="error") == []
    assert check(connector, path="{{ config.folder }}", match=r".*\.parquet", recursive=False) == []
    assert check(connector, path="exports/{{ partition.day }}", match=r"part-\d+\.jsonl") == []
    assert check(connector, path=".", match=r"[a-z]+\.csv") == []  # [a-z] is in the regex, not the path


@pytest.mark.parametrize("arguments,expected", [
    ({"path": "data", "match": "orders_("}, "files: `match` 'orders_(' is not a valid regex: missing ), "),
    ({"path": "data", "match": "*.csv"}, "files: `match` '*.csv' is not a valid regex"),
    ({"path": "data", "match": ""}, "files: `match` '' must be a non-empty text (a regex)"),
    ({"path": "data", "match": 3}, "files: `match` 3 must be a non-empty text (a regex)"),
    ({"path": "data", "match": None}, "files: `match` None must be a non-empty text (a regex)"),
    ({"path": "data", "match": "{{ config.pattern }}"}, "is a literal regex: it takes no references"),
    ({"path": "data", "match": "{{ secrets.x }}"}, "arguments cannot use secrets"),
    ({"path": "data/*.csv", "match": DAILY}, "files: with `match`, `path` is a folder, not a glob: 'data/*.csv'"),
    ({"path": "data/**", "match": DAILY, "recursive": True}, "with `match`, `path` is a folder, not a glob"),
    ({"path": ["a", "b"], "match": DAILY}, "files: with `match`, `path` is one folder, not a list"),
    ({"path": "../data", "match": DAILY}, "path '../data' goes up a folder (..)"),
    ({"match": DAILY}, "file.read needs `path`"),
    ({"path": "data", "match": DAILY, "recursive": "yes"}, "files: `recursive` must be true or false, got 'yes'"),
    ({"path": "data", "match": DAILY, "recursive": "{{ config.deep }}"}, "`recursive` must be true or false"),
    ({"path": "data/*.csv", "recursive": True}, "files: `recursive` goes with `match`"),
])
def test_match_and_recursive_are_checked(connector, arguments, expected):
    found = check(connector, **arguments)
    assert any(expected in message for message in found), found
