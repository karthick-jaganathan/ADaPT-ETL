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
from adapt.utils.config_reader import YamlReader


__all__ = ["Authorization"]


class Authorization(object):

    @classmethod
    def initialize(cls, config, external_input=None):
        # type: (dict, adapt.utils.Store) -> object
        client = typing_collection.init(config["initializer"], external_input)
        return client

    @classmethod
    def from_config_path(cls, config_path, external_input=None):
        # type: (str, adapt.utils.Store) -> object
        config = YamlReader.read(config_path)
        return cls.initialize(config, external_input)


def test():
    import yaml

    # Example of a yaml file, in this case, the FacebookAdsApi client initialization
    sample_yaml = """
    initializer:
        type: initializer
        client:
            type: callable
            module: facebook_business.api
            class: FacebookAdsApi
            method: init
        arguments:
            type: dict
            items:
                api_version:
                    type: constant
                    value: v17.0
                app_secret:
                    type: external_input
                    key: consumer_secret
                    required: true
                app_id:
                    type: external_input
                    key: consumer_key
                    required: true
                access_token:
                    type: external_input
                    key: refresh_token
                    required: true
    """
    config = yaml.safe_load(sample_yaml)
    external_input = adapt.utils.Store()
    external_input.add(key="refresh_token", value="77963b7a931377ad4ab5ad6a9cd718aa")
    external_input.add(key="consumer_secret", value="5ebe2294ecd0e0f08eab7690d2a6ee69")
    external_input.add(key="consumer_key", value="3c6e0b8a9c15224a8228b9a98ca1531d")

    client = Authorization.initialize(config,  external_input)
    print(client)
    assert client is not None
    assert client.__class__.__name__ == "FacebookAdsApi"
    assert client._api_version == "v17.0"
    assert client._session.access_token == "77963b7a931377ad4ab5ad6a9cd718aa"
    assert client._session.app_id == "3c6e0b8a9c15224a8228b9a98ca1531d"
    assert client._session.app_secret == "5ebe2294ecd0e0f08eab7690d2a6ee69"


if __name__ == "__main__":
    test()
