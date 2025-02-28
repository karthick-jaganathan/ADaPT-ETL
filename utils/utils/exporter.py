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
import time
import datetime
import csv
import gzip
import tempfile


__all__ = [
    "CSVExporter"
]


def filter_unique_records(records, unique_on):
    seen = set()
    for record in records:
        unique_token = tuple(record[key] for key in unique_on)
        if unique_token not in seen:
            seen.add(unique_token)
            yield record


class CSVExporter:

    @property
    def _output_base_dir(self):
        today = datetime.datetime.today().strftime("%Y%m%d")
        return os.path.join("/tmp", today)

    def create_output_directory(self):
        if not os.path.exists(self._output_base_dir):
            os.makedirs(self._output_base_dir)

    @staticmethod
    def _mk_temp_file(file_name, output_path):
        _fd, tmp_file = tempfile.mkstemp(
            prefix=".".join([file_name, datetime.datetime.now().strftime('%Y-%m-%d.%H%M%S%f.')]),
            dir=output_path,
            suffix='.csv.gz'
        )
        os.close(_fd)
        return tmp_file

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

    @staticmethod
    def _get_file_name(config, output_path):
        file_name = "{fname}_{ts}.csv.gz".format(fname=config["export"]['filename'],
                                                 ts=format(time.time() * 1000, ".0f"))
        return os.path.join(output_path, file_name)

    def export(self, records):
        self.create_output_directory()
        file_path = self._mk_temp_file(
            file_name=self.config["export"]['filename'],
            output_path=self._output_base_dir
        )
        _fd, _writer = self._get_file_descriptor(file_path, self.config["export"]['fields'])
        for record in filter_unique_records(records, self.config["export"]['unique_on']):
            _writer.writerow(record)
        _fd.flush()
        _fd.close()
        print("[EXPORTER] exported to file: {!r}".format(file_path))
        return file_path

    @classmethod
    def init(cls, config):
        new_cls = cls()
        new_cls.config = config
        return new_cls

    @classmethod
    def lazy_run(cls, config, records):
        return cls.init(config).export(records)
