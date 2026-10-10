# /*************************************************************************
# * Copyright 2026 Karthick Jaganathan
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

"""
The `restapi` connector: declarative HTTP connector exposing metadata and transport="http".
HTTP execution and pagination are performed declaratively by StreamWright's HTTP engine.
"""

from streamwright.core.runtime.components import Connector, ConnectorSpec

__all__ = ["RestApiConnector", "HttpConnector"]


class RestApiConnector(Connector):
    """
    Generic REST API connector metadata.
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="restapi",
        title="REST API",
        category="http",
        transport="http",
    )


class HttpConnector(Connector):
    """
    Generic HTTP connector metadata (alias of restapi).
    Configured via `source.yaml` with transport="http".
    """
    spec = ConnectorSpec(
        name="http",
        title="HTTP",
        category="http",
        transport="http",
    )
