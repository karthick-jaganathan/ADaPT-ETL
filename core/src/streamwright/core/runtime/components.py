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


"""
Components (docs/design/source-format.md) - connectors and query builders - registered by connector packages under
their names in entry-point groups; in a connector's pyproject.toml:

    [project.entry-points."streamwright.connectors"]           # Connector: an auth provider and its `sdk` requests
    google_ads = "streamwright.connectors.google_ads.connector:GoogleAdsConnector"

    [project.entry-points."streamwright.query_builders"]       # QueryBuilder: query text from a typed mapping
    gaql = "streamwright.connectors.google_ads.gaql:GaqlBuilder"

Source files can only use installed (and allowed) components. Each connector decides which services and methods a
source may call (read-only ones), and each query builder writes values safely into its query language.
"""

import logging
import time

from streamwright.core.runtime import logs
from streamwright.core.engine import queries
from streamwright.core.net.http import RetryPolicy


__all__ = ["ENTRY_POINT_GROUP", "QUERY_BUILDER_GROUP", "Connector", "QueryBuilder", "ConnectorError",
           "ComponentLoadError", "ConnectorContext", "register", "unregister", "available", "load", "component_problems",
           "check_source", "check_queries", "check_call", "sdk_call", "poll_call", "sdk_calls", "request_body",
           "path_text"]

LOG = logging.getLogger("streamwright.source")
ENTRY_POINT_GROUP = "streamwright.connectors"
QUERY_BUILDER_GROUP = "streamwright.query_builders"


class ConnectorError(Exception):
    """
    A failed connector call. `code` is the provider's error code, matched against `retry.codes`; `retryable` marks
    errors that are worth retrying anyway (quota, temporarily unavailable); `retry_after` is in seconds.
    """

    def __init__(self, message, code=None, retryable=False, retry_after=None):
        super(ConnectorError, self).__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class ComponentLoadError(Exception):
    pass


class Connector(object):
    """
    Base class for connectors. Subclasses set `name`, the `auth` keys their provider takes and the `headers` their
    requests can set, and implement connect() and request(); check_request() rejects requests the connector does not
    support before a run starts. `network_loggers` names the loggers the connector's SDK writes its requests and
    responses to, so that `streamwright connectors` (and the docs) can list them for `streamwright run --log NAME=DEBUG`; streamwright never
    turns them on by itself.
    """

    name = None
    transport = "sdk"     # "sdk": connect()/request() run sdk: requests; "http": core's HTTP engine runs http: requests
    query_builders = ()
    auth_required = ()
    auth_optional = ()
    request_headers = ()  # names a request's `headers` may use, e.g. a per-request account
    network_loggers = ()  # e.g. ("google.ads.googleads.client",)
    category = "other"    # how `streamwright connectors` groups it (e.g. "databases"); each connector sets its own
    summary = ""          # a one-line description shown by `streamwright connectors`

    def check_auth(self, auth):
        """Problems with the keys of the `auth` block (without `provider`)."""
        if self.transport == "http":
            return []
        known = tuple(self.auth_required) + tuple(self.auth_optional)
        problems = ["auth: provider %r needs %r" % (self.name, key) for key in self.auth_required if key not in auth]
        problems += ["auth: provider %r does not support %r (supported: %s)" % (self.name, key, ", ".join(known))
                     for key in auth if key not in known]
        return problems

    def check_request(self, request):
        """Problems with an unrendered {service, method, arguments, headers} request (headers are checked by
        check_call against request_headers)."""
        return []

    def connect(self, auth, context):
        """Builds the SDK client from the rendered `auth` block (without `provider`)."""
        raise NotImplementedError

    def request(self, client, request, context):
        """
        Yields the responses of a rendered {service, method, arguments, headers} request (`headers` only when the
        source sets them) as plain data (dicts, lists, text, numbers). Calls that reach the API go through
        context.call() for rate limits and retries.
        """
        raise NotImplementedError

    def error(self, exc):
        """Maps an SDK exception to a ConnectorError, or returns None for exceptions that are not API errors."""
        return None


class QueryBuilder(object):
    """
    Base class for query builders. In a request, `{<name>: spec}` (a mapping with that single key) is replaced by
    build(spec), the query text. The spec's references are rendered first, with their types (dates, numbers, lists),
    so build() must write every value safely: checked, quoted and escaped for its query language.
    """

    name = None
    connector = None

    def check(self, spec):
        """Problems with an unrendered spec (values may still be references) as (path inside the spec, message)."""
        return []

    def build(self, spec):
        """The query text for a rendered spec; raises streamwright.core.engine.queries.QueryError for values it cannot write."""
        raise NotImplementedError


class ConnectorContext(object):
    """
    What a stream gives its connector: redaction - secret() masks what the connector obtains while the run goes (access
    tokens, refreshed tokens, signatures) in every log line and error - and calls with the stream's rate limit and
    retry policy, counted in the run's metrics (`metrics`, logs.RunMetrics) for the stream and request in `where`,
    which the runner sets. `attempt` is the attempts the last call took.
    """

    def __init__(self, connector, redact, retry=None, rate_limiter=None, sleep=time.sleep, metrics=None,
                 clock=time.monotonic):
        self.connector = connector
        self.redact = redact
        self.retry = RetryPolicy(retry)
        # provider codes come only from the source: the default codes are HTTP statuses
        self.codes = set((retry or {}).get("codes") or ())
        self.rate_limiter = rate_limiter
        self.sleep = sleep
        self.metrics = metrics
        self.clock = clock
        self.where = None  # the stream, request, partition and window of the calls being made
        self.attempt = 0
        self.log = LOG

    def secret(self, value):
        """Masks `value` (e.g. an access token the connector obtained) as *** in every log line and error from now
        on."""
        self.redact.add(value)

    def retryable(self, error):
        return error.retryable or (error.code is not None and error.code in self.codes)

    def call(self, function, *args, **kwargs):
        """Calls function(*args, **kwargs) after the rate limit; retries errors per the retry policy."""
        attempt = 0
        while True:
            attempt += 1
            if self.rate_limiter is not None:
                self.rate_limiter.wait()
            if self.metrics is not None:
                self.metrics.request(self.where, retry=attempt > 1)
            self.attempt = attempt
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                error = exc if isinstance(exc, ConnectorError) else self.connector.error(exc)
                if error is None:
                    raise
                if not self.retryable(error) or attempt >= self.retry.max_attempts:
                    if error is exc:
                        raise
                    raise error from exc
            delay = self.retry.delay(attempt, error.retry_after)
            logs.NETWORK.warning("%s; retrying in %.1fs (attempt %d of %d)", self.redact(error), delay, attempt + 1,
                                 self.retry.max_attempts,
                                 extra=logs.fields(event="retry", code=None if error.code is None else str(error.code),
                                                   attempt=attempt + 1, duration_ms=int(round(delay * 1000)),
                                                   **(self.where or {})))
            self.sleep(delay)


# * --------
# * registry
# * --------
_KINDS = {"connector": (ENTRY_POINT_GROUP, Connector), "query builder": (QUERY_BUILDER_GROUP, QueryBuilder)}
_REGISTERED = dict((kind, {}) for kind in _KINDS)
_LOADED = dict((kind, {}) for kind in _KINDS)


def _kind(component):
    for kind, (_, base) in _KINDS.items():
        if isinstance(component, base):
            return kind
    raise TypeError("%r is neither a Connector nor a QueryBuilder" % (component,))


def register(component):
    """Registers a connector or query builder object in this process (for tests and embedding; installs use entry
    points)."""
    _REGISTERED[_kind(component)][component.name] = component


def unregister(name):
    for kind in _KINDS:
        _REGISTERED[kind].pop(name, None)
        _LOADED[kind].pop(name, None)


def _entry_points(group=ENTRY_POINT_GROUP):
    from importlib import metadata
    found = metadata.entry_points()
    if hasattr(found, "select"):
        return list(found.select(group=group))
    return list(found.get(group, []))  # Python 3.9


def available(kind="connector"):
    """Names of the components of a kind ("connector" or "query builder") that can be loaded; nothing is imported."""
    return sorted(set(_REGISTERED[kind]) | set(entry.name for entry in _entry_points(_KINDS[kind][0])))


def package_name(name):
    return "streamwright-" + name.replace("_", "-")


def load(name, allowed=None, kind="connector"):
    """The component registered as `name`; raises ComponentLoadError if it is not allowed or not installed."""
    group, base = _KINDS[kind]
    if kind == "connector" and allowed is not None and name not in allowed:
        raise ComponentLoadError("%s %r is not in the allowed list (%s)" % (kind, name, ", ".join(allowed) or "empty"))
    if name in _REGISTERED[kind]:
        component = _REGISTERED[kind][name]
    elif name in _LOADED[kind]:
        component = _LOADED[kind][name]
    else:
        entries = [entry for entry in _entry_points(group) if entry.name == name]
        if not entries:
            if allowed is not None and name not in allowed:
                raise ComponentLoadError("%s %r is not in the allowed list (%s)" % (
                    kind, name, ", ".join(allowed) or "empty"))
            raise ComponentLoadError("%s %r is not installed; install it with: pip install %s" % (
                kind, name, package_name(name)))
        try:
            loaded = entries[0].load()
        except Exception as exc:  # a broken install or a missing SDK
            raise ComponentLoadError("%s %r could not be loaded: %s: %s" % (kind, name, type(exc).__name__, exc))
        component = loaded() if isinstance(loaded, type) else loaded
        if not isinstance(component, base) or component.name != name:
            raise ComponentLoadError("entry point %r in group %s is not a %r %s" % (name, group, name, kind))
        _LOADED[kind][name] = component

    if allowed is not None and name not in allowed:
        if kind == "query builder":
            if getattr(component, "connector", None) in allowed:
                return component
            for conn_name in allowed:
                conn = _REGISTERED["connector"].get(conn_name) or _LOADED["connector"].get(conn_name)
                if conn and name in getattr(conn, "query_builders", ()):
                    return component
        raise ComponentLoadError("%s %r is not in the allowed list (%s)" % (kind, name, ", ".join(allowed) or "empty"))
    return component


# * ------
# * checks
# * ------

def sdk_call(request):
    """The {service, method, arguments[, headers, path, url]} call of an sdk request."""
    call = dict((key, request.get(key)) for key in ("service", "method", "arguments"))
    for extra in ("path", "url"):
        if request.get(extra) is not None:
            call[extra] = request[extra]
    if not call.get("service") and call.get("path"):
        call["service"] = call["path"]
    if request.get("headers") is not None:
        call["headers"] = request["headers"]
    return call


def poll_call(submit, poll):
    """An async job's poll call: the submit's service and headers, the poll's method and arguments."""
    call = {"service": submit.get("service"), "method": poll.get("method"), "arguments": poll.get("arguments")}
    for extra in ("path", "url"):
        if poll.get(extra) is not None:
            call[extra] = poll[extra]
        elif submit.get(extra) is not None:
            call[extra] = submit[extra]
    if not call.get("service") and call.get("path"):
        call["service"] = call["path"]
    if submit.get("headers") is not None:
        call["headers"] = submit["headers"]
    return call


def request_body(item):
    """What a stream's `requests` item sends: the item without its name, records, paginator and partitions."""
    return dict((key, value) for key, value in item.items() if key not in ("name", "records", "paginator",
                                                                           "partitions"))


def sdk_calls(request):
    """The calls an unrendered request (a request_body) makes through its connector (see sdk_call)."""
    if "sdk" in request:
        return [sdk_call(request)]
    job = request.get("async_job")
    if not job:
        return []
    submit = job.get("submit") or {}
    calls = sdk_calls(submit)
    if calls and job.get("poll"):
        calls.append(poll_call(submit, job["poll"]))
    return calls + sdk_calls(job.get("results") or {})


def check_call(connector, call, builders=None):
    """
    Problems with an unrendered call: headers the connector does not take, then the connector's own checks, which see
    the calls of query builders (`builders`: names; default: the installed ones) as BuiltQuery values.
    """
    problems = []
    headers = call.get("headers")
    if headers is not None:
        unknown = sorted(str(name) for name in (headers if isinstance(headers, dict) else {})
                         if name not in connector.request_headers)
        target = call.get("service") or call.get("path")
        where = "%s: %s.%s" % (connector.name, target, call.get("method"))
        if not isinstance(headers, dict):
            problems.append("%s: `headers` must be a mapping" % where)
        elif unknown:
            problems.append("%s does not take the header(s) %s (%s)" % (where, ", ".join(unknown), (
                "headers: %s" % ", ".join(connector.request_headers)) if connector.request_headers else
                "%s requests take no headers" % connector.name))
    names = set(available("query builder")) if builders is None else builders
    call = queries.replace_calls(call, names, lambda name, spec: queries.BuiltQuery(name))
    return problems + list(connector.check_request(call))


def check_queries(request, allowed=None, builders=None):
    """
    Problems with the query builder calls in an unrendered request, as (path inside the request, message, missing):
    a builder that is not allowed or cannot be loaded, and the builder's own checks.
    """
    names = set(available("query builder")) if builders is None else builders
    problems = []
    for path, name, spec in queries.find_calls(request, names):
        try:
            builder = load(name, allowed, kind="query builder")
        except ComponentLoadError as exc:
            problems.append((path + (name,), str(exc), False))
            continue
        problems += [(path + (name,) + tuple(inner), "%s: %s" % (name, message), False)
                     for inner, message in builder.check(spec)]
    return problems


def component_problems(source, allowed=None):
    """
    What stops a source from running here, before it starts, as (path in the source, message, missing): a connector or
    query builder that is not installed (`missing`), not allowed or broken, auth keys or calls its connector does not
    support, and query builder checks, for every `requests` item of every stream (as streamwright run checks them).
    """
    problems, builders = [], set(available("query builder"))
    provider = (source.get("auth") or {}).get("provider")
    connector = None
    if provider:
        try:
            connector = load(provider, allowed)
        except ComponentLoadError as exc:
            missing = provider not in available() and (allowed is None or provider in allowed)
            problems.append((("auth", "provider"), str(exc), missing))
        else:
            auth = dict((key, value) for key, value in source["auth"].items() if key != "provider")
            problems += [(("auth",), problem, False) for problem in connector.check_auth(auth)]
    effective_allowed = allowed
    if allowed is not None and connector is not None:
        extra = getattr(connector, "query_builders", ())
        if extra:
            effective_allowed = list(allowed) + [b for b in extra if b not in allowed]
    for index, stream in enumerate(source.get("streams") or []):
        items = stream.get("requests") if isinstance(stream, dict) else None
        for position, item in enumerate(items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            request, path = request_body(item), ("streams", index, "requests", position)
            problems += [(path + inner, message, missing) for inner, message, missing in
                         check_queries(request, effective_allowed, builders)]
            if connector is not None:
                for call in sdk_calls(request):
                    problems += [(path, problem, False) for problem in check_call(connector, call, builders)]
    return problems


def path_text(path):
    """("arguments", "where", 0) -> "arguments.where[0]"."""
    text = ""
    for part in path:
        text += "[%d]" % part if isinstance(part, int) else ("." if text else "") + str(part)
    return text


def check_source(source, allowed=None):
    """component_problems() as messages, e.g. "stream 'campaigns': requests[0].arguments.query.gaql.limit: ..."."""
    streams = source.get("streams") or []
    messages = []
    for path, message, _ in component_problems(source, allowed):
        if path[:1] == ("streams",):
            inner = path[2:]
            where = " %s:" % path_text(inner) if len(inner) > 1 else ""
            messages.append("stream %r:%s %s" % (streams[path[1]].get("name"), where, message))
        else:
            messages.append(message)
    return messages

