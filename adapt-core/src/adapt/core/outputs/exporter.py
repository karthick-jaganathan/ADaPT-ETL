#!/usr/bin/env python
# /*************************************************************************
# * Copyright 2025 Karthick Jaganathan
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
import datetime
import csv
import gzip
import tempfile
import json
from typing import List, Dict, Any, Optional


__all__ = [
    "CSVExporter",
    "JSONExporter", 
    "DataExporter",
    "filter_unique_records"
]


def _dated_output_dir():
    # Read at call time so a changed ADAPT_OUTPUT_DIR takes effect.
    today = datetime.datetime.today().strftime("%Y%m%d")
    return os.path.join(os.getenv("ADAPT_OUTPUT_DIR", "/tmp"), today)


class _AtomicFile(object):
    """
    Reserves a unique output path. Data is written to a hidden `.part` file that is renamed
    into place only when writing succeeds, so readers never see partial files.
    """

    def __init__(self, file_name, output_path, suffix):
        prefix = ".".join([file_name, datetime.datetime.now().strftime('%Y-%m-%d.%H%M%S%f.')])
        _fd, self.tmp_path = tempfile.mkstemp(prefix="." + prefix, suffix=suffix + ".part", dir=output_path)
        os.close(_fd)
        self.path = os.path.join(output_path, os.path.basename(self.tmp_path)[1:-len(".part")])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            os.replace(self.tmp_path, self.path)
        elif os.path.exists(self.tmp_path):
            os.remove(self.tmp_path)
        return False


def filter_unique_records(records, unique_on):
    """Filter records to keep only unique entries based on specified fields."""
    if not unique_on:
        yield from records
        return
        
    seen = set()
    for record in records:
        try:
            unique_token = tuple(record.get(key) for key in unique_on)
            if unique_token not in seen:
                seen.add(unique_token)
                yield record
        except (KeyError, TypeError):
            # If we can't create unique token, include the record
            yield record


class CSVExporter:

    # Exact output directory; when None, $ADAPT_OUTPUT_DIR/<YYYYMMDD> is used.
    output_dir = None

    @property
    def _output_base_dir(self):
        return self.output_dir or _dated_output_dir()

    def create_output_directory(self):
        os.makedirs(self._output_base_dir, exist_ok=True)

    @staticmethod
    def _get_file_descriptor(file_path, headers):
        _fd = gzip.open(file_path, mode="wt", encoding='utf-8', newline='')
        _writer = csv.DictWriter(_fd,
                                 fieldnames=headers,
                                 extrasaction='ignore',
                                 restval='',
                                 dialect='excel-tab',
                                 quoting=csv.QUOTE_MINIMAL)
        _writer.writeheader()
        return _fd, _writer

    def export(self, records):
        export = self.config["export"]
        self.create_output_directory()
        with _AtomicFile(export['filename'], self._output_base_dir, '.csv.gz') as target:
            _fd, _writer = self._get_file_descriptor(target.tmp_path, export['fields'])
            with _fd:
                for record in filter_unique_records(records, export['unique_on']):
                    _writer.writerow(record)
        print("[EXPORTER] exported to file: {!r}".format(target.path))
        return target.path

    @classmethod
    def init(cls, config, output_dir=None):
        new_cls = cls()
        new_cls.config = config
        new_cls.output_dir = output_dir
        return new_cls

    @classmethod
    def lazy_run(cls, config, records, output_dir=None):
        return cls.init(config, output_dir=output_dir).export(records)


class JSONExporter:
    """Export records to JSON format with optional compression."""

    # Exact output directory; when None, $ADAPT_OUTPUT_DIR/<YYYYMMDD> is used.
    output_dir = None

    @property
    def _output_base_dir(self):
        return self.output_dir or _dated_output_dir()

    def create_output_directory(self):
        os.makedirs(self._output_base_dir, exist_ok=True)

    @staticmethod
    def _get_file_descriptor(file_path, compress=True):
        if compress:
            return gzip.open(file_path, mode="wt", encoding='utf-8')
        else:
            return open(file_path, mode="w", encoding='utf-8')

    @staticmethod
    def _write_json_array(fd, records, fields):
        """Writes the same text as json.dump(list(records), fd, indent=2), one record at a time."""
        count = 0
        for record in records:
            if fields is not None:
                record = {field: record.get(field) for field in fields}
            text = json.dumps(record, indent=2, default=str)
            fd.write(("[\n" if count == 0 else ",\n") + "\n".join("  " + line for line in text.split("\n")))
            count += 1
        fd.write("\n]" if count else "[]")
        return count

    def export(self, records, compress=True):
        """Export records to JSON file."""
        export = self.config.get("export", {})
        self.create_output_directory()
        suffix = '.json.gz' if compress else '.json'
        with _AtomicFile(export.get('filename', 'data'), self._output_base_dir, suffix) as target:
            with self._get_file_descriptor(target.tmp_path, compress) as fd:
                self._write_json_array(fd, filter_unique_records(records, export.get('unique_on', [])),
                                       export.get("fields"))
        print("[EXPORTER] exported to file: {!r}".format(target.path))
        return target.path

    @classmethod
    def init(cls, config, output_dir=None):
        new_cls = cls()
        new_cls.config = config
        new_cls.output_dir = output_dir
        return new_cls

    @classmethod
    def lazy_run(cls, config, records, compress=True, output_dir=None):
        return cls.init(config, output_dir=output_dir).export(records, compress=compress)


class DataExporter:
    """Unified data exporter supporting multiple formats."""

    def __init__(self, format_type: str = "json", output_dir: Optional[str] = None, 
                 compress: bool = True, custom_filename: Optional[str] = None):
        """
        Initialize DataExporter.
        
        Args:
            format_type: Export format ('json', 'csv')
            output_dir: Custom output directory (optional)
            compress: Whether to compress output files
            custom_filename: Custom filename (optional)
        """
        self.format_type = format_type.lower()
        self.output_dir = output_dir
        self.compress = compress
        self.custom_filename = custom_filename
        
        if self.format_type not in ['json', 'csv']:
            raise ValueError(f"Unsupported format: {format_type}. Use 'json' or 'csv'.")

    def _create_config(self, records: List[Dict[str, Any]], 
                      filename: Optional[str] = None,
                      fields: Optional[List[str]] = None,
                      unique_on: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create export configuration."""
        # Determine filename
        if self.custom_filename:
            export_filename = self.custom_filename
        elif filename:
            export_filename = filename
        else:
            export_filename = f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Determine fields
        if not fields and records:
            # Auto-detect fields from first record
            fields = list(records[0].keys())
        
        return {
            "export": {
                "filename": export_filename,
                "fields": fields or [],
                "unique_on": unique_on or []
            }
        }

    def _get_output_dir(self) -> str:
        """Get output directory."""
        return self.output_dir or _dated_output_dir()

    def export(self, records: List[Dict[str, Any]], 
              filename: Optional[str] = None,
              fields: Optional[List[str]] = None,
              unique_on: Optional[List[str]] = None) -> str:
        """
        Export records to file.
        
        Args:
            records: List of dictionaries to export
            filename: Base filename (without extension)
            fields: List of fields to export (auto-detected if None)
            unique_on: Fields to use for deduplication
            
        Returns:
            Path to exported file (inside `output_dir` when given, else $ADAPT_OUTPUT_DIR/<YYYYMMDD>)
        """
        if not records:
            raise ValueError("No records to export")
        
        # Create configuration
        config = self._create_config(records, filename, fields, unique_on)
        
        if self.format_type == "csv":
            return CSVExporter.init(config, output_dir=self.output_dir).export(records)
        return JSONExporter.init(config, output_dir=self.output_dir).export(records, compress=self.compress)

    @classmethod
    def quick_export(cls, records: List[Dict[str, Any]], 
                    format_type: str = "json",
                    filename: Optional[str] = None,
                    output_dir: Optional[str] = None,
                    compress: bool = True,
                    fields: Optional[List[str]] = None,
                    unique_on: Optional[List[str]] = None) -> str:
        """
        Quick export method for one-off exports.
        
        Args:
            records: List of dictionaries to export
            format_type: Export format ('json', 'csv')
            filename: Base filename
            output_dir: Output directory
            compress: Whether to compress files
            fields: Fields to export
            unique_on: Fields for deduplication
            
        Returns:
            Path to exported file
        """
        exporter = cls(format_type=format_type, output_dir=output_dir, 
                      compress=compress, custom_filename=filename)
        return exporter.export(records, filename, fields, unique_on)
