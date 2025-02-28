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

# import sys
# import os
#
# # NOTE: below is a hack to import from the api_connector package
# #       without installing it as a package.
# #       This is done to avoid the need to install the package
# #       when developing it.
# base_path = "/".join(__file__.split("/")[:-3])
# sys.path.insert(0, os.path.join(base_path, "utils"))
# sys.path.insert(1, os.path.join(base_path, "connector"))
# sys.path.insert(2, os.path.join(base_path, "serializer"))
# sys.path.insert(3, os.path.join(base_path, "pipeline"))


import argparse
import ast

import adapt.utils
from adapt.utils import config_reader
from adapt.utils import typing_collection
from adapt.pipeline import pipeline


def type_key_value_pair(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "Key-value pair must be in the form of key=value")
    key, sep, value = value.partition("=")
    return key, ast.literal_eval(value)


def cli():

    parser = argparse.ArgumentParser(description="Pipeline")
    parser.add_argument("--namespace",
                        type=str,
                        action="store",
                        dest="namespace",
                        default=None,
                        help="Name of namespace. E.g: facebook, google, etc.")
    parser.add_argument("--pipeline-config",
                        type=str,
                        action="store",
                        dest="pipeline_config",
                        default=None,
                        help="Name of config file")
    parser.add_argument("--data-ingestion-config",
                        type=str,
                        action="store",
                        dest="data_ingestion_config",
                        default=None,
                        help="Name of config file")
    parser.add_argument("--auth-data",
                        action="extend",
                        nargs="*",
                        type=type_key_value_pair,
                        default=[],
                        help="Authorization data key-value pairs."
                             "E.g: --auth-data refresh_token=94a08da1fecbb6e8b46990538c7b50b2")
    parser.add_argument("--external-input",
                        action="extend",
                        nargs="*",
                        type=type_key_value_pair,
                        default=[],
                        help="External input key-value pairs."
                             "E.g: --external-input campaign_ids=[123456789, 987654321]")

    args = parser.parse_args()
    return args


def make_pipeline_items(options):
    # type: (argparse.Namespace) -> list
    pipeline_config_path = adapt.utils.config_finder(
        module="pipeline",
        namespace="",
        config_name=options.pipeline_config
    )
    config = config_reader.YamlReader.read(pipeline_config_path)
    auth_store = adapt.utils.Store()
    auth_store.from_dict(dict(options.auth_data))
    data_store = adapt.utils.Store()
    data_store.from_dict(dict(options.external_input))

    store = adapt.utils.Store()
    store.add(key="data_ingestion_config", value=options.data_ingestion_config)
    store.add(key="namespace", value=options.namespace)
    store.add(key="auth_store", value=auth_store)
    store.add(key="data_store", value=data_store)

    pipeline_items = typing_collection.init(config["pipeline"], store)
    return pipeline_items


def run_pipeline(pipeline_items):
    data_pipeline = pipeline.Pipeline()
    for item in pipeline_items:
        _item = pipeline.Item(**item)
        data_pipeline.add_item(_item)
    data_pipeline.run()


def main():
    options = cli()
    pipeline_items = make_pipeline_items(options)
    run_pipeline(pipeline_items)
    print("Done!")


if __name__ == "__main__":
    main()
