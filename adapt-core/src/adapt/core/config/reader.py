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

import yaml

__all__ = ["load_yaml"]


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader that reads a bare `=` (YAML 1.1's "value" type, e.g. a filter operator) as text."""


_ConfigLoader.add_constructor("tag:yaml.org,2002:value", yaml.SafeLoader.construct_scalar)


def load_yaml(path):
    """Loads a YAML file; a bare `=` is read as text. Raises yaml.YAMLError."""
    with open(path, "r", encoding="utf-8") as stream:
        return yaml.load(stream, Loader=_ConfigLoader)

