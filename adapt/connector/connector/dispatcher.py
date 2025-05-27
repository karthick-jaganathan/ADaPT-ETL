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
        response = getattr(client, config["method"])(**request_args)

        # Check if there's a post-processor configured
        if config.get("post_processor"):
            external_input.add("POST_PROCESSOR_RESPONSE", response)
            return typing_collection.init(config["post_processor"], external_input)
        else:
            return response


def test_dispatcher():
    import yaml
    from unittest.mock import MagicMock

    print("Running test_dispatcher...")
    
    client = MagicMock(
        search_stream=MagicMock(
            return_value=[
                {
                    "id": "123",
                    "name": "Test Campaign",
                }
            ]
        )
    )

    sample_yaml = """

    query: &query
      type: sql_query
      query: |
        SELECT campaign.id, campaign.name FROM campaign

    filters: &filters
      type: sql_filter
      items:
        campaign.id:
          operator: IN
          value:
            type: external_input
            key: campaign_ids
            split_on: ","
            format_as: INT_LIST
            ignore_if: null
        campaign.status:
          operator: IN
          value:
            type: external_input
            key: campaign_status
            split_on: ","
            format_as: DOUBLE_QUOTED_LIST
            ignore_if: null
        campaign.name:
          operator: LIKE
          value:
            type: external_input
            key: campaign_name
            format_as: STRING_DOUBLE_QUOTED
            ignore_if: null

    method: search_stream
    arguments:
      type: dict
      items:
        customer_id:
          type: external_input
          key: customer_id
          required: true
          format_as: INT
        query:
          type: query_builder
          query:
            <<: *query
          filters:
            <<: *filters
    """

    config = yaml.safe_load(sample_yaml)
    store = adapt.utils.Store()
    store.add(key="customer_id", value="123")
    store.add(key="campaign_ids", value="123,456")
    store.add(key="campaign_status", value="ENABLED")

    result = Dispatcher.receive(client, config, store)
    client.search_stream.assert_called_once_with(
        customer_id=123,
        query="""SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id IN (123, 456) AND campaign.status IN ("ENABLED")"""
    )
    assert result == [
        {
            "id": "123",
            "name": "Test Campaign",
        }
    ]
    print("test_dispatcher passed")


if __name__ == "__main__":
    test_dispatcher()
