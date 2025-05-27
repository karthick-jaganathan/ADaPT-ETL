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
import adapt.utils

__all__ = ["YamlReader"]


class YamlReader:

    @staticmethod
    def read(config_path):
        with open(config_path, 'r') as stream:
            try:
                return yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)

    @staticmethod
    def load_from_config_location(module, namespace, config_name):
        # type: (str, str, str) -> dict
        config_path = adapt.utils.config_finder(module, namespace, config_name)
        return YamlReader.read(config_path)
