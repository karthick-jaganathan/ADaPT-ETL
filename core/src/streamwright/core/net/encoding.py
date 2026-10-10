#!/usr/bin/env python
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

"""Query parameter encodings: plain (default) and dotted (Rest.li / nested dictionary syntax)."""

from typing import Any, Dict

__all__ = ["flatten_dotted", "encode_params"]


def flatten_dotted(params: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """
    Flattens nested dictionaries and lists into dotted/bracket query parameter syntax.
    e.g. {"dateRange": {"start": {"year": 2026}}} -> {"dateRange.start.year": 2026}
    and {"accounts": ["a", "b"]} -> {"accounts[0]": "a", "accounts[1]": "b"}
    """
    items = {}
    for key, val in (params or {}).items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            items.update(flatten_dotted(val, full_key))
        elif isinstance(val, (list, tuple)):
            for i, item in enumerate(val):
                items[f"{full_key}[{i}]"] = item
        else:
            items[full_key] = val
    return items


def encode_params(params: Dict[str, Any], encoding: str = "plain") -> Dict[str, Any]:
    """Encodes query parameters according to the chosen encoding strategy."""
    if not params:
        return {}
    if encoding == "dotted":
        return flatten_dotted(params)
    return dict(params)
