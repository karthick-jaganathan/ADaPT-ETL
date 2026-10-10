# REST API connectors: one declarative engine — implementation plan

**Status:** approved design, ready to implement. **Implementer:** read this whole document before changing code.
Every decision in §2 is final; do not re-open them. If something here is impossible, stop and report it — do not
work around it with per-network code.

---

## 1. Problem

StreamWright has two ways to call an HTTP API today, and the REST connectors use the wrong one.

| | Core `http:` sources (`net/http.py`, `engine/runner.py`) | `RestConnector` (`runtime/rest.py`) + 4 subclasses |
|---|---|---|
| Retries, `Retry-After`, 401 token refresh | yes (`HttpClient`) | partial, re-implemented |
| Rate limiting, request metrics, `streamwright.network` logs, redaction | yes | no / partial |
| Auth types (bearer, api_key, basic, oauth2 refresh) | yes (`Authenticator`) | re-implemented per class |
| Pagination | `paginate()`: offset, page_number, cursor (+ loop detection) | 4 hand-written `while True` loops |
| Record extraction | explicit `records.path` | **guesses** envelopes (`result`, `rows`, `elements`, `data.reportingDataResponse.row`, "the only list in the dict"…) |
| Network specifics | none — all in YAML | Python class attributes and overrides per network |

Concrete defects in the current REST code:
- `RestConnector` re-implements HTTP execution (~550 lines) instead of using `HttpClient`, so REST connectors miss
  rate limits, metrics and network log lines.
- `extract_records()` / `extract_total()` guess response shapes; one guess is Apple-specific
  (`reportingDataResponse`). A wrong guess silently returns `[]`.
- Each network is configured with Python class attributes (`pagination_type`, `cursor_param`,
  `append_query_suffix`, `method_http_verbs`, …) plus overrides (`configure_session`, `build_params`,
  `resolve_path`, `custom_check_request`). Adding a network means writing Python.
- Streams use invented verbs (`list`, `query`, `get`, `insights`, `reports`, `analytics`) that each class maps
  to HTTP differently (Apple's `query` → `POST {path}/query`; OpenAI's `insights` → `GET {path}/{id}/insights`).
- `AmazonAdsConnector` does not use `RestConnector` at all: it has its own session, OAuth refresh, `SERVICES`
  map, response-key list and pagination loop.
- The `rest` entry point in `core/pyproject.toml` registers a generic connector that duplicates `http:` sources.

## 2. Decisions (final)

- **D1 — The API is declared in the user's `source.yaml`.** Base URL, auth scheme, headers, default
  pagination, default record path, parameter encoding, retry and rate limit all live in the source's `auth:` and
  `http:` blocks. Nothing network-specific lives in core, and connector packages contain no network logic.
- **D2 — Streams keep raw HTTP.** A request is `http: {path, method, params, headers, json}`: real paths and real
  HTTP verbs, plus a per-request `paginator:` / `records:` when they differ from the source's defaults. The
  invented verbs (`list`, `query`, `insights`, …) and `arguments:` go away for REST networks.
- **D3 — One engine.** REST connectors run through the **same** core HTTP pipeline as plain `http:` sources
  (`HttpClient` + `Authenticator` + the paginator registry + `records`). `RestConnector`'s execution code is
  deleted, not moved. Improvements made for REST networks (new paginator options, header rules, parameter
  encoding) are available to every `http:` source.
- **D4 — `pip install` connectors stay, as placeholders.** `streamwright-openai-ads` etc. still exist, still
  register their name through the `streamwright.connectors` entry point, and are still required by
  `auth.provider: openai_ads` (so `streamwright connectors install`, `--allow-connector` and the hub keep working).
  Their class subclasses the **existing** `Connector` base with metadata only and declares
  `transport = "http"` (§5.2). There is **no** REST base class and **no** hook interface: nothing needs one today
  (YAGNI). If a future network needs behavior YAML cannot express, adding a hook is a separate design change.
- **D5 — No REST module.** `streamwright.core.runtime.rest` (`RestConnector`) is **deleted**, not renamed —
  core's HTTP engine replaces it. One HTTP library: `requests`, inside `net/http.py:HttpClient`; connectors never
  choose or import an HTTP library (swapping the library later, e.g. for HTTP/2, happens once in `HttpClient`).
- **D6 — No guessing.** Records come from `records.path` (or the whole response). Totals come from
  `total_path`. Unknown shapes are errors, not empty results.
- **D7 — Clean break for these four networks.** `openai_ads`, `linkedin_ads`, `apple_ads` and `amazon_ads` have
  not been released; their streams change syntax with no backward compatibility. Plain `http:` sources must keep
  working **unchanged** (existing tests are the proof).
- **D8 — `base_url` keeps its name.** It already exists for `http:` sources (schema, docs, tests); "endpoint"
  would collide with what `path` means (one operation). No alias.

**Out of scope (do not touch):** `google_ads` (gRPC SDK), `meta_ads` (SDK), `microsoft_ads` (SOAP SDK), every
reader connector, the `streamwright-orchestration` repo, the hub and catalog, releasing/publishing, and
`async_job` over `http:` (a follow-up, §10).

## 3. Target architecture

```
source.yaml (user)                                core (one engine)                       connector package (pip)
─────────────────────                             ────────────────────────────────        ─────────────────────────
auth:                                             SourceRunner                             class OpenAIAdsConnector(
  provider: openai_ads   ─── loads ──────────────►  components.load("openai_ads") ──────►      Connector):
  type: bearer                                      connector.transport == "http"           name = "openai_ads"
  token: "{{ secrets.k }}" ─► Authenticator         → HTTP mode: no connect()/request()     transport = "http"
http:                                                                                       category, summary
  base_url, headers,     ─► HttpClient (requests: the one HTTP library)
  retry, rate_limit
  paginator  (default)   ─► net/paginators.py  (registry: none, offset, page_number, cursor, link_header)
  records    (default)   ─► request_select()   (explicit path, explode)
  params_encoding        ─► net/encoding.py    (plain, dotted)
streams/*.yaml
  requests:
    - http: {path, method, params, headers, json}
      paginator / records  (optional overrides)
```

Per request the runner does: render → merge source defaults → encode params → for each page: apply paginator
patch → `HttpClient.request` → `records.path` → paginator `next`.

## 4. YAML reference (what the implementer must support)

### 4.1 `auth:` with an `http`-transport provider

`provider` may now be combined with a built-in `type` **when the provider's `transport` is `"http"`**. The keys
are exactly the existing `AUTH_TYPES` keys (`validation/schema.py`).

```yaml
auth:
  provider: openai_ads            # required for REST networks: the pip-installed connector
  type: bearer                    # bearer | api_key | basic | oauth2_refresh_token
  token: "{{ secrets.openai_ads_key }}"
```

Rules: an SDK connector (`transport = "sdk"`) with `type:` is still an error. An `http`-transport provider
without `type:` is an error (`missing-key`, "openai_ads needs `auth.type` (bearer, api_key, …)").

### 4.2 `http:` block — new keys

Existing: `base_url`, `headers`, `rate_limit`, `retry`. Add:

| Key | Meaning |
|---|---|
| `paginator` | Default paginator for every request item that has none (§4.4). Default: `{type: none}`. |
| `records` | Default `{path, explode}` for every request item that has none (§4.5). |
| `params_encoding` | `plain` (default) or `dotted` (§4.6). A request item may override with `http.params_encoding`. |

Header rules (apply to source `http.headers` and request `http.headers`; request headers override source headers
of the same name, case-insensitively):
- Values are templates. Scopes: `config`, `secrets`, `today`; request headers additionally `partition`, `window`.
  **Allowing `secrets` in headers is new**: every secret value is already registered with the redactor at runner
  start (`runner.py`, `for value in secrets.values()`); keep it that way so header values never reach logs.
- A header whose value renders to an empty string is **omitted** (e.g. an optional account-scope header).
- `secrets` stays forbidden everywhere else in `http:` (paths, params, `json`) — validation error as today.

### 4.3 Request item

Unchanged shape (`HTTP_REQUEST_KEYS = path, method, params, headers, json`) plus `params_encoding`. For a source
whose provider has `transport = "http"`, request items **must** use `http:`; an `sdk:` item is a validation error:
`"openai_ads is a REST API connector: use an http request (http: {path, method, …}), not sdk"`.

### 4.4 Paginators (`net/paginators.py`) — complete specification

Common keys: `type`; `in: query | body` (default `query`) — where the paginator writes its values. With
`in: body`, `*_param` names are **dotted paths into the JSON body** (e.g. `pagination.offset`) and the request
must have a `json` mapping (validation error otherwise). Common stop/safety rules for all types:
- an empty page stops;
- a repeated cursor token / next URL raises `HttpError("the paginator is not advancing…")` (existing behavior);
- a page whose records equal the previous page raises the same error (existing behavior).

| `type` | Required | Optional | Behavior |
|---|---|---|---|
| `none` | — | — | One request. |
| `offset` | `offset_param`, `limit_param`, `page_size` | `start` (0), `total_path`, `in` | Sends offset=`start`, limit=`page_size`. Next offset = previous offset + **number of records received**. Stop: empty page; if `total_path` is set → stop when offset ≥ total (read from that response); otherwise → stop on a short page (< `page_size`). |
| `page_number` | `page_param` | `page_size`, `size_param`, `start` (1), `total_pages_path`, `in` | As today; additionally stop when page > value at `total_pages_path`. |
| `cursor` | (`token_path` + `param`) or `next_url_path` | `has_more_path`, `in` | As today, plus: if `has_more_path` is set, stop when its value is false/absent; if it is true but the token is missing → `HttpError("has_more is true but there is no token at <token_path>")`. Token sent as `param` (query or body). |
| `link_header` | — | — | Follows the `Link: <…>; rel="next"` response header (RFC 8288) until absent. Requires `HttpClient` to expose the last response's headers (`client.last_headers`). |

Implementation shape (replace the `if/elif` in `net/http.py:paginate`):

```python
# net/paginators.py
class PagePatch(NamedTuple):
    params: dict          # merged into the query string
    body: dict            # {dotted.path: value} set into a deep copy of the request json
    url: Optional[str]    # absolute/relative next URL (cursor next_url_path, link_header); replaces path+params

class Paginator:
    def first(self) -> PagePatch: ...
    def next(self, response, records, headers) -> Optional[PagePatch]: ...   # None = stop

PAGINATORS = {"none": NoPaginator, "offset": OffsetPaginator, "page_number": PageNumberPaginator,
              "cursor": CursorPaginator, "link_header": LinkHeaderPaginator}

def paginator_for(spec: dict) -> Paginator: ...
def paginate(send, spec_for, select): ...   # same contract as today's net.http.paginate, driven by the registry
```

`spec_for(response)` keeps today's semantics: the paginator config may itself be templated on `response`
(existing feature — keep it working). Keep `net.http.paginate` importable (re-export) so existing imports and
tests do not change.

### 4.5 Records

`records: {path, explode}` (existing semantics in `runner.request_select`): `path` is a dotted path
(`templates.get_path`); a dict becomes one record; absent path → whole response. **No envelope guessing.**
If `path` is set and missing from a non-empty response → `SourceError("records path %r not found in the
response")` (today it silently returns `[]`; the change applies to all `http:` sources — update any core test
that relied on the silent behavior and note it in the changelog of the commit).

### 4.6 Parameter encoding (`net/encoding.py`)

- `plain`: params passed to `requests` unchanged. A nested mapping in params is a validation error
  ("nested params need `params_encoding: dotted`").
- `dotted`: nested mappings flatten with `.`, lists with `[i]`:
  `{"dateRange": {"start": {"year": 2026}}, "accounts": ["a","b"]}` →
  `{"dateRange.start.year": 2026, "accounts[0]": "a", "accounts[1]": "b"}`
  (exactly today's `linkedin_ads._flatten_params`; move it to core, delete it from the connector).

## 5. Python changes

### 5.1 Remove `runtime/rest.py` (the last core step — §8 step 5)

- Delete `core/src/streamwright/core/runtime/rest.py` and `core/tests/test_rest.py` (their useful scenarios are
  re-covered by the engine tests in §7).
- `runtime/components.py`: remove `RestConnector` from `__all__` and from the lazy `__getattr__`.
- `core/pyproject.toml`: **remove** the `rest = "streamwright.core.runtime.rest:RestConnector"` entry point
  (pure `http:` sources need no provider).
- `grep -rnE "runtime\.rest\b|RestConnector"` → no hits.
- Delete stale `build/` folders under `core/` and `connectors/ads/*/` (they contain old copies and confuse grep).

### 5.2 `Connector.transport` — the only addition to the connector interface

In `runtime/components.py`, class `Connector`:

```python
    transport = "sdk"   # "sdk": the connector's connect()/request() run its requests (sdk: items).
                        # "http": core's HTTP engine runs the source's http: requests; the connector only
                        # registers its name (install, --allow-connector, catalog) and is never connect()ed.
```

That is all: no base class, no hooks, no call object. A placeholder connector (§6) subclasses `Connector`, sets
`transport = "http"` and its metadata. `Connector.check_auth` must skip its `auth_required` check when
`transport == "http"` (core validates the keys from `auth.type`).

### 5.3 Runner (`engine/runner.py`)

1. `__init__`: after `components.load(provider)`:
   - if `connector.transport == "http"`: build `Authenticator(rendered, self.session, self.redact)` from the auth
     block **minus `provider`**. Do **not** set `connector_auth`; never call `connect()`.
   - else: unchanged (SDK connector).
2. `self.http`: render `headers` with `dict(self.scopes(), secrets=secrets)`; everything else with
   `self.scopes()` as today.
3. `http_pages(request, item, client, scopes, where)`:
   - effective paginator = the item's `paginator` **if the key is present** (even `{type: none}`), else
     `self.http.get("paginator")`; effective records = the item's `records` **if the key is present** (an empty
     `records: {}` means "the whole response" and must win over the source default — test this), else
     `self.http.get("records")`. Use `"records" in item`, never `item.get(...) or default`;
     effective encoding = request `params_encoding` else `self.http.get("params_encoding", "plain")`.
   - headers: source headers ⊕ request headers (request wins), drop empty values.
   - per page, apply the `PagePatch` to the rendered request (params merge; dotted body sets on a deep copy of
     `json`; or `url`), send through `client.request(...)`, and select records with `request_select` and the
     effective records.
   - `request_pages()` must pass the effective `records` to `explode` too.
4. `sdk_responses` is unchanged (SDK connectors only); reaching it with an `http`-transport provider is impossible because
   validation rejects it — also raise `SourceError` defensively.

### 5.4 `HttpClient` (`net/http.py`)

- Record `self.last_headers = response.headers` next to `self.last_url` (for `link_header`).
- Move `paginate` to `net/paginators.py`; re-export it from `net/http.py`.
- No other behavior change.

### 5.5 Validation (`validation/schema.py`, `validation/source.py`) and JSON Schemas

- `HTTP_KEYS` += `paginator`, `records`, `params_encoding`; `HTTP_REQUEST_KEYS` += `params_encoding`.
- `PAGINATOR_TYPES`: add the optional keys of §4.4 and the `link_header` type; check `in: body` ⇒ request has a
  mapping `json`; check `*_param` are non-empty strings.
- `params_encoding` ∈ {`plain`, `dotted`}; nested params with `plain` → error.
- `auth`: allow `provider` + `type` (JSON Schema: drop the `not: {required: [type]}`; `source.py` enforces
  "type only with an `http`-transport provider" when the connector is installed; when it is not installed keep today's
  `connector-not-installed` warning and skip the check).
- The check at `source.py` (~line 1057, "authenticates sdk requests only") must allow `http:` requests when the
  provider is a REST API connector, and reject `sdk:` requests for it (§4.3).
- Templates: allow `secrets` scope in `http.headers` (source and request), nowhere else.
- Regenerate `docs/schemas/*.json` with `streamwright-validate --export-schema docs/schemas`
  (`core/tests/test_validation.py` asserts they match).

## 6. Network migrations (target YAML)

Each connector's `connector.py` becomes (shape — keep the license header):

```python
from streamwright.core.runtime.components import Connector

__all__ = ["OpenAIAdsConnector"]


class OpenAIAdsConnector(Connector):
    """OpenAI Ads: an HTTP API declared in the source (auth + http); see examples/sources/ads/openai_ads."""
    name = "openai_ads"
    transport = "http"
    category = "advertising"
    summary = "OpenAI Ads"
    network_loggers = ("urllib3.connectionpool",)
```

`pyproject.toml`: keep the entry point; dependencies = `streamwright` only (drop `requests` etc. if listed);
remove `build_query_body` and every other helper from the package. Keep each connector's README, rewritten to show
the YAML.

### 6.1 OpenAI Ads

```yaml
auth:
  provider: openai_ads
  type: bearer
  token: "{{ secrets.openai_ads_key }}"
http:
  base_url: https://api.ads.openai.com
  paginator: {type: cursor, token_path: last_id, param: after, has_more_path: has_more}
  records: {path: data}
  retry: {codes: [429, 500, 502, 503, 504], max_attempts: 5, backoff: exponential, max_delay: 2m}
```
```yaml
# streams/campaigns.yaml
requests:
  - name: raw_campaigns
    http:
      path: /v1/campaigns
      method: GET
      params: {ad_account_id: "{{ partition.account_id }}", limit: 500}
# streams/campaign_performance.yaml — the insights call (was method: insights + arguments.id)
  - name: raw_campaign_insights
    partitions: [{from: campaign_rows, fields: [account_id, campaign_id]}]
    http:
      path: "/v1/campaigns/{{ partition.campaign_id }}/insights"
      method: GET
      params: {start_date: "{{ window.start }}", end_date: "{{ window.end }}", limit: 500}
```
A single-entity read (was `method: get`, `arguments.id`): `path: "/v1/ad_groups/{{ partition.ad_group_id }}"`,
`paginator: {type: none}`, `records: {}` (the whole response is the record; overrides the source's `data`).

### 6.2 LinkedIn Ads

```yaml
auth:
  provider: linkedin_ads
  type: bearer
  token: "{{ secrets.linkedin_access_token }}"
http:
  base_url: https://api.linkedin.com
  headers:
    LinkedIn-Version: "{{ config.api_version }}"     # spec.config.api_version default "202401"
    X-Restli-Protocol-Version: "2.0.0"
  params_encoding: dotted
  paginator: {type: offset, offset_param: start, limit_param: count, page_size: 100, total_path: paging.total}
  records: {path: elements}
```
`adAnalytics` streams: `path: /rest/adAnalytics`, `method: GET`, nested `params` (`dateRange`, `accounts`) as in
today's `campaign_performance.yaml`. The current `analytics`-only-on-`/rest/adAnalytics` check is dropped (the path
*is* the operation now).

> ⚠️ Verify, do not change: today's dotted/bracket encoding is Rest.li 1.0 style, while the header says protocol
> 2.0.0 (which uses `List(...)` / `(k:v)` syntax). Preserve current behavior exactly (tests encode it) and add a
> `TODO(linkedin-restli2)` line in the connector README. A `restli2` encoder is a follow-up (§10).

### 6.3 Apple Ads

```yaml
spec:
  config:
    ad_account_id: {type: string, description: "Apple Ads ad account ID (sent as X-AP-Context)"}
auth:
  provider: apple_ads
  type: bearer
  token: "{{ secrets.apple_ads_token }}"
http:
  base_url: https://api.ads.apple.com
  headers:
    X-AP-Context: "adAccountId={{ config.ad_account_id }}"
  paginator:
    type: offset
    in: body
    offset_param: pagination.offset
    limit_param: pagination.limit
    page_size: 100
    total_path: pagination.totalCount
  records: {path: result}
```
```yaml
# streams/campaigns.yaml (was method: query → POST {path}/query)
  - name: raw_campaigns
    http:
      path: /v1/campaigns/query
      method: POST
      json: {pagination: {pageSize: 100, fetchTotalCount: true}}
# streams/keywords.yaml
      json:
        pagination: {pageSize: 100, fetchTotalCount: true}
        filters: [{field: campaignId, operator: EQUALS, value: "{{ partition.campaign_id }}"}]
# streams/campaign_reports.yaml and insights: records override
    records: {path: result.rows}
# single entity: GET /v1/campaigns/{{ … }}, paginator: {type: none}, records: {path: data}
```
Behavior parity with today's tests: same URLs, same body (`pagination.limit/offset/pageSize/fetchTotalCount`), same
`X-AP-Context` value. `ad_account_id` becomes required (the header would otherwise render as `adAccountId=`).

### 6.4 Amazon Ads

```yaml
spec:
  config:
    api_url: {type: string, default: "https://advertising-api.amazon.com",
              description: "Regional endpoint: …-api.amazon.com (NA), …-api-eu.amazon.com (EU), …-api-fe.amazon.com (FE)"}
    profile_id: {type: string, required: false}
  secrets:
    amazon_client_id: {type: string}
    amazon_client_secret: {type: string}
    amazon_refresh_token: {type: string}
auth:
  provider: amazon_ads
  type: oauth2_refresh_token
  token_url: https://api.amazon.com/auth/o2/token
  client_id: "{{ secrets.amazon_client_id }}"
  client_secret: "{{ secrets.amazon_client_secret }}"
  refresh_token: "{{ secrets.amazon_refresh_token }}"
http:
  base_url: "{{ config.api_url }}"
  headers:
    Amazon-Advertising-API-ClientId: "{{ secrets.amazon_client_id }}"
    Amazon-Advertising-API-Scope: "{{ config.profile_id }}"     # omitted when empty (§4.2)
  paginator: {type: offset, offset_param: startIndex, limit_param: count, page_size: 100}
```
Streams: `service: sp_campaigns` → `http: {path: /v2/sp/campaigns, method: GET}`; `profiles` → `/v2/profiles`;
`sp_ad_groups` → `/v2/sp/adGroups`; `sp_ads` → `/v2/sp/productAds`; `sp_keywords` → `/v2/sp/keywords`. These v2
endpoints return a bare JSON list, so no `records` default is needed. A v3 endpoint uses a per-request
`paginator: {type: cursor, token_path: nextToken, param: nextToken}` — the "mixed token/offset" loop is
**not** reimplemented; the region map, `SERVICES` map and response-key list are deleted. Document `type: bearer`
with a static `access_token` as the alternative auth in the example's comments.

## 7. Tests

Write tests **before** migrating each network and keep them green after; they define behavior parity.

**Core (new `core/tests/test_http_engine.py`, plus additions to `test_runner.py` / `test_validation.py`):**
- each paginator type × `in: query|body`, with exact request sequences (params/body per page), stop rules
  (empty page, short page, `total_path`, `has_more_path` false, `has_more` true without token → error,
  repeated token → error), `link_header`;
- `params_encoding: dotted` vectors (§4.6) and the `plain` nested-params validation error;
- headers: source+request merge (case-insensitive override), empty value omitted, `secrets` rendered and **never**
  present in log output (assert with caplog at DEBUG on `streamwright.network`);
- records: path, dict → one record, missing path → error;
- `transport: http` provider mode: `auth.provider` + `type` builds the right `Authenticator`; `connect()` is never
  called; an `sdk:` item is rejected (validation and runtime); `streamwright connectors` still lists the connector;
- validation: provider+type allowed for an `http`-transport connector, rejected for an SDK connector, an `http` provider without
  `type` rejected; JSON Schemas regenerated;
- **unchanged**: every existing core test for `http:` sources passes without edits (except the deliberate
  "records path missing" change in §4.5, if any test depended on it).

**Per network (`connectors/ads/<net>/tests/`):** rewrite to run a real `SourceRunner` with `responses`, using
the migrated YAML, and assert the **exact** HTTP calls (method, URL, query, JSON body, the auth and custom headers)
and output records. Port every scenario in today's tests: OpenAI cursor + `has_more`; LinkedIn offset + total +
dotted params + 429 retry; Apple body offset + `result`/`result.rows`/`data` + `X-AP-Context`; Amazon token refresh
(one POST to the token URL, then bearer), ClientId/Scope headers (Scope absent when `profile_id` empty), regional
`api_url`, offset paging, empty-list stop. Delete the old `test_missing_auth`-style tests that called
`connector.connect()` directly; replace with runner-level auth errors.

## 8. Execution order (each step ends green)

| # | Step | Gate |
|---|---|---|
| 0 | Baseline: fresh venv, `pip install -e core -e connectors/ads/* -e connectors/readers/* pytest responses jsonschema`; record pass counts. Note the working tree already has uncommitted changes — keep them. | `pytest core/tests connectors -q` |
| 1 | Add `Connector.transport` (§5.2) with the default `"sdk"`; nothing uses it yet. | same counts |
| 2 | `net/paginators.py` registry + `net/encoding.py`; `paginate` re-exported; new core tests. | core green, `http:` tests unchanged |
| 3 | Runner + `HttpClient` changes (§5.3–5.4): defaults, headers rule, encoding, `http`-transport mode. | core green |
| 4 | Validation + JSON Schemas (§5.5). | core green |
| 5 | Delete `runtime/rest.py`, `test_rest.py` and the `rest` entry point (§5.1). | core green |
| 6 | OpenAI: tests → YAML (source + 9 streams) → placeholder class. | its tests + `streamwright validate --strict examples/sources/ads/openai_ads` |
| 7 | LinkedIn (5 streams). | same |
| 8 | Apple (4 streams). | same |
| 9 | Amazon (5 streams). | same |
| 10 | Docs (§9), final sweep (§11). | all of §11 |

Commit per step with a clear message (`git -c user.email=kjaganathan.sde@gmail.com -c user.name="Karthick
Jaganathan" commit …`, no AI co-author trailers). Stage files by explicit path; never stage `.claude/`,
`.scout/`, `AGENTS.md`, `CLAUDE.md`. **Do not push, publish, or bump versions** — the maintainer releases.

## 9. Docs

- `docs/design/source-format.md`: document `http.paginator` / `records` / `params_encoding`, the paginator table
  (§4.4), the header rules (§4.2) and `auth.provider` + `type`.
- New `docs/core/rest-api-connectors.md` (or a section in `docs/core/index.md`): "a REST connector is YAML": the
  §6.1 example end to end, and the placeholder class (`transport = "http"`).
- Each of the four connector READMEs: the source YAML for that network.
- `connectors/README.md`: list the four as REST API connectors (YAML-defined).

## 10. Follow-ups (not in this plan)

- `async_job` with `http:` submit/poll/download (Amazon v3 reports, Apple/LinkedIn async reports).
- `restli2` parameter encoder for LinkedIn (`List(…)`, `(k:v)`), verified against LinkedIn's docs.
- `oauth2_client_credentials` (and JWT client assertion) auth type — Apple's production auth (a core auth type,
  available to every `http:` source — not connector code).
- Connector hooks: only if a future network needs behavior YAML cannot express; design them then.
- Hub entries for the four connectors after release.

## 11. Acceptance criteria (all must hold)

```bash
python -m pytest core/tests connectors -q                       # all pass; count ≥ baseline + new tests
streamwright validate --strict examples                         # 0 errors, 0 warnings
grep -rnE "runtime\.rest\b|RestConnector|RestApiConnector" --include=*.py --include=*.toml .   # no hits
grep -rniE "openai|linkedin|apple|amazon" core/src              # no hits (no network names in core)
grep -rnE "^\s+(class|def) " connectors/ads/{openai,linkedin,apple,amazon}_ads/src   # only the placeholder class
grep -rnE "import requests|while True" connectors/ads/{openai,linkedin,apple,amazon}_ads/src   # no hits
```

- Every former test scenario of the four networks has a runner-level test with exact request assertions.
- `google_ads`, `meta_ads`, `microsoft_ads` and all readers: no diff.
- No secret value appears in any log line at DEBUG (tested).
- Plain `http:` sources: no behavior change (existing tests untouched and green).
