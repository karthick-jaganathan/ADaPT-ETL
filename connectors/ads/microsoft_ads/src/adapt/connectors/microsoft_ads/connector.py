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
The `microsoft_ads` connector (docs/design/source-format.md, example 3), on the bingads SDK (API v13).

- `auth: {provider: microsoft_ads, developer_token, client_id, refresh_token, client_secret, tenant, customer_id,
  account_id, environment}` gets an OAuth token from the Microsoft identity platform (with `client_secret` for web
  apps, without it for desktop and mobile apps); the SDK refreshes it while the run lasts.
- A `requests` item `{name, sdk: microsoft_ads, service, method, arguments}` calls a read-only operation of
  CampaignManagementService, ReportingService, CustomerManagementService or AdInsightService: methods named Get*,
  Search*, Find* and Poll*, and SubmitGenerateReport. `arguments` are the operation's fields: nested objects are
  mappings, abstract types name their concrete type with "@type" (e.g. CampaignPerformanceReportRequest), lists fill
  ArrayOf* types (and are joined with spaces for list values such as CampaignType), and dates fill Date objects.
  Unknown fields are errors.
- Responses become plain dicts. A response with one part is that part, e.g. GetCampaignsByAccountId returns
  {"Campaign": [...]} (`records: {path: Campaign}`); one with several parts maps their names to them, e.g.
  GetCampaignCriterionsByIds returns {"CampaignCriterions": {"CampaignCriterion": [...]}, "PartialErrors": ...}.
- The CustomerAccountId header is the request's `headers: {CustomerAccountId: ...}`, else its `AccountId`, else the
  only account of a report's `Scope.AccountIds`, else `auth.account_id`. Set it in `headers` for operations about
  an account's entities that do not name the account, e.g. GetAdGroupsByCampaignId. `headers: {CustomerId: ...}`
  replaces `auth.customer_id` for one request.

Reports are async jobs: SubmitGenerateReport, PollGenerateReport until Status is Success, then the zipped CSV at
ReportDownloadUrl (with ExcludeReportHeader and ExcludeReportFooter, so the file is a plain CSV table).

`adapt run --log suds.client=DEBUG --log suds.transport=DEBUG` shows the SOAP messages the SDK sends and receives,
redacted: the developer token, the OAuth tokens (refreshed ones too) and the signatures of report URLs are masked.
"""

import datetime
import http.client
import socket
import urllib.error

from adapt.core.config.inputs import InputError, as_date
from adapt.core.runtime.components import Connector, ConnectorError


__all__ = ["MicrosoftAdsConnector", "MicrosoftAdsConnection", "SERVICES"]

API_VERSION = 13
SERVICES = ("CampaignManagementService", "ReportingService", "CustomerManagementService", "AdInsightService")
READ_PREFIXES = ("Get", "Search", "Find", "Poll")
READ_METHODS = ("SubmitGenerateReport",)
ENVIRONMENTS = ("production", "sandbox")
# InternalError, CallRateExceeded, ConcurrentRequestOverLimit (too many report requests running)
RETRYABLE_CODES = (0, 117, 207)
WAIT_CODES = (117, 207)  # Microsoft asks for a pause before trying again
NATIVE_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"


def _is_object(value):
    return hasattr(value, "__keylist__")


def _keys(value):
    return list(value.__keylist__)


def _text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


class _Builder(object):
    """Builds suds request objects from plain arguments, using the WSDL's templates."""

    def __init__(self, factory, method):
        self.factory = factory
        self.method = method

    def create(self, type_name, path):
        try:
            return self.factory.create(type_name)
        except Exception as exc:  # suds.TypeNotFound and friends
            raise ConnectorError("microsoft_ads: %s: unknown type %r (%s)" % (path, type_name, exc))

    def fill(self, target, values, path):
        if not isinstance(values, dict):
            raise ConnectorError("microsoft_ads: %s expects a mapping of fields, got %r" % (path, values))
        keys = _keys(target)
        templates = dict((key, getattr(target, key)) for key in keys)
        for key in keys:  # only the fields given are sent
            setattr(target, key, None)
        for key, value in values.items():
            if key == "@type":
                continue
            if key not in templates:
                raise ConnectorError("microsoft_ads: %s has no field %r (fields: %s)" % (path, key, ", ".join(keys)))
            setattr(target, key, self.convert(templates[key], value, "%s.%s" % (path, key)))
        return target

    def convert(self, template, value, path):
        if value is None:
            return None
        if isinstance(value, dict) and "@type" in value:
            return self.fill(self.create(value["@type"], path), value, path)
        if not _is_object(template):  # a simple value; lists are XML list values ("Search Shopping")
            if isinstance(value, (list, tuple)):
                return " ".join(_text(item) for item in value)
            if isinstance(value, (dict,)):
                raise ConnectorError("microsoft_ads: %s expects a value, got a mapping" % path)
            return _text(value) if isinstance(value, (datetime.date, datetime.datetime)) else value
        keys = _keys(template)
        if keys == ["value"]:  # an enumeration
            return " ".join(_text(item) for item in value) if isinstance(value, (list, tuple)) else _text(value)
        if sorted(keys) == ["Day", "Month", "Year"]:
            try:
                day = as_date(value)
            except InputError as exc:
                raise ConnectorError("microsoft_ads: %s: %s" % (path, exc))
            template.Day, template.Month, template.Year = day.day, day.month, day.year
            return template
        if len(keys) == 1 and isinstance(getattr(template, keys[0]), list):  # ArrayOf<Item>
            item_type = keys[0]
            items = value if isinstance(value, (list, tuple)) else [value]
            setattr(template, item_type, [self.item(item_type, item, "%s[%d]" % (path, i))
                                          for i, item in enumerate(items)])
            return template
        return self.fill(template, value, path)

    def item(self, item_type, value, path):
        if isinstance(value, dict):
            return self.fill(self.create(value.get("@type", item_type), path), value, path)
        return _text(value) if isinstance(value, (bool, datetime.date)) else value

    def arguments(self, values):
        wrapper = self.fill(self.create("%sRequest" % self.method, self.method), values, self.method)
        return dict((key, getattr(wrapper, key)) for key in _keys(wrapper) if getattr(wrapper, key) is not None)


def _plain(value):
    """A suds response as plain data."""
    if _is_object(value):
        return dict((key, _plain(getattr(value, key))) for key in _keys(value))
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, str):
        return str(value)  # suds Text
    return value


def _account_id(arguments):
    """The account a request is about: its AccountId, or the only account of a report scope."""
    if isinstance(arguments.get("AccountId"), (str, int)) and not isinstance(arguments.get("AccountId"), bool):
        return arguments["AccountId"]
    for value in arguments.values():
        scope = value.get("Scope") if isinstance(value, dict) else None
        ids = scope.get("AccountIds") if isinstance(scope, dict) else None
        if isinstance(ids, list) and len(ids) == 1:
            return ids[0]
    return None


def _fault_errors(node, found):
    """(code, error_code, message) of every error object in a SOAP fault detail."""
    if _is_object(node):
        keys = _keys(node)
        if "Code" in keys and "Message" in keys:
            found.append((getattr(node, "Code", None), getattr(node, "ErrorCode", None),
                          getattr(node, "Message", None)))
        for key in keys:
            _fault_errors(getattr(node, key), found)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _fault_errors(item, found)
    return found


def _tracking_id(node):
    if _is_object(node):
        if "TrackingId" in _keys(node) and getattr(node, "TrackingId", None):
            return str(node.TrackingId)
        for key in _keys(node):
            found = _tracking_id(getattr(node, key))
            if found:
                return found
    return None


def _http_status(exc):
    """suds reports an HTTP error that is not a SOAP fault as Exception((status, reason))."""
    if type(exc) is Exception and len(exc.args) == 1 and isinstance(exc.args[0], tuple) and \
            len(exc.args[0]) == 2 and isinstance(exc.args[0][0], int):
        return exc.args[0]
    return None


def _fault_error(exc):
    fault = getattr(exc, "fault", None)
    detail = getattr(fault, "detail", None)
    errors = _fault_errors(detail, [])
    codes = []
    for code, _, _ in errors:
        try:
            codes.append(int(code))
        except (TypeError, ValueError):
            pass
    text = "; ".join("%s %s: %s" % (code, name or "", message) for code, name, message in errors) or \
        str(getattr(fault, "faultstring", exc))
    tracking = _tracking_id(detail)
    return ConnectorError("microsoft_ads: %s%s" % (text, " (tracking id %s)" % tracking if tracking else ""),
                          code=codes[0] if codes else None, retryable=any(code in RETRYABLE_CODES for code in codes),
                          retry_after=60 if any(code in WAIT_CODES for code in codes) else None)


class MicrosoftAdsConnection(object):
    """The OAuth session plus one bingads ServiceClient per service."""

    def __init__(self, authentication, developer_token, customer_id=None, account_id=None, environment="production"):
        self.authentication = authentication
        self.developer_token = developer_token
        self.customer_id = customer_id
        self.account_id = account_id
        self.environment = environment
        self.services = {}

    def service(self, name, account_id=None, customer_id=None):
        from bingads import AuthorizationData, ServiceClient
        if name not in self.services:
            data = AuthorizationData(account_id=None, customer_id=self.customer_id,
                                     developer_token=self.developer_token, authentication=self.authentication)
            self.services[name] = ServiceClient(name, API_VERSION, authorization_data=data,
                                                environment=self.environment)
        client = self.services[name]
        # the SDK builds the SOAP headers from this when an operation is looked up
        client.authorization_data.account_id = account_id if account_id is not None else self.account_id
        client.authorization_data.customer_id = customer_id if customer_id is not None else self.customer_id
        return client


class MicrosoftAdsConnector(Connector):

    name = "microsoft_ads"
    auth_required = ("developer_token", "client_id", "refresh_token")
    auth_optional = ("client_secret", "tenant", "customer_id", "account_id", "environment")
    request_headers = ("CustomerAccountId", "CustomerId")
    # suds, the SDK's SOAP client: the messages sent and received (suds.client), and their HTTP exchanges
    network_loggers = ("suds.client", "suds.transport")
    category = "advertising"
    summary = "Microsoft Advertising (Bing Ads SDK)"

    def check_request(self, request):
        service, method = request.get("service"), request.get("method")
        if service not in SERVICES:
            return ["microsoft_ads: service %r is not supported (supported: %s)" % (service, ", ".join(SERVICES))]
        if not isinstance(method, str) or not (method.startswith(READ_PREFIXES) or method in READ_METHODS):
            return ["microsoft_ads: %s.%s is not a read-only operation (supported: Get*, Search*, Find*, Poll*, %s)"
                    % (service, method, ", ".join(READ_METHODS))]
        if not isinstance(request.get("arguments") or {}, dict):
            return ["microsoft_ads: `arguments` must be a mapping"]
        return []

    def connect(self, auth, context):
        from bingads.authorization import OAuthDesktopMobileAuthCodeGrant, OAuthWebAuthCodeGrant
        for key in self.auth_required:
            if auth.get(key) in (None, ""):
                raise ConnectorError("microsoft_ads: auth %r is empty" % key)
        for key in ("developer_token", "client_secret", "refresh_token"):  # also when they come from `config`
            context.secret(auth.get(key))
        environment = auth.get("environment") or "production"
        if environment not in ENVIRONMENTS:
            raise ConnectorError("microsoft_ads: environment %r is not one of: %s" % (environment,
                                                                                   ", ".join(ENVIRONMENTS)))
        options = {"env": environment, "tenant": str(auth.get("tenant") or "common")}
        if auth.get("client_secret"):
            oauth = OAuthWebAuthCodeGrant(str(auth["client_id"]), str(auth["client_secret"]), NATIVE_REDIRECT_URI,
                                          **options)
        else:
            oauth = OAuthDesktopMobileAuthCodeGrant(client_id=str(auth["client_id"]), **options)

        def remember(tokens):  # refreshed tokens are secrets too
            context.secret(tokens.access_token)
            context.secret(tokens.refresh_token)
        oauth.token_refreshed_callback = remember
        oauth.request_oauth_tokens_by_refresh_token(str(auth["refresh_token"]))
        return MicrosoftAdsConnection(oauth, str(auth["developer_token"]), customer_id=auth.get("customer_id"),
                                      account_id=auth.get("account_id"), environment=environment)

    def request(self, client, request, context):
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))
        method, arguments, headers = request["method"], request.get("arguments") or {}, request.get("headers") or {}
        account_id = headers.get("CustomerAccountId")
        service = client.service(request["service"], account_id if account_id not in (None, "") else
                                 _account_id(arguments), headers.get("CustomerId") or None)
        values = _Builder(service.factory, method).arguments(arguments)
        yield _plain(context.call(lambda: getattr(service, method)(**values)))

    def error(self, exc):
        try:
            import requests
            from bingads.exceptions import OAuthTokenRequestException
            from suds import WebFault
        except ImportError:
            return None
        if isinstance(exc, WebFault):
            return _fault_error(exc)
        if isinstance(exc, OAuthTokenRequestException):
            return ConnectorError("microsoft_ads: the OAuth token refresh failed (check client_id, client_secret, "
                                  "tenant and refresh_token): %s: %s" % (exc.error_code, exc.error_description),
                                  code=exc.error_code,
                                  retryable=exc.error_code in ("temporarily_unavailable", "server_error"))
        reply = _http_status(exc)
        if reply is not None:
            status, reason = reply
            return ConnectorError("microsoft_ads: HTTP %d: %s" % (status, reason), code=status,
                                  retryable=status == 429 or status >= 500)
        # connection failures: urllib (the SOAP calls) and requests (OAuth)
        if isinstance(exc, (urllib.error.URLError, http.client.HTTPException, ConnectionError, socket.timeout,
                            TimeoutError, requests.ConnectionError, requests.Timeout)):
            return ConnectorError("microsoft_ads: %s: %s" % (type(exc).__name__, exc), retryable=True)
        return None
