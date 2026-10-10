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

from streamwright.core.runtime.components import Connector

__all__ = ["OpenAIAdsConnector"]


class OpenAIAdsConnector(Connector):
    """OpenAI Ads: an HTTP API declared in the source (auth + http); see examples/sources/ads/openai_ads."""
    name = "openai_ads"
    transport = "http"
    category = "advertising"
    summary = "OpenAI Ads"
    network_loggers = ("urllib3.connectionpool",)
