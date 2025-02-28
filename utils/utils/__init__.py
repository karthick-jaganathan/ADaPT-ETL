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


try:
    CONFIG_LOCATION = os.environ["ADAPT_CONFIGS"]
except KeyError:
    raise Exception("'ADAPT_CONFIGS' path environment variable not set")


class ConfigNotFoundError(Exception):
    pass


def config_finder(module, namespace, config_name):
    loc = os.path.join(CONFIG_LOCATION, module, namespace, config_name)
    if os.path.exists(loc) and os.path.isfile(loc):
        return loc
    else:
        raise ConfigNotFoundError('config file not found: {}'.format(loc))


class Store(object):
    """
    A store to hold values that can be used by multiple components
    """
    def __init__(self):
        self.store = {}

    def add(self, key, value):
        self.store[key] = value

    def get(self, key, required=False, poison_pill="##NOT_FOUND##"):
        if required and key not in self.store:
            return poison_pill
        return self.store.get(key)

    def clear(self):
        self.store.clear()

    def from_dict(self, data):
        self.store.update(data)
