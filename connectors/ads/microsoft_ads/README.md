# ADaPT Microsoft Ads connector

The `microsoft_ads` connector for [adapt-core](../../../adapt-core/README.md), on the
[bingads](https://pypi.org/project/bingads/) SDK (API v13): the `microsoft_ads` auth provider signs in with OAuth,
and `sdk: microsoft_ads` requests call SOAP operations, including reports as async jobs. Example:
the source folder [examples/sources/ads/microsoft_ads/](../../../examples/sources/ads/microsoft_ads/).

## Install

```bash
make install-microsoft-ads   # from the repository root; or: pip install ./connectors/ads/microsoft_ads (installs bingads)
adapt connectors             # lists microsoft_ads (with its SDK loggers)
```

## Auth

| Key | Meaning |
|---|---|
| `developer_token`, `client_id`, `refresh_token` | required |
| `client_secret` | for web apps; without it the desktop and mobile app flow is used |
| `tenant` | the Microsoft Entra tenant (default `common`) |
| `customer_id` | the `CustomerId` header, needed by most operations |
| `account_id` | the default `CustomerAccountId` header |
| `environment` | `production` (default) or `sandbox` |

The SDK refreshes the access token during a run. Refresh tokens it receives are redacted from logs but not saved.

## Requests

- Services: `CampaignManagementService`, `ReportingService`, `CustomerManagementService`, `AdInsightService`.
  Methods: read-only operations (`Get*`, `Search*`, `Find*`, `Poll*`) and `SubmitGenerateReport`.
- `arguments` are the operation's fields, built into SOAP objects from the service's WSDL: nested objects are
  mappings, abstract types name their concrete type with `"@type"` (e.g. `CampaignPerformanceReportRequest`), lists
  fill `ArrayOf*` types (and are joined with spaces for list values such as `CampaignType: [Search, Shopping]`), and
  dates fill `Date` objects. Unknown fields are errors that list the valid ones.
- Responses become plain dicts. A response with one part is that part: `GetCampaignsByAccountId` gives
  `{"Campaign": [...]}`, so use `records: {path: Campaign}`. One with several parts maps their names to them:
  `GetCampaignCriterionsByIds` gives `{"CampaignCriterions": {"CampaignCriterion": [...]}, "PartialErrors": ...}`
  (`records: {path: CampaignCriterions.CampaignCriterion}`). The operation's page on Microsoft Learn lists the parts.
- `headers: {CustomerAccountId, CustomerId}` set those SOAP headers for one request. The `CustomerAccountId` header
  is `headers.CustomerAccountId`, else the request's `AccountId`, else the only account of a report's
  `Scope.AccountIds`, else `auth.account_id`; operations about a campaign or ad group name only its ID, so set the
  account in `headers` (below). `headers.CustomerId` replaces `auth.customer_id`.

```yaml
# streams/ad_groups.yaml: the ad groups of each campaign of the campaigns stream
partitions:
  - {from_stream: campaigns, fields: [account_id, campaign_id]}
requests:
  - name: raw_ad_groups
    sdk: microsoft_ads
    service: CampaignManagementService
    method: GetAdGroupsByCampaignId
    headers: {CustomerAccountId: "{{ partition.account_id }}"}
    arguments: {CampaignId: "{{ partition.campaign_id }}", ReturnAdditionalFields: [AdGroupType]}
    records: {path: AdGroup}
transform:
  mode: page
  steps:
    - name: ad_groups
      select: |
        SELECT partition->>'account_id'  AS account_id,
               partition->>'campaign_id' AS campaign_id,
               record->>'Id'             AS ad_group_id,
               record->>'Name'           AS ad_group_name,
               record->>'AdGroupType'    AS ad_group_type
        FROM raw_ad_groups
export:
  ad_groups: {step: ad_groups, primary_key: [account_id, ad_group_id]}
```

The example folder reads campaigns, ad groups, keywords, location targets (campaign criteria) and audience targets
(ad group criteria; name each audience type: the composite `Audience` is not valid for ad groups) this way: one call
per campaign or ad group, which the Bulk API would do in one file for large accounts. Its `ad_group_tree` stream
reads each account's campaigns and then their ad groups in one stream (`transform.mode: run`, the second request
partitioned `from:` a step), and joins them.

## Reports

Reports are async jobs: `SubmitGenerateReport`, then `PollGenerateReport` until `Status` is `Success`, then the
zipped CSV at `ReportDownloadUrl` (see the example). Set `ExcludeReportHeader` and `ExcludeReportFooter` to `true`
so the file is a plain table. A report without data has no download URL and gives no records.

## Errors

Retried: `0` InternalError, `117` CallRateExceeded and `207` ConcurrentRequestOverLimit (after 60 seconds), HTTP 429
and 5xx without a SOAP fault, and connection errors. Other faults fail the window with their codes, messages and tracking ID.

## Logs

The SDK sends its SOAP messages with suds, which logs them at `DEBUG` on `suds.client` (the messages sent and
received) and `suds.transport` (their HTTP requests and replies); `adapt connectors` lists both. adapt never turns them
on; name them with `--log`:

```bash
adapt run examples/sources/ads/microsoft_ads --set account_ids=123456 --set customer_id=555 \
  --log adapt.network=INFO --log suds.client=DEBUG --log suds.transport=DEBUG
```

Their lines are redacted like adapt's own: the developer token, the OAuth tokens (refreshed ones too) and the
signatures of report URLs are `***`. `--log adapt.network=INFO` alone gives adapt's line per call, after its stream,
request, partition and window, e.g. `microsoft_ads ReportingService.PollGenerateReport: 0.41 s, attempt 1`, and one
per report download.
