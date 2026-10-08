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


"""Reading files that async jobs produce: csv, jsonl or json, optionally zip or gzip compressed."""

import csv
import gzip
import io
import json
import zipfile
import zlib


__all__ = ["DownloadError", "read", "PAGE_SIZE"]

PAGE_SIZE = 1000


class DownloadError(Exception):
    pass


def _members(handle, compression):
    if compression == "zip":
        try:
            archive = zipfile.ZipFile(handle)
        except zipfile.BadZipFile as exc:
            raise DownloadError("the file is not a zip archive: %s" % exc)
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        if not names:
            raise DownloadError("the zip archive is empty")
        for name in names:  # several files are read in name order
            with archive.open(name) as member:
                yield member
    elif compression == "gzip":
        with gzip.GzipFile(fileobj=handle, mode="rb") as member:
            yield member
    else:
        yield handle


def _rows(text, file_format):
    if file_format == "csv":
        for row in csv.DictReader(text):
            row.pop(None, None)  # values beyond the header row
            yield dict((key, None if value == "" else value) for key, value in row.items())
    else:
        for line in text:
            if line.strip():
                yield json.loads(line)


def read(handle, file_format, compression="none", page_size=PAGE_SIZE):
    """
    Yields pages of records from a downloaded binary file: lists of rows for csv (empty cells are null) and jsonl;
    for json, the whole document (records are then selected with `records.path`).
    """
    handle.seek(0)
    try:
        for binary in _members(handle, compression or "none"):
            text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            try:
                if file_format == "json":
                    yield json.load(text)
                    continue
                page = []
                for row in _rows(text, file_format):
                    page.append(row)
                    if len(page) >= page_size:
                        yield page
                        page = []
                if page:
                    yield page
            finally:
                text.detach()  # leaves the underlying file open for its owner
    except (ValueError, csv.Error, OSError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
        raise DownloadError("cannot read the downloaded %s file%s: %s" % (
            file_format, "" if compression in (None, "none") else " (%s)" % compression, exc))
