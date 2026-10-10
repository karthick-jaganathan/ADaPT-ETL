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
The `meta_ads` connector: Meta Marketing API campaigns, ad sets, ads, and insights extraction.

Connects to Meta Marketing API using official `facebook_business` SDK.
Supports object reads and graph edge traversals (`get_campaigns`, `get_insights`) with automatic pagination and throttling handling.

### 1. `source.yaml` Contract
```yaml
kind: source
name: meta_ads_pipeline
spec:
  secrets:
    meta_access_token: {type: string, required: true}
    meta_app_secret: {type: string, required: false}

auth:
  provider: meta_ads
  access_token: "{{ secrets.meta_access_token }}" # System-user or long-lived user token
  # Optional:
  # app_id: "123456789"
  # app_secret: "{{ secrets.meta_app_secret }}"  # Enables appsecret_proof verification
  # api_version: "v21.0"
```

### 2. `streams/<stream>.yaml` Contract
```yaml
requests:
  # Method 1: Edge traversal on AdAccount (e.g. insights)
  - name: campaign_insights
    sdk: meta_ads
    service: AdAccount
    method: get_insights
    arguments:
      id: "act_1234567890"             # Account ID prefixed with act_
      fields:
        - "campaign_id"
        - "campaign_name"
        - "impressions"
        - "clicks"
        - "spend"
        - "date_start"
        - "date_stop"
      params:
        level: "campaign"
        time_range:
          since: "{{ window.start }}"
          until: "{{ window.end }}"

  # Method 2: Entity inspection (e.g. read campaign details)
  # - name: campaign_details
  #   sdk: meta_ads
  #   service: Campaign
  #   method: api_get
  #   arguments:
  #     id: "120210000000"
  #     fields: ["id", "name", "objective", "status", "daily_budget"]

transform:
  - name: final_insights
    select: |
      SELECT 
        campaign_id::BIGINT AS campaign_id,
        campaign_name,
        impressions::BIGINT AS impressions,
        clicks::BIGINT AS clicks,
        spend::DOUBLE AS spend,
        date_start AS report_date
      FROM campaign_insights

export:
  insights:
    step: final_insights
    primary_key: [campaign_id, report_date]
```

### 3. Authentication & Security
- `provider`: `meta_ads`
- Credentials: `access_token` and optional `app_secret` must be `{{ secrets.* }}` references. Credentials and appsecret proofs are never written in source configs and are redacted from all logs.
- Security: Sandboxed SDK client. Only read-only operations (`api_get` and `get_*` edge methods) on allowed ad objects are permitted.

### 4. Transform & Data Shaping
- Emits records as JSON dictionaries representing API entity fields. Numeric metric fields (e.g. `spend`, `clicks`) are returned as strings by Meta API and should be cast in SQL transforms.
- In `transform` steps, request results are available as relational tables in DuckDB SQL.

### 5. Execution & Behavior
- **Transport**: `sdk` (`facebook_business`).
- **Services & Methods**:
  - Services: `AdAccount`, `Campaign`, `AdSet`, `Ad`, `AdCreative`, `Business`, `User`, `CustomAudience`, `AdsPixel`.
  - Methods: `api_get` (fetch object), `get_<edge>` (fetch collection, e.g. `get_insights`, `get_campaigns`, `get_ads`).
- **Streaming**: Yields rows in cursor pages of up to 500 records.
- **Throttling**: Automatically backs off according to `x-business-use-case-usage` headers and retries rate limit errors.
"""

import datetime
import json
import re

from streamwright.core.runtime import logs
from streamwright.core.runtime.components import Connector, ConnectorError, ConnectorSpec


__all__ = ["MetaAdsConnector", "OBJECTS"]

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
    """A requests response hook: the exchange's headers and bodies on `streamwright.network` at DEBUG."""
    def hook(response, *args, **kwargs):
        logs.http_details(response, context.redact)
    return hook


class MetaAdsConnector(Connector):

    spec = ConnectorSpec(
        name="meta_ads",
        title="Meta Ads",
        category="advertising",
        transport="sdk",
        package="facebook-business",
        loggers=("urllib3.connectionpool",),
    )
    auth_required = ("access_token",)
    auth_optional = ("app_id", "app_secret", "api_version")

    def check_request(self, request):
        service, method = request.get("service"), request.get("method")
        if service not in OBJECTS:
            return ["meta_ads: service %r is not supported (supported: %s)" % (service, ", ".join(OBJECTS))]
        if not isinstance(method, str) or not (method == "api_get" or method.startswith("get_") and
                                               not method.endswith("_async")):
            return ["meta_ads: %s.%s is not a read (supported: api_get and get_* edges)" % (service, method)]
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["meta_ads: `arguments` must be a mapping"]
        problems = []
        if "id" not in arguments:
            problems.append("meta_ads: %s.%s needs `id` (e.g. act_<account id> for an ad account)" % (
                service, method))
        problems += ["meta_ads: %s.%s does not take `%s` (arguments: %s)" % (service, method, key,
                                                                                  ", ".join(ARGUMENTS))
                     for key in arguments if key not in ARGUMENTS]
        if not isinstance(arguments.get("params") or {}, dict):
            problems.append("meta_ads: `params` must be a mapping")
        try:
            if not hasattr(_object_class(service), method):
                problems.append("meta_ads: %s has no method %r" % (service, method))
        except ImportError:  # the SDK is missing: connect() reports it
            pass
        return problems

    def connect(self, auth, context):
        from facebook_business.api import FacebookAdsApi
        from facebook_business.session import FacebookSession
        if auth.get("access_token") in (None, ""):
            raise ConnectorError("meta_ads: auth 'access_token' is empty")
        version = auth.get("api_version") or None
        if version and not _API_VERSION.match(str(version)):
            raise ConnectorError("meta_ads: api_version %r is not a Graph API version such as v26.0" % (version,))
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
            return ConnectorError("meta_ads: %s (%s)" % (exc.api_error_message() or exc.get_message(),
                                                             ", ".join(details)),
                               code=code, retryable=retryable, retry_after=_regain_seconds(exc.http_headers()))
        if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ContentDecodingError)):  # including connections dropped mid-page
            return ConnectorError("meta_ads: %s: %s" % (type(exc).__name__, exc), retryable=True)
        return None
