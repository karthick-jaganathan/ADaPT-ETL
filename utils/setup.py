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

from setuptools import setup, find_packages


dependencies = [
    "pyyaml==5.4.1",
]


setup(
    name="adapt_utils",
    version="0.0.1",
    description="Adaptive Data Pipeline Toolkit - Utilities",
    package_dir={
        "adapt.utils": "utils",
    },
    include_package_data=True,
    install_requires=dependencies,
    url="https://github.com/karthick-jaganathan/ADaPT-ETL"
)
