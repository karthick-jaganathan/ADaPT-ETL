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

__all__ = ["OpenAIAdsConnector"]

SERVICES = {
    "ad_accounts": "/v1/ad_accounts",
    "campaigns": "/v1/campaigns",
    "ad_groups": "/v1/ad_groups",
    "ads": "/v1/ads",
}
ARGUMENTS = ("id", "params")
PAGE_SIZE = 500
TIMEOUT_SECONDS = 60

def _network_details(context):
    def hook(response, *args, **kwargs):
        logs.http_details(response, context.redact)
    return hook

class OpenAIAdsConnector(Connector):
    """
    The `openai_ads` connector: interacts with the OpenAI Advertiser API.
    
    Supports fetching ad accounts, campaigns, ad groups, and ads via pagination,
    as well as delivery insights nested under campaigns, ad groups, and ads.
    Uses the `requests` library to handle REST API calls and rate-limiting.
    """
    name = "openai_ads"
    auth_required = ("advertiser_api_key",)
    auth_optional = ()
    network_loggers = ("urllib3.connectionpool",)
    category = "advertising"
    summary = "OpenAI Ads"

    def check_request(self, request):
        """
        Validates the request dictionary against supported services and methods.
        Returns a list of error strings, or an empty list if valid.
        """
        service = request.get("service")
        method = request.get("method")
        
        if service not in SERVICES:
            return [f"openai_ads: service {service!r} is not supported (supported: {', '.join(SERVICES)})"]
        
        if method not in ("list", "get", "insights"):
            return [f"openai_ads: method {method!r} is not supported (supported: list, get, insights)"]
            
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            return ["openai_ads: `arguments` must be a mapping"]
            
        if "params" in arguments and not isinstance(arguments["params"], dict):
            return ["openai_ads: `params` in arguments must be a mapping"]
            
        if method in ("get", "insights") and not arguments.get("id"):
            return [f"openai_ads: {service}.{method} requires non-empty `id` in arguments"]
            
        if method == "insights" and service not in ("campaigns", "ad_groups", "ads"):
            return [f"openai_ads: {service} does not support `insights` (supported: campaigns, ad_groups, ads)"]
            
        return []

    def connect(self, auth, context):
        """
        Creates and returns an authenticated requests.Session for the OpenAI API.
        Registers the API key as a secret for redaction.
        """
        import requests
        api_key = str(auth.get("advertiser_api_key") or "").strip()
        if not api_key:
            raise ConnectorError("openai_ads: auth 'advertiser_api_key' is empty")
            
        context.secret(str(api_key))
        
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
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
        
        base_url = "https://api.ads.openai.com"
        path = SERVICES[service]
        if method == "get":
            path = f"{path}/{arguments['id']}"
        elif method == "insights":
            path = f"{path}/{arguments['id']}/insights"
            
        url = f"{base_url}{path}"
        
        def fetch_page(current_url, current_params):
            response = client.get(current_url, params=current_params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
            
        if method == "get":
            data = context.call(lambda: fetch_page(url, params))
            yield [data]
            return
            
        # method in ("list", "insights") (Pagination using 'after' token typical for OpenAI)
        current_url = url
        current_params = params.copy()
        
        while True:
            data = context.call(lambda: fetch_page(current_url, current_params))
            items = data.get("data", [])
            
            if items:
                # Yield in chunks
                for i in range(0, len(items), PAGE_SIZE):
                    yield items[i:i + PAGE_SIZE]
                    
            if not data.get("has_more") or not data.get("last_id"):
                break
                
            current_params["after"] = data["last_id"]

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
                    message = body.get("error", {}).get("message", str(exc))
                except Exception:
                    message = str(exc)
                    
                retryable = status in (429, 500, 502, 503, 504)
                retry_after = response.headers.get("Retry-After")
                retry_seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
                
                return ConnectorError(f"openai_ads: HTTP {status} - {message}", code=status, retryable=retryable, retry_after=retry_seconds)
            return ConnectorError(f"openai_ads: {exc}", retryable=False)
            
        if isinstance(exc, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)):
            return ConnectorError(f"openai_ads: {type(exc).__name__}: {exc}", retryable=True)
            
        return None
