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

import adapt.utils
from adapt.utils import config_finder
from adapt.utils import typing_collection
from adapt.utils.config_reader import YamlReader
from adapt.connector.authorization import Authorization


__all__ = ["Service"]


class Service(object):

    @classmethod
    def initialize(cls, config, external_input):
        # type: (dict, adapt.utils.Store) -> object
        auth_config_path = config_finder(
            module="authorization",
            namespace=config["authorization"]["namespace"],
            config_name=config["authorization"]["config_name"]
        )
        auth = Authorization.from_config_path(auth_config_path, external_input)
        external_input.add("authorization", auth)
        client = typing_collection.init(config["client"], external_input)
        return client

    @classmethod
    def from_config_path(cls, config_path, external_input):
        # type: (str, adapt.utils.Store) -> object
        config = YamlReader.read(config_path)
        return cls.initialize(config, external_input)
