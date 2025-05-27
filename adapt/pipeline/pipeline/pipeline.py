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

from typing import Any, Dict, List, Optional, Union, Callable


__all__ = [
    "Pipeline",
    "Item"
]


class PipelineExists(Exception):
    pass


class ItemArgumentExists(Exception):
    pass


class Item(object):
    def __init__(self, name, processor, arguments, forward_to=None):
        # type: (str, Callable, Dict, Optional[Dict]) -> None
        self.name = name
        self.processor = processor
        self.arguments = arguments
        self.forward_to = forward_to

    def add_argument(self, name, value):
        if name in self.arguments:
            raise ItemArgumentExists("[ERROR] argument with name {!r} already exists "
                                     "in pipeline item {!r}".format(name, self.name))
        self.arguments[name] = value


class Pipeline:

    def __init__(self):
        self._items = {}

    def add_item(self, item):
        # type: (Item) -> None
        if item.name in self._items:
            raise PipelineExists("[ERROR] processor with name {!r} already exists".format(item.name))
        self._items[item.name] = item

    def _record_pipeline_result(self, item, result):
        # type: (Item, Any) -> None
        if not item.forward_to:
            print("[INFO] pipeline item {!r} results are ignored.".format(item.name))
            return
        for prop in item.forward_to:
            item = self._items[prop["forward_to"]]
            item.add_argument(prop["name"], result)

    def _build_pipeline_args(self, item):
        # type: (Item) -> Dict
        _pipeline_result = self._get_pipeline_result(item.name)
        if _pipeline_result == "#NOT_FOUND#":
            return item.arguments
        else:
            return dict(item.arguments, **{_pipeline_result: _pipeline_result})

    def run(self):
        for name, item in self._items.items():
            print("[START] processing pipeline item {!r}".format(name))
            # print("pipeline item {!r} arguments: {!r}".format(name, item.arguments.keys()))
            result = item.processor(**item.arguments)
            self._record_pipeline_result(item, result)
            print("[END] processed pipeline item {!r}".format(name))
