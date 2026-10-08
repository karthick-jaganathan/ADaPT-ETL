import gzip
import json
import os
import re

import pytest

from streamwright.core.outputs import exporter

CONFIG = {"export": {"filename": "campaign", "fields": ["id", "name"], "unique_on": ["id"]}}


def broken_stream():
    yield {"id": 1, "name": "a"}
    raise RuntimeError("API stream broke")


def test_csv_export_writes_one_complete_file(tmp_path):
    rows = [{"id": 1, "name": "a"}, {"id": 1, "name": "duplicate"}, {"id": 2, "name": "b", "extra": "x"}]
    path = exporter.CSVExporter.lazy_run(CONFIG, rows, output_dir=str(tmp_path))
    assert os.path.dirname(path) == str(tmp_path)
    assert re.match(r"campaign\.\d{4}-\d\d-\d\d\.\d+\..+\.csv\.gz$", os.path.basename(path))
    assert os.listdir(str(tmp_path)) == [os.path.basename(path)]
    with gzip.open(path, "rt") as stream:
        assert stream.read().splitlines() == ["id\tname", "1\ta", "2\tb"]


@pytest.mark.parametrize("exporter_class", [exporter.CSVExporter, exporter.JSONExporter])
def test_failed_export_leaves_no_files(tmp_path, exporter_class):
    with pytest.raises(RuntimeError):
        exporter_class.lazy_run(CONFIG, broken_stream(), output_dir=str(tmp_path))
    assert os.listdir(str(tmp_path)) == []


def test_default_output_dir_is_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMWRIGHT_OUTPUT_DIR", str(tmp_path))
    path = exporter.CSVExporter.lazy_run(CONFIG, [{"id": 1}])
    dated_folder = os.path.dirname(path)
    assert os.path.dirname(dated_folder) == str(tmp_path)
    assert re.match(r"\d{8}$", os.path.basename(dated_folder))


@pytest.mark.parametrize("records", [
    [],
    [{"id": 1, "name": "a", "nested": [1, {"b": 2}]}],
    [{"id": 1}, {"id": 2, "name": "line\nbreak", "when": object}],
])
def test_streamed_json_matches_json_dump(tmp_path, records):
    path = exporter.JSONExporter.lazy_run({"export": {"filename": "data"}}, iter(records), compress=False,
                                          output_dir=str(tmp_path))
    with open(path) as stream:
        assert stream.read() == json.dumps(records, indent=2, default=str)


def test_json_export_projects_fields_after_de_duplication(tmp_path):
    rows = [{"id": 1, "name": "a", "other": 1}, {"id": 1, "name": "b"}]
    path = exporter.JSONExporter.lazy_run(CONFIG, rows, output_dir=str(tmp_path))
    with gzip.open(path, "rt") as stream:
        assert json.load(stream) == [{"id": 1, "name": "a"}]


@pytest.mark.parametrize("format_type", ["json", "csv"])
def test_data_exporter_uses_output_dir_without_touching_the_environment(tmp_path, monkeypatch, format_type):
    monkeypatch.setenv("STREAMWRIGHT_OUTPUT_DIR", str(tmp_path / "default"))
    target = tmp_path / "custom"
    path = exporter.DataExporter.quick_export([{"a": 1}], format_type=format_type, filename="x",
                                              output_dir=str(target))
    assert os.path.dirname(path) == str(target)
    assert os.environ["STREAMWRIGHT_OUTPUT_DIR"] == str(tmp_path / "default")
