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

from typing import List, Dict, Any, Generator

import adapt.utils


__all__ = ["SearchStreamToDict"]


class SearchStreamToDict(object):
    """
    Post-processor for Google Ads API streaming responses.
    Converts SearchGoogleAdsStreamResponse objects to dictionaries.
    """

    @staticmethod
    def _to_dict(pb):
        from google.protobuf.json_format import MessageToDict
        import google.protobuf
        
        # Handle protobuf version compatibility
        # In protobuf 5.x, 'including_default_value_fields' was renamed to 'always_print_fields_with_no_presence'
        protobuf_version = tuple(map(int, google.protobuf.__version__.split('.')[:2]))
        
        if protobuf_version >= (5, 0):
            # protobuf 5.x and later
            return MessageToDict(pb._pb, always_print_fields_with_no_presence=False, preserving_proto_field_name=True)
        else:
            # protobuf 4.x and earlier
            return MessageToDict(pb._pb, including_default_value_fields=False, preserving_proto_field_name=True)

    @staticmethod
    def _process_stream(stream):
        # type: (Any) -> Generator[Dict, None, None]
        if hasattr(stream, '__iter__'):
            for item in stream:
                yield item
        else:
            return []

    @staticmethod
    def process(stream):
        # type: (Any) -> Generator[Dict, None, None]
        stream_results = SearchStreamToDict._process_stream(stream)
        for result in stream_results:
            for item in result.results:
                yield SearchStreamToDict._to_dict(item)
