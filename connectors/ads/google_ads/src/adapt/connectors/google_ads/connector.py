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
The `google_ads` connector (docs/design/source-format.md, example 1).

- `auth: {provider: google_ads, developer_token, client_id, client_secret, refresh_token, login_customer_id,
  api_version}` builds a GoogleAdsClient.
- A `requests` item `{name, sdk: google_ads, service: GoogleAdsService, method: search_stream | search, arguments:
  {customer_id, query}}` runs a GAQL query, written as `query: {gaql: {...}}` so values are escaped. Each row becomes
  a plain dict keyed like the GAQL fields (`customer.id`, `metrics.clicks`, `ad_group.type`) as in the API's JSON:
  int64 values are text, enums are names, and fields the API does not return are left out, so cast them and give
  defaults in the stream's `transform` steps.
- `CustomerService.list_accessible_customers` lists the customers the credentials can access, as
  {customer_id, resource_name} records (e.g. to partition other streams with `from_stream`).
- `adapt run --log google.ads.googleads.client=DEBUG` shows the client's own request and response logs (INFO: a line
  per call; DEBUG: the payloads), redacted: the access tokens the credentials get are masked like the secrets.

Only these read-only methods can be called.
"""

import importlib
import re

from adapt.core.runtime.components import Connector, ConnectorError
from adapt.core.engine.queries import BuiltQuery


__all__ = ["GoogleAdsConnector", "GoogleAdsConnection", "SERVICES"]

SERVICES = {
    "GoogleAdsService": {"search_stream": ("customer_id", "query"), "search": ("customer_id", "query")},
    "CustomerService": {"list_accessible_customers": ()},
}
DEFAULT_SERVICE = "GoogleAdsService"
# gRPC statuses and Google Ads error codes that are worth retrying
RETRYABLE_STATUSES = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "ABORTED")
RETRYABLE_ERRORS = ("RESOURCE_EXHAUSTED", "RESOURCE_TEMPORARILY_EXHAUSTED", "INTERNAL_ERROR", "TRANSIENT_ERROR",
                    "DEADLINE_EXCEEDED")
_CUSTOMER_ID = re.compile(r"^[0-9]+$")


def _customer_id(value, key):
    text = str(value if value is not None else "").replace("-", "").strip()
    if not _CUSTOMER_ID.match(text):
        raise ConnectorError("google_ads: %s %r is not a Google Ads customer ID (digits; dashes are removed)" % (
            key, value))
    return text


def _api_versions():
    from google.ads.googleads import client as module
    return list(getattr(module, "_VALID_API_VERSIONS", []))


def _gaql_names(data, descriptor):
    """
    Renames, in place, the fields the Python library renamed (`type_`, since `type` is a Python name) back to their
    GAQL names. Only field names change: map keys are data and stay as they are.
    """
    for key in list(data):  # the fields the row has, not the hundreds its message declares
        field = descriptor.fields_by_name.get(key)
        if field is None:
            continue
        message = field.message_type
        if message is not None and not message.GetOptions().map_entry:
            value = data[key]
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):  # well-known types (e.g. timestamps) are rendered as text
                    _gaql_names(item, message)
        if key.endswith("_") and field.json_name == key[:-1]:
            data[field.json_name] = data.pop(key)
    return data


def _rows(response):
    """The rows of one SearchGoogleAdsResponse / SearchGoogleAdsStreamResponse as plain dicts."""
    from google.protobuf.json_format import MessageToDict
    rows = []
    for row in response.results:
        message = getattr(row, "_pb", row)
        rows.append(_gaql_names(MessageToDict(message, preserving_proto_field_name=True), message.DESCRIPTOR))
    return rows


def _error_code(error):
    """The name of a GoogleAdsError's code, e.g. RESOURCE_EXHAUSTED (from `quota_error`)."""
    code = error.error_code
    kind = type(code).pb(code).WhichOneof("error_code")
    if not kind:
        return "UNKNOWN"
    value = getattr(code, kind)
    return getattr(value, "name", str(value))


def _call_details(call):
    """(GoogleAdsFailure or None, request ID or None) from a failed gRPC call's trailing metadata."""
    failure = request_id = None
    metadata = call.trailing_metadata() if callable(getattr(call, "trailing_metadata", None)) else None
    for key, value in metadata or ():
        if key.endswith("googleadsfailure-bin"):  # google.ads.googleads.<version>.errors.googleadsfailure-bin
            parts = key.split(".")
            try:
                module = importlib.import_module("google.ads.googleads.%s.errors.types.errors" % parts[3])
                failure = module.GoogleAdsFailure.deserialize(value)
            except Exception:  # an unknown version or a malformed value: the status is still reported
                failure = None
        elif key == "request-id":
            request_id = value
    return failure, request_id


def _connector_error(status, failure, request_id, message):
    """A ConnectorError from a gRPC status and the Google Ads failure that came with it."""
    codes, details, retry_after = [], [], None
    for error in (failure.errors if failure is not None else ()):
        code = _error_code(error)
        codes.append(code)
        details.append("%s: %s" % (code, error.message))
        delay = error.details.quota_error_details.retry_delay
        seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else getattr(delay, "seconds", 0)
        if seconds:
            retry_after = max(retry_after or 0, seconds)
    retryable = status in RETRYABLE_STATUSES or any(code in RETRYABLE_ERRORS for code in codes)
    text = "; ".join(details) or message or status or "unknown error"
    return ConnectorError("google_ads: %s%s" % (text, " (request id %s)" % request_id if request_id else ""),
                          code=codes[0] if codes else status, retryable=retryable, retry_after=retry_after)


def _remember_tokens(credentials, context):
    """The access tokens the client's credentials hold and get when they refresh are masked like secrets."""
    if credentials is None:
        return
    context.secret(getattr(credentials, "token", None))
    refresh = getattr(credentials, "refresh", None)
    if not callable(refresh):
        return

    def refresh_and_remember(request):
        refresh(request)
        context.secret(getattr(credentials, "token", None))
    try:
        credentials.refresh = refresh_and_remember
    except (AttributeError, TypeError):  # credentials that take no attributes: their tokens are not logged anyway
        pass


class GoogleAdsConnection(object):
    """A GoogleAdsClient and the API version its services use."""

    def __init__(self, client, version=None):
        self.client = client
        self.version = version
        self.services = {}

    def service(self, name):
        if name not in self.services:
            options = {"version": self.version} if self.version else {}
            self.services[name] = self.client.get_service(name, **options)
        return self.services[name]


class GoogleAdsConnector(Connector):

    name = "google_ads"
    auth_required = ("developer_token", "client_id", "client_secret", "refresh_token")
    auth_optional = ("login_customer_id", "api_version")
    # the client's request logs (its LoggingInterceptor: a summary at INFO, requests and responses at DEBUG)
    network_loggers = ("google.ads.googleads.client",)
    category = "advertising"
    summary = "Google Ads (GAQL via the google-ads SDK)"

    def check_request(self, request):
        problems = self._check_call(request)
        query = (request.get("arguments") or {}).get("query") if isinstance(request.get("arguments"), dict) else None
        if isinstance(query, BuiltQuery):  # built at run time, e.g. by the gaql query builder
            return problems
        if isinstance(query, str) and "{{" in query:
            problems.append("google_ads: write the query as `query: {gaql: {...}}` so values are escaped; "
                            "references inside query text are not allowed")
        elif query is not None and not isinstance(query, str):
            problems.append("google_ads: `query` must be GAQL text or a query builder call such as "
                            "`query: {gaql: {...}}` (the gaql builder comes with adapt-google-ads; is it installed "
                            "and allowed?)")
        return problems

    @staticmethod
    def _check_call(request):
        """The service, method and arguments; also for rendered requests, whose built queries may contain {{."""
        service = request.get("service") or DEFAULT_SERVICE
        methods = SERVICES.get(service)
        if methods is None:
            return ["google_ads: service %r is not supported (supported: %s)" % (service, ", ".join(SERVICES))]
        method = request.get("method")
        if method not in methods:
            return ["google_ads: %s.%s is not supported (supported: %s)" % (service, method, ", ".join(methods))]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["google_ads: `arguments` must be a mapping"]
        expected = methods[method]
        problems = ["google_ads: %s.%s needs `%s`" % (service, method, key) for key in expected if key not in arguments]
        problems += ["google_ads: %s.%s does not take `%s` (arguments: %s)" % (
            service, method, key, ", ".join(expected) or "none") for key in arguments if key not in expected]
        return problems

    def connect(self, auth, context):
        from google.ads.googleads.client import GoogleAdsClient
        version = auth.get("api_version") or None
        supported = _api_versions()
        if version and supported and version not in supported:
            raise ConnectorError("google_ads: api_version %r is not supported by the installed google-ads library "
                                 "(supported: %s); upgrade google-ads or change api_version" % (
                                     version, ", ".join(supported)))
        config = {"use_proto_plus": True}
        for key in self.auth_required:
            if auth.get(key) in (None, ""):
                raise ConnectorError("google_ads: auth %r is empty" % key)
            config[key] = str(auth[key])
        for key in ("developer_token", "client_secret", "refresh_token"):  # also when they come from `config`
            context.secret(config[key])
        if auth.get("login_customer_id") not in (None, ""):
            config["login_customer_id"] = _customer_id(auth["login_customer_id"], "login_customer_id")
        try:
            client = GoogleAdsClient.load_from_dict(config, version=version)
        except ValueError as exc:
            raise ConnectorError("google_ads: invalid auth configuration: %s" % exc)
        _remember_tokens(getattr(client, "credentials", None), context)
        return GoogleAdsConnection(client, version)

    def request(self, client, request, context):
        problems = self._check_call(request)
        if problems:
            raise ConnectorError("; ".join(problems))
        service_name = request.get("service") or DEFAULT_SERVICE
        method = request["method"]
        service = client.service(service_name)
        if method == "list_accessible_customers":
            response = context.call(service.list_accessible_customers)
            yield [{"customer_id": name.split("/")[-1], "resource_name": name} for name in response.resource_names]
            return
        arguments = request["arguments"]
        customer_id = _customer_id(arguments["customer_id"], "customer_id")
        query = arguments["query"]
        if not isinstance(query, str) or not query.strip():
            raise ConnectorError("google_ads: `query` is empty")
        pages = self._search_stream if method == "search_stream" else self._search
        for page in pages(service, customer_id, query, context):
            yield page

    @staticmethod
    def _search_stream(service, customer_id, query, context):
        def start():
            batches = iter(service.search_stream(customer_id=customer_id, query=query))
            return next(batches, None), batches
        first, batches = context.call(start)
        if first is None:
            return
        yield _rows(first)
        for batch in batches:  # a failure after the first batch is not retried: its rows were already read
            yield _rows(batch)

    @staticmethod
    def _search(service, customer_id, query, context):
        request = {"customer_id": customer_id, "query": query}
        while True:
            page = context.call(service.search, request=dict(request))
            yield _rows(page)  # the pager's `results` and `next_page_token` are those of the current page
            if not page.next_page_token:
                return
            request["page_token"] = page.next_page_token

    def error(self, exc):
        try:
            import grpc
            from google.ads.googleads.errors import GoogleAdsException
            from google.api_core import exceptions as api_errors
            from google.auth import exceptions as auth_errors
        except ImportError:
            return None
        if isinstance(exc, GoogleAdsException):
            status = exc.error.code().name if callable(getattr(exc.error, "code", None)) else None
            return _connector_error(status, exc.failure, exc.request_id, None)
        if isinstance(exc, api_errors.GoogleAPICallError):
            # the client raises these for RESOURCE_EXHAUSTED, INTERNAL, UNAVAILABLE, ...: Google Ads leaves them
            # unconverted, so the failure details are read from the call
            status = exc.grpc_status_code.name if exc.grpc_status_code is not None else None
            failure, request_id = _call_details(exc.response if exc.response is not None else exc.__cause__)
            return _connector_error(status, failure, request_id, "%s: %s" % (status or exc.code, exc.message))
        if isinstance(exc, api_errors.RetryError):
            return ConnectorError("google_ads: %s" % exc, code="RETRY_DEADLINE", retryable=True)
        if isinstance(exc, auth_errors.RefreshError):
            return ConnectorError("google_ads: the OAuth token refresh failed (check client_id, client_secret and "
                                  "refresh_token): %s" % exc, code="REFRESH_FAILED",
                                  retryable=bool(getattr(exc, "retryable", False)))
        if isinstance(exc, auth_errors.TransportError):
            return ConnectorError("google_ads: cannot reach the OAuth server: %s" % exc, code="TRANSPORT_ERROR",
                                  retryable=True)
        if isinstance(exc, grpc.RpcError) and callable(getattr(exc, "code", None)):
            status = exc.code().name
            failure, request_id = _call_details(exc)
            details = exc.details() if callable(getattr(exc, "details", None)) else str(exc)
            return _connector_error(status, failure, request_id, "%s: %s" % (status, details))
        return None
