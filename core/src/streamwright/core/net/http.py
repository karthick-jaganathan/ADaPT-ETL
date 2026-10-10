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

"""HTTP for sources: built-in auth, retries, rate limits, pagination and secret redaction."""

import base64
import collections
import datetime
import hashlib
import json
import logging
import threading
import time
from urllib.parse import quote, quote_plus, urljoin, urlsplit

import requests

from streamwright.core.runtime import logs
from streamwright.core.config.inputs import parse_duration
from streamwright.core.runtime.templates import get_path, to_text


__all__ = ["HttpError", "Redactor", "Authenticator", "RateLimiter", "RetryPolicy", "HttpClient", "paginate",
           "DEFAULT_RETRY", "merge_headers"]

LOG = logging.getLogger("streamwright.source")
# the characters of a response body an error message shows
ERROR_BODY_CHARS = 300

# the redirects one request follows (on its own origin only)
MAX_REDIRECTS = 10
REDIRECT_CODES = (301, 302, 303, 307, 308)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url):
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def same_origin(trusted, url):
    """
    True when `url` is on the origin (scheme, host, port) of `trusted`: next-page links and redirects that leave it
    are not sent the source's credentials. An http -> https upgrade on the same host (default ports) is allowed; a
    downgrade to http is not.
    """
    scheme, host, port = _origin(trusted)
    other_scheme, other_host, other_port = _origin(url)
    if not host or host != other_host:
        return False
    if (scheme, port) == (other_scheme, other_port):
        return True
    return (scheme, port, other_scheme, other_port) == ("http", 80, "https", 443)


DEFAULT_RETRY = {"codes": [429, 500, 502, 503, 504], "max_attempts": 3, "backoff": "exponential", "max_delay": "60s"}
TIMEOUT_SECONDS = 60


def merge_headers(source_headers, request_headers):
    """
    Merges source and request headers. Request headers override source headers of the same name,
    case-insensitively. Headers with empty string or None values are omitted.
    """
    merged = {}
    key_map = {}  # lowercase -> actual key in merged
    for k, v in (source_headers or {}).items():
        if v not in ("", None):
            merged[k] = v
            key_map[k.lower()] = k
    for k, v in (request_headers or {}).items():
        lower = k.lower()
        if lower in key_map:
            old_k = key_map[lower]
            del merged[old_k]
        if v not in ("", None):
            merged[k] = v
            key_map[lower] = k
    return merged


class HttpError(Exception):

    def __init__(self, message, status=None):
        super(HttpError, self).__init__(message)
        self.status = status


def error_excerpt(text, redact, limit=ERROR_BODY_CHARS):
    """
    The start of a response body (or other text) for an error message: redacted (`redact`, then credentials masked
    by name) before it is cut to `limit` characters, so a secret that crosses the cut is never shown in part.
    """
    return logs.mask_text(redact(text))[:limit]


class Redactor(object):
    """Replaces secret values (and tokens obtained at run time) with *** in messages; log lines of any thread."""

    def __init__(self, values=()):
        self.values = []
        self._lock = threading.Lock()
        for value in values:
            self.add(value)

    def add(self, value):
        if value is None or isinstance(value, bool):
            return
        text = str(value)
        # also the forms a value takes in URLs, reprs, JSON and logs: URL-encoded, escaped, stripped
        forms = set(form for form in (text, text.strip(), quote(text, safe=""), quote_plus(text), repr(text)[1:-1],
                                      json.dumps(text)[1:-1]) if len(form) >= 4)
        with self._lock:
            if forms - set(self.values):
                # a new list: other threads may be redacting with the current one
                self.values = sorted(set(self.values) | forms, key=lambda form: (-len(form), form))

    def __call__(self, text):
        text = str(text)
        for value in self.values:
            text = text.replace(value, "***")
        return text


class Authenticator(object):
    """Applies a rendered built-in `auth` block to each request."""

    def __init__(self, auth, session, redact, clock=time.time):
        self.auth = auth or {}
        self.kind = self.auth.get("type")
        self.session = session
        self.redact = redact
        self.clock = clock
        self._token = None
        self._expires_at = 0.0

    def apply(self, headers, params):
        auth = self.auth
        if self.kind == "bearer":
            token = auth.get("token")
            if not token:
                raise HttpError("bearer auth token is empty or missing")
            headers["Authorization"] = "Bearer %s" % token
        elif self.kind == "api_key":
            if auth.get("in", "header") == "query":
                params[auth["name"]] = auth["value"]
            else:
                headers[auth["name"]] = to_text(auth["value"])
        elif self.kind == "basic":
            pair = ("%s:%s" % (auth["username"], auth["password"])).encode("utf-8")
            credential = base64.b64encode(pair).decode("ascii")
            self.redact.add(credential)
            headers["Authorization"] = "Basic %s" % credential
        elif self.kind == "oauth2_refresh_token":
            headers["Authorization"] = "Bearer %s" % self._access_token()

    def refresh(self):
        """Drops a cached OAuth token after a 401; returns True if a retry can help."""
        if self.kind != "oauth2_refresh_token":
            return False
        self._token = None
        return True

    def _access_token(self):
        if self._token and self.clock() < self._expires_at:
            return self._token
        auth = self.auth
        refresh_token = auth.get("refresh_token")
        if not refresh_token:
            raise HttpError("oauth2 refresh_token is empty or missing")
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": auth["client_id"], "client_secret": auth["client_secret"]}
        if auth.get("scopes"):
            data["scope"] = " ".join(auth["scopes"])
        url = self.redact(logs.mask_url(auth["token_url"]))
        started = time.monotonic()
        try:
            response = self.session.post(auth["token_url"], data=data, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logs.http_summary("POST", url, error=exc, seconds=time.monotonic() - started)
            raise HttpError(self.redact("token request to %s failed: %s" % (auth["token_url"], exc)))
        payload = response.json() if response.status_code == 200 else None
        if isinstance(payload, dict):  # the access token, and a new refresh token or ID token, are secrets too
            for key, value in payload.items():
                if str(key).lower().endswith("token") and isinstance(value, str):
                    self.redact.add(value)
        logs.http_summary("POST", url, response, seconds=time.monotonic() - started)
        logs.http_details(response, self.redact, body=False)  # a token response is credentials
        if response.status_code != 200:
            raise HttpError(self.redact("token request to %s failed: HTTP %d: %s" % (
                auth["token_url"], response.status_code, error_excerpt(response.text, self.redact))),
                response.status_code)
        token = payload.get("access_token")
        if not token:
            raise HttpError("token response from %s has no access_token" % auth["token_url"])
        self.redact.add(token)
        self._token = token
        self._expires_at = self.clock() + max(0.0, float(payload.get("expires_in", 3600)) - 60)
        return token


class RateLimiter(object):
    """Allows `requests` calls per `per` seconds (sliding window)."""

    def __init__(self, requests_per_period, period_seconds, clock=time.monotonic, sleep=time.sleep):
        self.limit = requests_per_period
        self.period = period_seconds
        self.clock = clock
        self.sleep = sleep
        self.calls = collections.deque()

    def wait(self):
        now = self.clock()
        while self.calls and now - self.calls[0] >= self.period:
            self.calls.popleft()
        if len(self.calls) >= self.limit:
            delay = self.period - (now - self.calls[0])
            logs.NETWORK.info("rate limit reached; waiting %.1fs", delay,
                              extra=logs.fields(event="rate_limit", duration_ms=int(round(delay * 1000))))
            self.sleep(delay)
            self.calls.popleft()
        self.calls.append(self.clock())


class RetryPolicy(object):
    """A `retry` block: codes (HTTP statuses or provider error codes), max_attempts, backoff, max_delay."""

    def __init__(self, retry=None):
        retry = dict(DEFAULT_RETRY, **(retry or {}))
        self.codes = set(retry["codes"])
        self.max_attempts = retry["max_attempts"]
        self.backoff = retry["backoff"]
        self.max_delay = parse_duration(retry["max_delay"]).total_seconds()

    def delay(self, attempt, retry_after=None):
        delay = 2 ** (attempt - 1) if self.backoff == "exponential" else 1
        if retry_after is not None:
            delay = retry_after
        return min(delay, self.max_delay)


def _retry_after(response):
    value = response.headers.get("Retry-After") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _jsonable(value):
    if isinstance(value, dict):
        return dict((k, _jsonable(v)) for k, v in value.items())
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _with_params(url, params):
    """The URL a request with these query parameters goes to."""
    if not params:
        return url
    try:
        prepared = requests.models.PreparedRequest()
        prepared.prepare_url(url, params)
        return prepared.url
    except Exception:  # (the request fails too, and says why)
        return url


class HttpClient(object):
    """
    HTTP requests with auth, rate limits and retries. `metrics` (logs.RunMetrics) counts the requests and retries of
    the requests that name their stream (`fields`); `streamwright.network` logs each request (INFO) and its details (DEBUG).
    """

    def __init__(self, base_url=None, headers=None, authenticator=None, retry=None, rate_limiter=None,
                 redact=None, session=None, sleep=time.sleep, metrics=None, clock=time.monotonic):
        self.base_url = base_url
        self.headers = dict(headers or {})
        self.session = session or requests.Session()
        self.redact = redact or Redactor()
        self.authenticator = authenticator or Authenticator({}, self.session, self.redact)
        self.retry = RetryPolicy(retry)
        self.rate_limiter = rate_limiter
        self.sleep = sleep
        self.metrics = metrics
        self.clock = clock
        self.last_url = None
        self.last_headers = None

    def url(self, path):
        if path.startswith(("http://", "https://")):
            return path
        if not self.base_url:
            raise HttpError("%r is not an absolute URL (http:// or https://)" % self.redact(path))
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def follow(self, link):
        """
        Resolves a next-page link against the previous request's URL. The link comes from a response, so it must stay
        on that request's origin: the next request carries the source's credentials and headers.
        """
        current = self.last_url or self.url("")
        url = urljoin(current, str(link))
        if not same_origin(current, url):
            raise HttpError(self.redact("the next-page link %s leaves %s; not sending the source's credentials to "
                                        "another origin" % (logs.mask_url(url), logs.mask_url(current))))
        return url

    def _sent(self, fields, attempt):
        if self.metrics is not None:
            self.metrics.request(fields, retry=attempt > 1)

    def _logged(self, method, url, params, headers, response, error, started, attempt, fields, size=None, body=True):
        """The `streamwright.network` lines of one request: its summary line (INFO), then its details (DEBUG)."""
        if not logs.NETWORK.isEnabledFor(logging.INFO):
            return
        shown = self.redact(logs.mask_url(_with_params(url, params)))
        logs.http_summary(method, shown, response, error, self.clock() - started, attempt, fields, size)
        if response is not None:
            logs.http_details(response, self.redact, body)
        else:
            logs.request_details(method, shown, headers, self.redact)

    def request(self, method, path, params=None, headers=None, json_body=None, fields=None):
        """
        Sends a request with auth, rate limiting and retries; returns the decoded JSON (or None). `fields`: the
        stream, request, partition and window it reads, for log lines and the run's metrics.
        """
        url = self.url(path)
        refreshed = False
        attempt = 0
        redirects = 0
        while True:
            attempt += 1
            if self.rate_limiter is not None:
                self.rate_limiter.wait()
            request_headers = merge_headers(self.headers, headers)
            request_params = dict(params or {})
            self.authenticator.apply(request_headers, request_params)
            request_headers = dict((name, to_text(value)) for name, value in request_headers.items() if value not in ("", None))
            self._sent(fields, attempt)
            started = self.clock()
            response = None
            try:
                response = self.session.request(method, url, params=request_params or None, headers=request_headers,
                                                json=_jsonable(json_body) if json_body is not None else None,
                                                timeout=TIMEOUT_SECONDS, allow_redirects=False)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                self._logged(method, url, request_params, request_headers, None, exc, started, attempt, fields)
                error, retryable = HttpError(self.redact("%s %s failed: %s" % (method, url, exc))), True
            except requests.RequestException as exc:
                self._logged(method, url, request_params, request_headers, None, exc, started, attempt, fields)
                raise HttpError(self.redact("%s %s failed: %s" % (method, url, exc)))
            else:
                self._logged(method, url, request_params, request_headers, response, None, started, attempt, fields)
                status = response.status_code
                if status == 401 and not refreshed and self.authenticator.refresh():
                    refreshed = True
                    attempt -= 1
                    continue
                location = response.headers.get("Location") if status in REDIRECT_CODES else None
                if location:
                    # followed here, not by requests: requests drops only `Authorization` on another host, not the
                    # source's other (secret) headers or an api key
                    target = urljoin(url, location)
                    if not same_origin(url, target):
                        raise HttpError(self.redact("%s %s redirects to %s; not sending the source's credentials to "
                                                    "another origin" % (method, logs.mask_url(url),
                                                                        logs.mask_url(target))), status)
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise HttpError(self.redact("%s %s: more than %d redirects" % (method, logs.mask_url(url),
                                                                                      MAX_REDIRECTS)), status)
                    if status == 303 or (status in (301, 302) and method == "POST"):
                        method, json_body = "GET", None
                    url, params = target, None  # the server's Location carries the query
                    attempt -= 1
                    continue
                if response is not None:
                    self.last_headers = response.headers
                if 200 <= status < 300:
                    self.last_url = response.url
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        raise HttpError("%s %s returned invalid JSON" % (method, self.redact(url)), status)
                error = HttpError(self.redact("%s %s failed: HTTP %d: %s" % (
                    method, url, status, error_excerpt(response.text, self.redact))), status)
                retryable = status in self.retry.codes
            self._wait_or_raise(error, retryable, attempt, response, fields)

    def _wait_or_raise(self, error, retryable, attempt, response, fields=None):
        if not retryable or attempt >= self.retry.max_attempts:
            raise error
        delay = self.retry.delay(attempt, _retry_after(response))
        logs.NETWORK.warning("%s; retrying in %.1fs (attempt %d of %d)", error, delay, attempt + 1,
                             self.retry.max_attempts,
                             extra=logs.fields(event="retry", status=error.status, attempt=attempt + 1,
                                               duration_ms=int(round(delay * 1000)), **(fields or {})))
        self.sleep(delay)

    def download(self, url, handle, fields=None):
        """
        Streams the file at url into the binary file `handle` (rewritten on every attempt), with this client's
        auth, rate limit and retries; returns the number of bytes. `fields`: as for request().
        """
        url = self.url(url)
        attempt = 0
        while True:
            attempt += 1
            if self.rate_limiter is not None:
                self.rate_limiter.wait()
            headers, params = dict(self.headers), {}
            self.authenticator.apply(headers, params)
            headers = dict((name, to_text(value)) for name, value in headers.items())
            handle.seek(0)
            handle.truncate()
            self._sent(fields, attempt)
            started = self.clock()
            response = None
            try:
                response = self.session.get(url, params=params or None, headers=headers, stream=True,
                                            timeout=TIMEOUT_SECONDS)
                with response:
                    self.last_headers = response.headers
                    status = response.status_code
                    if 200 <= status < 300:
                        size = 0
                        for chunk in response.iter_content(chunk_size=1 << 20):
                            handle.write(chunk)
                            size += len(chunk)
                        self._logged("GET", url, params, headers, response, None, started, attempt, fields, size,
                                     body=False)
                        return size
                    error = HttpError(self.redact("GET %s failed: HTTP %d: %s" % (
                        url, status, error_excerpt(response.text, self.redact))), status)
                    self._logged("GET", url, params, headers, response, None, started, attempt, fields, body=False)
                    retryable = status in self.retry.codes
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as exc:
                self._logged("GET", url, params, headers, None, exc, started, attempt, fields)
                error, retryable = HttpError(self.redact("GET %s failed: %s" % (url, exc))), True
            except requests.RequestException as exc:
                self._logged("GET", url, params, headers, None, exc, started, attempt, fields)
                raise HttpError(self.redact("GET %s failed: %s" % (url, exc)))
            self._wait_or_raise(error, retryable, attempt, response, fields)


# Re-export paginate from net/paginators
from streamwright.core.net.paginators import paginate
