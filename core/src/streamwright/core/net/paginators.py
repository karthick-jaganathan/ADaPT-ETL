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

"""Paginators for HTTP requests: none, offset, page_number, cursor, and link_header."""

import copy
import hashlib
import json
from typing import Any, Dict, List, NamedTuple, Optional

from streamwright.core.runtime.templates import get_path


__all__ = [
    "PagePatch",
    "Paginator",
    "NoPaginator",
    "OffsetPaginator",
    "PageNumberPaginator",
    "CursorPaginator",
    "LinkHeaderPaginator",
    "PAGINATORS",
    "paginator_for",
    "paginate",
    "set_dotted_path",
    "apply_patch",
    "parse_next_link",
]


class PagePatch(NamedTuple):
    params: Dict[str, Any]
    body: Dict[str, Any]
    url: Optional[str] = None


def set_dotted_path(target: Dict[str, Any], dotted_path: str, value: Any) -> None:
    """Sets a value in a nested mapping by dotted key path."""
    parts = dotted_path.split(".")
    curr = target
    for part in parts[:-1]:
        if part not in curr or not isinstance(curr[part], dict):
            curr[part] = {}
        curr = curr[part]
    curr[parts[-1]] = value


def apply_patch(rendered_request: Dict[str, Any], patch: PagePatch) -> Dict[str, Any]:
    """Applies a PagePatch to a rendered request dictionary."""
    out = dict(rendered_request)
    if patch.url:
        out["url"] = patch.url
    if patch.params:
        out["params"] = dict(out.get("params") or {}, **patch.params)
    if patch.body:
        json_copy = copy.deepcopy(out.get("json") or {})
        for path, val in patch.body.items():
            set_dotted_path(json_copy, path, val)
        out["json"] = json_copy
    return out


def parse_next_link(link_header: Optional[str]) -> Optional[str]:
    """Extracts next-page URL from an RFC 8288 Link header."""
    if not link_header:
        return None
    for entry in link_header.split(","):
        sections = [s.strip() for s in entry.split(";")]
        if not sections:
            continue
        url_part = sections[0]
        if url_part.startswith("<") and url_part.endswith(">"):
            url = url_part[1:-1]
            for param in sections[1:]:
                clean = param.lower().replace(" ", "").replace("'", '"')
                if clean == 'rel="next"' or clean == "rel=next":
                    return url
    return None


def _lookup(data: Any, path: Optional[str]) -> Any:
    if not path or not data:
        return None
    try:
        return get_path(data, path)
    except (KeyError, TypeError, IndexError):
        return None


class Paginator:
    """
    Turns pages into the request changes (PagePatch) of the next page. `configure(spec)` reads the paginator's
    settings; it runs again on every page (the settings may be templated on `response`), while the position (offset,
    page) carries over.
    """

    def __init__(self, spec: Dict[str, Any]):
        self.update_spec(spec)

    def update_spec(self, spec: Dict[str, Any]) -> None:
        self.spec = dict(spec or {})
        self.configure(self.spec)

    def configure(self, spec: Dict[str, Any]) -> None:
        pass

    def first(self) -> PagePatch:
        return PagePatch(params={}, body={}, url=None)

    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        raise NotImplementedError

    def progress_marker(self, patch: PagePatch) -> Any:
        """The value that must change from page to page (a cursor or next URL); None when the paginator always advances."""
        return patch.url or None

    def _patch(self, mapping: Dict[str, Any]) -> PagePatch:
        if self.spec.get("in", "query") == "body":
            return PagePatch(params={}, body=mapping, url=None)
        return PagePatch(params=mapping, body={}, url=None)


class NoPaginator(Paginator):
    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        return None


def _whole_number(value: Any, name: str) -> int:
    from streamwright.core.net.http import HttpError

    try:
        return int(value)
    except (TypeError, ValueError):
        raise HttpError("paginator `%s` must be a whole number, got %r" % (name, value))


def _reached(response: Any, path: Optional[str], position: int, inclusive: bool) -> bool:
    """True when the total at `path` in the response (when there is one) says `position` is past the last page."""
    if not path:
        return False
    total = _lookup(response, path)
    try:
        total = int(total)
    except (TypeError, ValueError):
        return False
    return position >= total if inclusive else position > total


class OffsetPaginator(Paginator):
    current_offset = None

    def configure(self, spec: Dict[str, Any]) -> None:
        self.offset_param = str(spec.get("offset_param") or "offset")
        self.limit_param = str(spec.get("limit_param") or "limit")
        self.page_size = _whole_number(spec.get("page_size", 100), "page_size")
        self.start = _whole_number(spec.get("start", 0), "start")
        self.total_path = spec.get("total_path")

    def first(self) -> PagePatch:
        self.current_offset = self.start
        return self._patch({self.offset_param: self.current_offset, self.limit_param: self.page_size})

    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        if not records:
            return None
        self.current_offset += len(records)
        if self.total_path:
            if _reached(response, self.total_path, self.current_offset, inclusive=True):
                return None
        elif len(records) < self.page_size:
            return None
        return self._patch({self.offset_param: self.current_offset, self.limit_param: self.page_size})

    def progress_marker(self, patch: PagePatch) -> Any:
        return None


class PageNumberPaginator(Paginator):
    current_page = None

    def configure(self, spec: Dict[str, Any]) -> None:
        self.page_param = str(spec.get("page_param") or "page")
        self.size_param = spec.get("size_param")
        self.page_size = _whole_number(spec["page_size"], "page_size") if spec.get("page_size") is not None else None
        self.start = _whole_number(spec.get("start", 1), "start")
        self.total_pages_path = spec.get("total_pages_path")

    def _page(self, page: int) -> PagePatch:
        mapping = {self.page_param: page}
        if self.size_param and self.page_size is not None:
            mapping[self.size_param] = self.page_size
        return self._patch(mapping)

    def first(self) -> PagePatch:
        self.current_page = self.start
        return self._page(self.current_page)

    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        if not records:
            return None
        if self.size_param and self.page_size is not None and len(records) < self.page_size:
            return None
        self.current_page += 1
        if _reached(response, self.total_pages_path, self.current_page, inclusive=False):
            return None
        return self._page(self.current_page)

    def progress_marker(self, patch: PagePatch) -> Any:
        return None


class CursorPaginator(Paginator):
    def configure(self, spec: Dict[str, Any]) -> None:
        self.token_path = spec.get("token_path")
        self.param = spec.get("param")
        self.next_url_path = spec.get("next_url_path")
        self.has_more_path = spec.get("has_more_path")

    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        from streamwright.core.net.http import HttpError

        if not records:
            return None
        if self.has_more_path and not _lookup(response, self.has_more_path):
            return None
        source = self.next_url_path or self.token_path
        value = _lookup(response, source)
        if value in (None, ""):
            if self.has_more_path:
                raise HttpError("`%s` says there are more pages but there is nothing at %r" % (self.has_more_path,
                                                                                                source))
            return None
        if self.next_url_path:
            return PagePatch(params={}, body={}, url=str(value))
        return self._patch({self.param: value})

    def progress_marker(self, patch: PagePatch) -> Any:
        if patch.url:
            return patch.url
        mapping = patch.body if self.spec.get("in", "query") == "body" else patch.params
        token = mapping.get(self.param)
        return json.dumps(token, sort_keys=True, default=str) if isinstance(token, (dict, list)) else token


class LinkHeaderPaginator(Paginator):
    def next(self, response: Any, records: List[Any], headers: Any) -> Optional[PagePatch]:
        if not records:
            return None
        url = parse_next_link(_header(headers, "Link"))
        if not url:
            return None
        return PagePatch(params={}, body={}, url=url)


def _header(headers: Any, name: str) -> Optional[str]:
    """A response header, case-insensitively (requests' headers already are; plain dicts in tests are not)."""
    if not headers:
        return None
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None


PAGINATORS = {
    "none": NoPaginator,
    "offset": OffsetPaginator,
    "page_number": PageNumberPaginator,
    "cursor": CursorPaginator,
    "link_header": LinkHeaderPaginator,
}


def paginator_for(spec: Optional[Dict[str, Any]]) -> Paginator:
    """Returns a Paginator instance for a paginator configuration dictionary."""
    from streamwright.core.net.http import HttpError

    if not spec:
        return NoPaginator({})
    kind = spec.get("type", "none")
    if kind not in PAGINATORS:
        raise HttpError("unknown paginator type %r (one of: %s)" % (kind, ", ".join(sorted(PAGINATORS))))
    return PAGINATORS[kind](spec)


def paginate(send, spec_for, select):
    """
    Yields (response, records) pages.
    send(patch) performs one request (a PagePatch: the page's params, body changes or next URL) and returns the
    decoded response; the response headers of the last request are read from `send.last_headers` (or the
    response's `headers`).
    spec_for(response) returns the paginator config (response is None before the first page).
    select(response) returns the page's records.
    """
    from streamwright.core.net.http import HttpError

    initial_spec = spec_for(None) if callable(spec_for) else spec_for
    paginator = paginator_for(initial_spec or {"type": "none"})

    seen = set()
    previous = [None]

    def check_progress(marker):
        if marker in seen:
            raise HttpError("the paginator is not advancing: %r repeats" % (marker,))
        seen.add(marker)

    def check_page(records):
        digest = hashlib.sha1(json.dumps(records, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if digest == previous[0]:
            raise HttpError("the paginator is not advancing: a page repeats the previous page")
        previous[0] = digest

    patch = paginator.first()
    while True:
        response = send(patch)
        records = select(response)
        if not records:
            yield response, records
            return
        check_page(records)
        yield response, records

        if callable(spec_for):
            updated_spec = spec_for(response)
            if updated_spec and updated_spec != paginator.spec:
                if updated_spec.get("type", "none") != paginator.spec.get("type", "none"):
                    raise HttpError("the paginator type cannot change between pages")
                paginator.update_spec(updated_spec)

        headers = getattr(send, "last_headers", None) or getattr(response, "headers", None) or {}
        patch = paginator.next(response, records, headers)
        if patch is None:
            return
        marker = paginator.progress_marker(patch)
        if marker is not None:
            check_progress(marker)
