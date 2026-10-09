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

from streamwright.core.runtime.components import Connector, ConnectorError
from streamwright.core.runtime import logs

__all__ = ["LinkedInAdsConnector"]

SERVICES = {
    "ad_accounts": "/rest/adAccounts",
    "campaign_groups": "/rest/adCampaignGroups",
    "campaigns": "/rest/adCampaigns",
    "creatives": "/rest/adCreatives",
    "ad_analytics": "/rest/adAnalytics",
}
ARGUMENTS = ("id", "params")
DEFAULT_API_VERSION = "202401"
DEFAULT_PAGE_SIZE = 100
TIMEOUT_SECONDS = 60

def _flatten_params(params, prefix=""):
    """
    Flattens nested dictionaries and lists into Rest.li 2.0 query parameter syntax.
    e.g. {"dateRange": {"start": {"year": 2026}}} -> {"dateRange.start.year": 2026}
    and {"accounts": ["urn:li:..."]} -> {"accounts[0]": "urn:li:..."}
    """
    items = {}
    for key, val in params.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            items.update(_flatten_params(val, full_key))
        elif isinstance(val, (list, tuple)):
            for i, item in enumerate(val):
                items[f"{full_key}[{i}]"] = item
        else:
            items[full_key] = val
    return items

def _network_details(context):
    def hook(response, *args, **kwargs):
        logs.http_details(response, context.redact)
    return hook

class LinkedInAdsConnector(Connector):
    """
    The `linkedin_ads` connector: interacts with the LinkedIn Marketing Developer Platform.
    
    Supports fetching ad accounts, campaign groups, campaigns, creatives, and ad analytics
    via Rest.li offset pagination (start/count), and retrieving individual entities by ID.
    Uses the `requests` library to handle REST API calls and rate-limiting.
    """
    name = "linkedin_ads"
    auth_required = ("access_token",)
    auth_optional = ("api_version",)
    network_loggers = ("urllib3.connectionpool",)
    category = "advertising"
    summary = "LinkedIn Ads"

    def check_request(self, request):
        """
        Validates the request dictionary against supported services and methods.
        Returns a list of error strings, or an empty list if valid.
        """
        service = request.get("service")
        method = request.get("method")
        
        if service not in SERVICES:
            return [f"linkedin_ads: service {service!r} is not supported (supported: {', '.join(SERVICES)})"]
        
        if method not in ("list", "get", "analytics"):
            return [f"linkedin_ads: method {method!r} is not supported (supported: list, get, analytics)"]
            
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["linkedin_ads: `arguments` must be a mapping"]
            
        if "params" in arguments and not isinstance(arguments["params"], dict):
            return ["linkedin_ads: `params` in arguments must be a mapping"]
            
        if method == "get" and not arguments.get("id"):
            return [f"linkedin_ads: {service}.{method} requires non-empty `id` in arguments"]
            
        if method == "analytics" and service != "ad_analytics":
            return [f"linkedin_ads: {service} does not support `analytics` (supported: ad_analytics)"]
            
        return []

    def connect(self, auth, context):
        """
        Creates and returns an authenticated requests.Session for the LinkedIn REST API.
        Registers the access token as a secret for redaction.
        """
        import requests
        access_token = str(auth.get("access_token") or "").strip()
        if not access_token:
            raise ConnectorError("linkedin_ads: auth 'access_token' is empty")
            
        context.secret(access_token)
        
        api_version = str(auth.get("api_version") or DEFAULT_API_VERSION).strip()
        
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        })
        session.hooks["response"].append(_network_details(context))
        return session

    def request(self, client, request, context):
        """
        Executes a data retrieval request using the provided API client.
        Yields pages of records (lists of dictionaries) and uses `context.call()` to track network time.
        """
        problems = self.check_request(request)
        if problems:
            raise ConnectorError("; ".join(problems))
            
        service = request["service"]
        method = request["method"]
        arguments = request.get("arguments") or {}
        params = arguments.get("params") or {}
        
        base_url = "https://api.linkedin.com"
        path = SERVICES[service]
        
        def fetch_page(current_url, current_params):
            response = client.get(current_url, params=current_params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
            
        if method == "get":
            url = f"{base_url}{path}/{arguments['id']}"
            data = context.call(fetch_page, url, _flatten_params(params))
            yield [data]
            return
            
        # method in ("list", "analytics") (Rest.li offset-based pagination: start / count)
        url = f"{base_url}{path}"
        current_params = _flatten_params(params)
        
        page_size = int(current_params.get("count", DEFAULT_PAGE_SIZE))
        current_params["count"] = page_size
        start = int(current_params.get("start", 0))
        current_params["start"] = start
        
        while True:
            data = context.call(fetch_page, url, current_params)
            elements = data.get("elements", [])
            
            if elements:
                for i in range(0, len(elements), DEFAULT_PAGE_SIZE):
                    yield elements[i:i + DEFAULT_PAGE_SIZE]
                    
            paging = data.get("paging") or {}
            total = paging.get("total")
            
            if not elements:
                break
                
            start += len(elements)
            if total is not None and start >= total:
                break
                
            if len(elements) < page_size:
                break
                
            current_params["start"] = start

    def error(self, exc):
        """
        Translates a request exception into a ConnectorError.
        Determines whether the error is retryable (e.g., HTTP 429) and parses the retry delay.
        """
        import requests
        if isinstance(exc, requests.HTTPError):
            response = getattr(exc, "response", None)
            if response is not None:
                status = response.status_code
                try:
                    body = response.json()
                    message = body.get("message") or body.get("error", {}).get("message") or str(exc)
                except Exception:
                    message = str(exc)
                    
                retryable = status in (429, 500, 502, 503, 504)
                retry_after = response.headers.get("Retry-After")
                retry_seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
                
                return ConnectorError(f"linkedin_ads: HTTP {status} - {message}", code=status, retryable=retryable, retry_after=retry_seconds)
            return ConnectorError(f"linkedin_ads: {exc}", retryable=False)
            
        if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)):
            return ConnectorError(f"linkedin_ads: {type(exc).__name__}: {exc}", retryable=True)
            
        return None
