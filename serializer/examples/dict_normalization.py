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
import sys

dict_normalize = True if '--dict-normalize' in sys.argv else False


# setting environment variable for ADAPT_CONFIGS as empty string
# so that it will not read any configs from the default path
os.environ["ADAPT_CONFIGS"] = ""


from adapt.serializer.serializer import Serializer
from adapt.utils.config_reader import YamlReader


data = [
    {
        "id": 22345865434546,
        "name": "Campaign 01",
        "status": "RUNNING",
        "start_date": "23/01/23",
        "end_date": "23/01/24",
        "days_left": 1,
        "daily_budget": 0,
        "ad_sets": [
            {
                "id": 347563424354654,
                "name": "Campaign 01 :: adSet 01",
                "status": "RUNNING",
                "start_date": "23/01/23",
                "end_date": "23/01/24",
                "days_left": 1,
                "daily_budget": 100,
                "promoted_object": {
                    "optimization_goal": "LINK_CLICKS",
                    "custom_event_type": "ADD_TO_CART"
                },
                "ads": [
                    {
                        "id": 123456789,
                        "name": "Campaign 01 :: adSet 01 :: ad 01",
                        "status": "RUNNING",
                        "start_date": "23/01/23",
                        "end_date": "23/01/24",
                    },
                    {
                        "id": 123456780,
                        "name": "Campaign 01 :: adSet 01 :: ad 02",
                        "status": "RUNNING",
                        "start_date": "23/01/23",
                        "end_date": "23/01/24",
                    }
                ]
            },
            {
                "id": 56578908746789,
                "name": "Campaign 01 :: adSet 02",
                "status": "ON_HOLD",
                "start_date": "23/01/23",
                "end_date": "23/01/24",
                "daily_budget": "34",
                "promoted_object": {
                    "optimization_goal": "PAGE_LIKES",
                    "custom_event_type": "ADD_TO_CART"
                },
                "ads": []
            }
        ]
    },
    {
        "id": 12345678765432,
        "name": "Campaign 02",
        "status": "RUNNING",
        "start_date": "23/01/23",
        "end_date": "23/01/24",
        "days_left": 1,
        "daily_budget": 100,
        "ad_sets": [
            {
                "id": 12345,
                "name": "Campaign 02 :: adSet 01",
                "status": "RUNNING",
                "start_date": "10/01/23",
                "end_date": "30/01/24",
                "daily_budget": "--",
                "promoted_object": {
                    "optimization_goal": "LINK_CLICKS",
                    "custom_event_type": "PAGE_VIEW"
                },
                "ads": [
                    {
                        "id": 123456789,
                        "name": "Campaign 02 :: adSet 01 :: ad 01",
                        "status": "RUNNING",
                        "start_date": "23/01/23",
                        "end_date": "23/01/24",
                    }
                ]
            }
        ]
    },
    {
        "id": 12345678765432,
        "name": "Campaign 03",
        "status": "RUNNING",
        "start_date": "23/01/23",
        "end_date": "23/01/24",
        "days_left": 1,
        "daily_budget": 100,
        "ad_sets": []
    }
]


class ApiData(object):
    def __init__(self, data):
        self.data = data

    def run(self):
        for item in self.data:
            yield item

    @classmethod
    def lazy_run(cls, data):
        for item in data:
            yield item


class DummyExporter(object):

    def __init__(self):
        pass

    def export(self, records):
        for record in records:
            print("-" * 100)
            print(record)


def main():

    api_response = ApiData.lazy_run(data)
    # passing 'api_response' to serializer

    config = YamlReader.read(
        os.path.join(os.path.dirname(__file__), "configs/dict_normalization.yaml")
    )
    serializer = Serializer.init(config, dict_normalize=dict_normalize)

    exporter = DummyExporter()
    exporter.export(records=serializer.serialize_records(api_response))


if __name__ == "__main__":
    main()
