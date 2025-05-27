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

# setting environment variable for ADAPT_CONFIGS as empty string
# so that it will not read any configs from the default path
os.environ["ADAPT_CONFIGS"] = ""

from adapt.serializer.serializer import Serializer
from adapt.utils.config_reader import YamlReader


data = [
    {
        "account_id": "277381546383784",
        "attribution_spec": [
            {
                "event_type": "CLICK_THROUGH",
                "window_days": 7
            },
            {
                "event_type": "VIEW_THROUGH",
                "window_days": 1
            }
        ],
        "bid_amount": 230,
        "bid_strategy": "COST_CAP",
        "budget_remaining": "803",
        "campaign": {
            "id": "23861273961600732"
        },
        "campaign_id": "23861273961600732",
        "created_time": "2023-10-13T05:47:14-0700",
        "daily_budget": "803",
        "id": "23861274251890732",
        "lifetime_budget": "0",
        "name": "campaign 01",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "promoted_object": {
            "custom_conversion_id": "719479913527844",
            "custom_event_type": "OTHER",
            "pixel_id": "726744084339149",
        },
        "start_time": "2023-10-13T05:47:14-0700",
        "status": "ACTIVE",
        "updated_time": "2023-10-13T06:13:11-0700"
    },
    {
        "account_id": "277381546383784",
        "attribution_spec": [
            {
                "event_type": "CLICK_THROUGH",
                "window_days": 7
            },
            {
                "event_type": "VIEW_THROUGH",
                "window_days": 1
            }
        ],
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "budget_remaining": "2000",
        "campaign": {
            "id": "23857528383050732"
        },
        "campaign_id": "23857528383050732",
        "created_time": "2023-09-12T22:41:15-0700",
        "daily_budget": "2000",
        "id": "23857924806060732",
        "lifetime_budget": "0",
        "name": "campaign 02",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "promoted_object": {
            "custom_event_type": "ADD_TO_CART",
            "pixel_id": "726744084339149"
        },
        "start_time": "2023-09-12T22:41:15-0700",
        "status": "PAUSED",
        "updated_time": "2023-09-12T22:41:15-0700"
    },
    {
        "account_id": "277381546383784",
        "attribution_spec": [
            {
                "event_type": "CLICK_THROUGH",
                "window_days": 7
            },
            {
                "event_type": "VIEW_THROUGH",
                "window_days": 1
            }
        ],
        "bid_amount": 230,
        "bid_strategy": "COST_CAP",
        "budget_remaining": "803",
        "campaign": {
            "id": "23861273961600732"
        },
        "campaign_id": "23861273961600732",
        "created_time": "2023-10-13T05:47:14-0700",
        "daily_budget": "803",
        "id": "23861274251890732",
        "lifetime_budget": "0",
        "name": "campaign 03",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "promoted_object": {
            "custom_event_str": "app_install",
            "custom_event_type": "OTHER",
            "pixel_id": "726744084339149",
        },
        "start_time": "2023-10-13T05:47:14-0700",
        "status": "ACTIVE",
        "updated_time": "2023-10-13T06:13:11-0700"
    },
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
    # dummy API response data
    api_data = ApiData.lazy_run(data)

    # reading json_pipeline config
    config = YamlReader.read(
        os.path.join(os.path.dirname(__file__), "configs/json_pipeline.yaml")
    )

    # creating serializer
    serializer = Serializer.init(config)

    # exporting serialized records
    exporter = DummyExporter()
    exporter.export(records=serializer.serialize_records(api_data))


if __name__ == "__main__":
    main()
