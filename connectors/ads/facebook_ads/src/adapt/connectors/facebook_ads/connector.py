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
The `facebook_ads` connector: the Meta Marketing API through the facebook_business SDK.

- `auth: {provider: facebook_ads, access_token, app_id, app_secret, api_version}`: a system-user (or long-lived)
  access token; with `app_secret` every call carries an appsecret_proof.
- A `requests` item `{name, sdk: facebook_ads, service, method, arguments: {id, fields, params}}` reads an object or
  its edges. `service` is the object type (AdAccount, Campaign, AdSet, Ad, AdCreative, Business, User, CustomAudience,
  AdsPixel), `id` its ID (`act_<account id>` for ad accounts), and `method` is `api_get` (the object itself) or a
  `get_*` edge (get_campaigns, get_insights, ...). `params` are the API's parameters; dates are sent as YYYY-MM-DD.
- Edges are paged by the SDK, and every page is fetched with the stream's rate limit and retries. Records are the
  objects' fields as plain dicts; insights numbers are text, as the API returns them, so cast them in the stream's
  `transform` steps.
- Throttling errors (codes 4, 17, 32, 613, 80000-80014), temporary errors and HTTP 5xx are retried, waiting as long
  as the x-business-use-case-usage header asks.
- `adapt run --log urllib3.connectionpool=DEBUG` shows each request's method, URL and status, and
  `--log adapt.network=DEBUG` its headers and bodies, redacted: the access token and appsecret_proof are masked.
"""

import datetime
import json
import re

from adapt.core.runtime import logs
from adapt.core.runtime.components import Connector, ConnectorError


__all__ = ["FacebookAdsConnector", "OBJECTS"]

OBJECTS = {"AdAccount": "adaccount", "Campaign": "campaign", "AdSet": "adset", "Ad": "ad",
           "AdCreative": "adcreative", "Business": "business", "User": "user", "CustomAudience": "customaudience",
           "AdsPixel": "adspixel"}
ARGUMENTS = ("id", "fields", "params")
RETRYABLE_CODES = (1, 2, 4, 17, 32, 341, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014)
PAGE_SIZE = 500
TIMEOUT_SECONDS = 300  # insights can take minutes
_API_VERSION = re.compile(r"^v[0-9]+\.[0-9]+$")


def _jsonable(value):
    if isinstance(value, dict):
        return dict((key, _jsonable(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _export(item):
    return item.export_all_data() if hasattr(item, "export_all_data") else item


def _object_class(name):
    import importlib
    module = importlib.import_module("facebook_business.adobjects.%s" % OBJECTS[name])
    return getattr(module, name)


def _regain_seconds(headers):
    """Seconds until access is restored, from the x-business-use-case-usage header (in minutes)."""
    value = None
    for key in headers or {}:
        if key.lower() == "x-business-use-case-usage":
            value = headers[key]
    try:
        usage = json.loads(value) if value else {}
    except ValueError:
        return None
    minutes = [entry.get("estimated_time_to_regain_access") or 0 for entries in usage.values()
               if isinstance(entries, list) for entry in entries if isinstance(entry, dict)]
    longest = max(minutes or [0])
    return longest * 60 if longest else None


def _network_details(context):
    """A requests response hook: the exchange's headers and bodies on `adapt.network` at DEBUG."""
    def hook(response, *args, **kwargs):
        logs.http_details(response, context.redact)
    return hook


class FacebookAdsConnector(Connector):

    name = "facebook_ads"
    auth_required = ("access_token",)
    auth_optional = ("app_id", "app_secret", "api_version")
    # the SDK sends its requests with requests: urllib3 logs each one's method, URL and status
    network_loggers = ("urllib3.connectionpool",)
    category = "advertising"
    summary = "Meta / Facebook Ads (facebook-business SDK)"

    def check_request(self, request):
        service, method = request.get("service"), request.get("method")
        if service not in OBJECTS:
            return ["facebook_ads: service %r is not supported (supported: %s)" % (service, ", ".join(OBJECTS))]
        if not isinstance(method, str) or not (method == "api_get" or method.startswith("get_") and
                                               not method.endswith("_async")):
            return ["facebook_ads: %s.%s is not a read (supported: api_get and get_* edges)" % (service, method)]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["facebook_ads: `arguments` must be a mapping"]
        problems = []
        if "id" not in arguments:
            problems.append("facebook_ads: %s.%s needs `id` (e.g. act_<account id> for an ad account)" % (
                service, method))
        problems += ["facebook_ads: %s.%s does not take `%s` (arguments: %s)" % (service, method, key,
                                                                                  ", ".join(ARGUMENTS))
                     for key in arguments if key not in ARGUMENTS]
        if not isinstance(arguments.get("params") or {}, dict):
            problems.append("facebook_ads: `params` must be a mapping")
        try:
            if not hasattr(_object_class(service), method):
                problems.append("facebook_ads: %s has no method %r" % (service, method))
        except ImportError:  # the SDK is missing: connect() reports it
            pass
        return problems

    def connect(self, auth, context):
        from facebook_business.api import FacebookAdsApi
        from facebook_business.session import FacebookSession
        if auth.get("access_token") in (None, ""):
            raise ConnectorError("facebook_ads: auth 'access_token' is empty")
        version = auth.get("api_version") or None
        if version and not _API_VERSION.match(str(version)):
            raise ConnectorError("facebook_ads: api_version %r is not a Graph API version such as v26.0" % (version,))
        session = FacebookSession(app_id=auth.get("app_id") or None, app_secret=auth.get("app_secret") or None,
                                  access_token=str(auth["access_token"]), timeout=TIMEOUT_SECONDS)
        context.secret(str(auth["access_token"]))  # also when it comes from `config`
        context.secret(auth.get("app_secret"))
        if auth.get("app_secret"):
            context.secret(session.appsecret_proof)
        session.requests.hooks["response"].append(_network_details(context))
        # an API object of our own: no global default API and no crash reporter
        return FacebookAdsApi(session, api_version=version)

    def request(self, client, request, context):
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))
        arguments = request["arguments"]
        node = _object_class(request["service"])(str(arguments["id"]), api=client)
        method = getattr(node, request["method"])
        fields = arguments.get("fields") or []
        if isinstance(fields, str):
            fields = [field.strip() for field in fields.split(",") if field.strip()]
        params = _jsonable(arguments.get("params") or {})
        result = context.call(lambda: method(fields=fields, params=params))  # edges load their first page here
        if request["method"] == "api_get":
            yield [_export(result)]
            return
        load_next_page = result.load_next_page

        def next_page():  # every later page: rate limit and retries; none once the edge is read (no request)
            if getattr(result, "_finished_iteration", False):
                return False
            return context.call(load_next_page)
        result.load_next_page = next_page
        page = []
        for item in result:
            page.append(_export(item))
            if len(page) >= PAGE_SIZE:
                yield page
                page = []
        if page:
            yield page

    def error(self, exc):
        try:
            import requests
            from facebook_business.exceptions import FacebookRequestError
        except ImportError:
            return None
        if isinstance(exc, FacebookRequestError):
            code, subcode, status = exc.api_error_code(), exc.api_error_subcode(), exc.http_status()
            body = exc.body() if isinstance(exc.body(), dict) else {}
            trace = (body.get("error") or {}).get("fbtrace_id") if isinstance(body.get("error"), dict) else None
            details = ["code %s" % code] + (["subcode %s" % subcode] if subcode else []) + ["HTTP %s" % status] + \
                (["fbtrace_id %s" % trace] if trace else [])
            retryable = bool(exc.api_transient_error()) or code in RETRYABLE_CODES or \
                (isinstance(status, int) and status >= 500)
            return ConnectorError("facebook_ads: %s (%s)" % (exc.api_error_message() or exc.get_message(),
                                                             ", ".join(details)),
                               code=code, retryable=retryable, retry_after=_regain_seconds(exc.http_headers()))
        if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError)):  # including connections dropped mid-page
            return ConnectorError("facebook_ads: %s: %s" % (type(exc).__name__, exc), retryable=True)
        return None
