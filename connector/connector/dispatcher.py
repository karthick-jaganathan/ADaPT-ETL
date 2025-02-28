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
from adapt.utils import typing_collection


__all__ = ["Dispatcher"]


class Dispatcher(object):

    @staticmethod
    def receive(client, config, external_input):
        # type: (object, dict, adapt.utils.Store) -> object
        request_args = typing_collection.init(config["arguments"], external_input)
        return getattr(client, config["method"])(**request_args)
