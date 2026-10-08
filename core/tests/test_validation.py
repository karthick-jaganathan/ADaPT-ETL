import copy
import functools
import json
import os
import re
from textwrap import dedent

import pytest
import yaml

from streamwright.core.validation import schema
from streamwright.core.validation.engine import ERROR, WARNING, main, validate_file, validate_paths, validate_source, \
    validate_text
from streamwright.core.config.loader import SourceFilesError, find_source, load_source

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCES = os.path.join(REPO_ROOT, "examples", "sources")

BASE = {
    "kind": "source",
    "name": "demo",
    "spec": {
        "config": {"account_ids": {"type": "list", "items": "string"}},
        "secrets": {"token": {"type": "string"}},
    },
    "auth": {"type": "bearer", "token": "{{ secrets.token }}"},
    "http": {"base_url": "https://api.example.com"},
    "streams": [{
        "name": "campaigns",
        "partitions": [{"name": "account_id", "values": "{{ config.account_ids }}"}],
        "requests": [{
            "name": "raw_campaigns",
            "http": {"path": "/accounts/{{ partition.account_id }}/campaigns"},
            "paginator": {"type": "offset", "offset_param": "start", "limit_param": "count", "page_size": 100},
            "records": {"path": "elements"},
        }],
        "transform": {"mode": "page", "steps": [
            {"name": "campaigns",
             "select": "SELECT record->>'id' AS id, partition->>'account_id' AS account_id FROM raw_campaigns"},
        ]},
        "export": {"campaigns": {"step": "campaigns", "primary_key": ["id"]}},
    }],
}


def check(mutate=None, **options):
    doc = copy.deepcopy(BASE)
    if mutate:
        mutate(doc)
    return validate_text(yaml.safe_dump(doc, sort_keys=False), "source.yaml", **options)


def stream(doc, index=0):
    return doc["streams"][index]


def request(doc, index=0, item=0):
    """An item of a stream's `requests`."""
    return stream(doc, index)["requests"][item]


def steps(doc, index=0):
    return stream(doc, index)["transform"]["steps"]


def new_stream(name, partitions=None, path="/items", **keys):
    """A stream in the one form, named as the examples are: request raw_<name>, step and export <name>."""
    item = {"name": name}
    if partitions is not None:
        item["partitions"] = partitions
    item.update(requests=[{"name": "raw_" + name, "http": {"path": path}}],
                transform={"mode": "page", "steps": [
                    {"name": name, "select": "SELECT record->>'id' AS id FROM raw_%s" % name}]},
                export={name: {"step": name}})
    item.update(keys)
    return item


def requests_stream(doc, partitions):
    """The base stream in run mode with a second request, `raw_ads`, that has these request partitions."""
    stream(doc)["transform"]["mode"] = "run"
    stream(doc)["requests"].append({"name": "raw_ads", "partitions": partitions,
                                    "http": {"path": "/ads/{{ partition.account_id }}/{{ partition.campaign_id }}"}})


def sdk_source(doc):
    doc["auth"] = {"provider": "google_ads", "refresh_token": "{{ secrets.token }}"}
    del doc["http"]
    stream(doc)["requests"] = [{"name": "raw_campaigns", "sdk": "google_ads", "service": "GoogleAdsService",
                                "method": "search_stream",
                                "arguments": {"customer_id": "{{ partition.account_id }}",
                                              "query": {"gaql": {"select": ["campaign.id"], "from": "campaign"}}}}]


def codes(issues):
    return sorted((i.severity, i.code) for i in issues)


def messages(issues):
    return " | ".join(i.message for i in issues)


def found(issues):
    return [(i.code, i.path, i.message) for i in issues]


# * --------------------------------
# * examples, schema and doc in sync
# * --------------------------------

def example_sources():
    """The example source folders, grouped under examples/sources/<group>/ (ads, readers)."""
    return sorted(os.path.join(SOURCES, group, name)
                  for group in os.listdir(SOURCES) if os.path.isdir(os.path.join(SOURCES, group))
                  for name in os.listdir(os.path.join(SOURCES, group))
                  if os.path.isdir(os.path.join(SOURCES, group, name)))


def example_files():
    """{path relative to the source's group folder (<source>/...): data} for every file of the example sources."""
    files = {}
    for folder in example_sources():
        for path in find_source(folder).files:
            with open(path) as stream_:
                files[os.path.relpath(path, os.path.dirname(folder))] = yaml.safe_load(stream_)
    return files


def test_base_document_is_valid():
    assert check() == []


@pytest.mark.parametrize("path", example_sources(), ids=os.path.basename)
def test_examples_are_valid(path):
    layout = find_source(path)
    assert layout.folder == path and layout.stream_files
    assert validate_source(path) == []


@pytest.mark.parametrize("path", example_sources(), ids=os.path.basename)
def test_examples_follow_the_naming_convention(path):
    """Requests are raw_<entity>; a stream's export and its last step have the stream's name."""
    for item in load_source(path)["streams"]:
        name = item["name"]
        assert all(request_["name"].startswith("raw_") for request_ in item["requests"]), name
        assert item["transform"]["steps"][-1]["name"] == name, name
        assert list(item["export"]) == [name] and item["export"][name]["step"] == name, name
        if item["transform"]["mode"] == "page":
            assert [request_["name"] for request_ in item["requests"]] == ["raw_" + name], name


def test_examples_match_the_design_doc():
    """
    The files the design doc shows (in blocks that start with their path) match the examples, every source.yaml is
    shown, and its Metadata streams table lists every stream of every example.
    """
    with open(os.path.join(REPO_ROOT, "docs", "design", "source-format.md")) as stream_:
        text = stream_.read()
    blocks = re.findall(r"```yaml\n# ([\w/]+\.yaml)\n(.*?)```", text, re.S)
    documented = dict((path, yaml.safe_load(body)) for path, body in blocks)
    files = example_files()
    assert len(documented) == len(blocks)
    assert dict((path, files.get(path)) for path in documented) == documented
    assert sorted(path for path in documented if path.endswith("/source.yaml")) == sorted(
        path for path in files if path.endswith("/source.yaml"))
    section = text[text.index("### Metadata streams"):]
    section = section[:section.index("\n## ")]
    table = dict((source, set(re.findall(r"`(\w+)`", streams)))
                 for source, streams in re.findall(r"^\| `(\w+)` \| (.*) \|$", section, re.M))
    streams = {}
    for path in files:
        source, _, name = path.partition("/streams/")
        if name:
            streams.setdefault(source, set()).add(name[:-len(".yaml")])
    assert table == streams


@pytest.mark.parametrize("name,schema", [("source", schema.json_schema),
                                         ("stream", schema.stream_json_schema)])
def test_published_schemas_are_up_to_date(name, schema):
    with open(os.path.join(REPO_ROOT, "docs", "schemas", "%s.schema.json" % name)) as stream_:
        assert json.load(stream_) == schema(), "regenerate with: streamwright-validate --export-schema docs/schemas"


@pytest.mark.parametrize("path", example_sources(), ids=os.path.basename)
def test_schemas_accept_examples(path):
    jsonschema = pytest.importorskip("jsonschema")
    source_schema, stream_schema = schema.json_schema(), schema.stream_json_schema()
    for document in (source_schema, stream_schema):
        jsonschema.Draft7Validator.check_schema(document)
    layout = find_source(path)
    with open(layout.source_file) as stream_:
        jsonschema.validate(yaml.safe_load(stream_), source_schema, cls=jsonschema.Draft7Validator)
    for document, files in ((stream_schema, layout.stream_files),):
        for _, file_path in files:
            with open(file_path) as stream_:
                jsonschema.validate(yaml.safe_load(stream_), document, cls=jsonschema.Draft7Validator)
    jsonschema.validate(load_source(path), source_schema, cls=jsonschema.Draft7Validator)


def test_schemas_accept_the_one_stream_form():
    jsonschema = pytest.importorskip("jsonschema")
    validate = functools.partial(jsonschema.validate, cls=jsonschema.Draft7Validator)

    def mutate(d):
        requests_stream(d, [{"from": "campaigns", "fields": ["account_id", "campaign_id"]},
                            {"name": "kind", "values": ["search", "display"]},
                            {"name": "ad_id", "from": "raw_campaigns", "field": "ad.id"}])
        stream(d)["description"] = "Campaigns and their ads."
        stream(d)["on_partition_error"] = "skip"
        steps(d)[0]["description"] = "One row per campaign."
        steps(d).append({"name": "ads", "select": "SELECT record->>'id' AS ad_id FROM raw_ads"})
        stream(d)["export"].update(ads={"step": "ads", "primary_key": ["ad_id"], "description": "The ads."})
    assert check(mutate) == []
    doc = copy.deepcopy(BASE)
    mutate(doc)
    validate(doc, schema.json_schema())
    stream_file = dict((key, value) for key, value in stream(doc).items() if key != "name")
    validate(stream_file, schema.stream_json_schema())


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: stream(d).update(fields=[{"name": "x", "from": "a"}]),          # removed keys
    lambda d: stream(d).update(request={"http": {"path": "/a"}}),
    lambda d: stream(d).update(select="SELECT record FROM raw_campaigns"),
    lambda d: stream(d).update(raw=True),
    lambda d: stream(d).update(primary_key=["id"]),
    lambda d: stream(d).update(transform_mode="page"),
    lambda d: stream(d).update(paginator={"type": "none"}),
    lambda d: stream(d).update(records={"path": "data"}),
    lambda d: stream(d).pop("requests"),                                       # required keys
    lambda d: stream(d).pop("transform"),
    lambda d: stream(d).pop("export"),
    lambda d: stream(d).update(export={}),
    lambda d: stream(d).update(requests=[]),
    lambda d: request(d).pop("name"),
    lambda d: stream(d)["transform"].pop("mode"),
    lambda d: stream(d)["transform"].update(mode="pages"),
    lambda d: stream(d)["transform"].update(steps=[]),
    lambda d: stream(d).update(transform=steps(d)),                            # the previous form: a list
    lambda d: steps(d)[0].pop("select"),
    lambda d: steps(d)[0].update(select=""),
    lambda d: stream(d)["export"]["campaigns"].update(step=1),
    lambda d: stream(d)["export"]["campaigns"].update(primary_key=[]),
    lambda d: d["http"].update(retry={"on": [429]}),
    lambda d: request(d).update(paginator={"type": "cursor"}),
    lambda d: request(d).update(sdk="x", method="m"),                         # two request kinds
    lambda d: stream(d).update(requests=[{"name": "r", "http": {"path": "/x"}, "headers": {"X": "y"}}]),
    lambda d: stream(d).update(requests=[{"name": "r", "sdk": "x", "method": "m", "headers": {"A": {"b": 1}}}]),
    lambda d: stream(d).update(partitions=[{"from_stream": "x", "fields": []}]),
    lambda d: stream(d).update(partitions=[{"from_stream": "x", "fields": ["a"], "name": "a"}]),
    lambda d: request(d).update(partitions=[{"from": "x", "fields": ["a"], "field": "a"}]),
])
def test_schema_rejects_invalid_documents(mutate):
    jsonschema = pytest.importorskip("jsonschema")
    doc = copy.deepcopy(BASE)
    mutate(doc)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, schema.json_schema(), cls=jsonschema.Draft7Validator)


# * ------------------
# * document and inputs
# * ------------------

def test_routing_and_version():
    # `kind: source` selects these checks; `version` is optional and, when given, must be 1
    for version in (1, "1", "1.0"):
        assert check(lambda d: d.update(version=version)) == []
    issues = check(lambda d: d.update(version=2))
    assert codes(issues) == [(ERROR, "unsupported-version")] and "omit `version`" in issues[0].message
    doc = copy.deepcopy(BASE)
    del doc["kind"]
    text = yaml.safe_dump(doc, sort_keys=False)
    assert validate_text(text, "s.yaml", kind="source") == []
    assert (ERROR, "missing-key") in codes(validate_text(text, "s.yaml"))  # without --kind: `kind` is missing


def test_top_level_keys():
    issues = check(lambda d: d.update(stream=[], **{"x-defaults": {"a": 1}}))
    assert codes(issues) == [(ERROR, "unknown-key")]
    assert "did you mean 'streams'" in messages(issues)
    assert codes(check(lambda d: d.pop("name"))) == [(ERROR, "missing-key")]
    for streams in (5, []):
        issues = [issue for issue in check(lambda d: d.update(streams=streams)) if issue.severity == ERROR]
        assert found(issues) == [("bad-value", "streams", "expected a non-empty list of streams, got %s" % (
            "a number" if streams == 5 else "a list"))]


def test_inputs():
    def mutate(d):
        d["spec"]["config"].update({
            "a": {"type": "strng"},
            "b": {"type": "list"},
            "c": {"type": "date", "default": "yesterday"},
            "d e": {"type": "string"},
        })
        d["spec"]["secrets"]["token"]["default"] = "abc"
    issues = check(mutate)
    assert codes(issues) == [(ERROR, "bad-value")] * 3 + [(ERROR, "missing-key")] + [(WARNING, "secret-default")] \
        + [(WARNING, "unused-input")] * 4
    assert "did you mean 'string'" in messages(issues)


def test_unused_input_is_reported():
    issues = check(lambda d: d["spec"]["config"].update(start_date={"type": "date", "default": "-30d"}))
    assert codes(issues) == [(WARNING, "unused-input")]
    assert issues[0].path == "spec.config.start_date"


@pytest.mark.parametrize("query", [
    "SELECT $client AS client, record->>'id' AS id FROM raw_campaigns",          # a bound parameter
    "SELECT config->>'client' AS client, record->>'id' AS id FROM raw_campaigns",  # the requests' config column
])
def test_config_inputs_read_in_sql_are_used(query):
    def mutate(d):
        d["spec"]["config"]["client"] = {"type": "string"}
        steps(d)[0]["select"] = query
    assert check(mutate) == []
    assert codes(check(lambda d: d["spec"]["config"].update(client={"type": "string"}))) == \
        [(WARNING, "unused-input")]


# * ----------
# * references
# * ----------

@pytest.mark.parametrize("template,code,text", [
    ("{{ config.acount_ids }}", "unknown-reference", "did you mean 'account_ids'"),
    ("{{ secrets.token }}", "secret-outside-auth", "secrets can only be used inside `auth`"),
    ("{{ window.start }}", "unavailable-reference", "needs `incremental`"),
    ("{{ response.next }}", "unavailable-reference", "cannot be used here"),
    ("{{ setting.x }}", "unknown-reference", "unknown scope"),
    ("{{ config.account_ids | joined(',') }}", "unknown-filter", "did you mean 'join'"),
    ("{{ config.account_ids | join }}", "bad-value", "takes 1 argument"),
    ("{{ today.date }}", "template-syntax", "no attributes"),
    ("{{ config.account_ids", "template-syntax", "unclosed"),
    ("{{ 1 + 2 }}", "template-syntax", "invalid reference"),
])
def test_reference_checks(template, code, text):
    issues = check(lambda d: request(d)["http"].update(params={"q": template}))
    assert [i.code for i in issues] == [code], issues
    assert text in issues[0].message
    assert issues[0].path == "streams[0].requests[0].http.params.q"


def test_filters_with_quoted_commas_and_partition_scope():
    assert check(lambda d: request(d)["http"].update(params={
        "ids": "{{ config.account_ids | join(', ') }}", "day": "{{ today | date('%Y-%m-%d') }}"})) == []
    issues = [i for i in check(lambda d: stream(d).pop("partitions")) if i.code != "unused-input"]
    assert {i.message.split(":")[0] for i in issues} == {"{{ partition.account_id }}"}
    assert "has no partitions" in messages(issues)


def test_partition_references_see_the_stream_and_own_request_partitions():
    def mutate(d):
        requests_stream(d, [{"name": "campaign_id", "from": "campaigns", "field": "id"}])
        request(d)["http"]["params"] = {"campaign": "{{ partition.campaign_id }}"}  # a partition of raw_ads only
    issues = check(mutate)
    assert found(issues) == [("unknown-reference", "streams[0].requests[0].http.params.campaign",
                              "{{ partition.campaign_id }}: 'campaign_id' is not declared in partition")]
    assert check(lambda d: requests_stream(d, [{"name": "campaign_id", "from": "campaigns", "field": "id"}])) == []


def test_templates_are_not_allowed_in_names_and_records():
    issues = check(lambda d: request(d)["records"].update(path="{{ config.account_ids }}"))
    assert codes(issues) == [(ERROR, "unavailable-reference")]
    assert "cannot be used here" in issues[0].message


# * ----
# * auth
# * ----

def test_auth_checks():
    assert codes(check(lambda d: d["auth"].update(provider="x"))) == [(ERROR, "bad-value")]
    assert codes(check(lambda d: d["auth"].update(type="bearr"))) == [(ERROR, "bad-value")]
    issues = check(lambda d: d.update(auth={"type": "oauth2_refresh_token", "token_url": "https://x/token",
                                            "client_id": "id", "client_secret": "s3cret",
                                            "refresh_token": "{{ secrets.token }}"}))
    assert codes(issues) == [(WARNING, "literal-secret")]
    assert issues[0].path == "auth.client_secret"
    assert codes(check(lambda d: d.update(auth={"type": "api_key", "value": "{{ secrets.token }}"}))) == \
        [(ERROR, "missing-key")]


def test_connectors_and_sdk_requests():
    assert check(sdk_source) == []
    assert codes(check(sdk_source, allowed_connectors=["facebook"])) == [(ERROR, "connector-not-allowed")] * 2

    def mismatch(d):
        sdk_source(d)
        request(d)["sdk"] = "microsoft_ads"
    assert "does not match auth.provider" in messages(check(mismatch))

    def no_provider(d):
        sdk_source(d)
        d["auth"] = {"type": "bearer", "token": "{{ secrets.token }}"}
    assert "connector `auth.provider`" in messages(check(no_provider))

    def paginated(d):
        sdk_source(d)
        request(d)["paginator"] = {"type": "page_number", "page_param": "page"}
    issues = check(paginated)
    assert "paginated by the connector" in messages(issues)
    assert issues[0].path == "streams[0].requests[0].paginator.type"


# * --------------------
# * http, limits, requests
# * --------------------

def test_http_checks():
    issues = check(lambda d: d.pop("http"))
    assert codes(issues) == [(ERROR, "missing-key")] and "needs `http.base_url`" in messages(issues)
    assert codes(check(lambda d: d["http"].update(base_url="http://api.example.com"))) == [(WARNING, "insecure-url")]
    assert codes(check(lambda d: d["http"].update(headers={"Version": 2}))) == [(ERROR, "bad-value")]
    assert codes(check(lambda d: d["http"].update(rate_limit={"requests": 0, "per": "1 hour"}))) == \
        [(ERROR, "bad-value")] * 2
    assert codes(check(lambda d: d["http"].update(retry={"codes": [429], "max_attempts": 3, "backoff": "linear"}))) \
        == [(ERROR, "bad-value")]
    assert codes(check(lambda d: request(d)["http"].update(method="get"))) == [(ERROR, "bad-value")]


def test_retry_on_key_is_caught():
    text = yaml.safe_dump(BASE, sort_keys=False).replace("base_url:", "retry: {on: [429]}\n  base_url:")
    assert codes(validate_text(text, "s.yaml")) == [(ERROR, "unknown-key"), (WARNING, "yaml-boolean")]


def test_request_shape():
    issues = check(lambda d: request(d).update(sdk="x", method="m"))
    assert found(issues) == [("bad-value", "streams[0].requests[0]",
                              "a request needs exactly one of: http, sdk, async_job (found: http, sdk)")]
    issues = check(lambda d: request(d).pop("http"))
    assert "a request needs exactly one of: http, sdk, async_job (found: none)" in messages(issues)
    issues = check(lambda d: request(d).pop("name"))
    assert found(issues) == [("missing-key", "streams[0].requests[0]", "a request needs `name`")]


def test_request_items_reject_wrong_kind_keys():
    def mutate(d):
        stream(d)["requests"] = [{"name": "raw_campaigns", "http": {"path": "/a"},
                                  "headers": {"X-Api-Key": "{{ secrets.token }}"}, "method": "POST"}]
    issues = check(mutate)
    assert codes(issues) == [(ERROR, "unknown-key"), (ERROR, "unknown-key")]
    assert "unknown key 'headers'" in messages(issues)
    assert "unknown key 'method'" in messages(issues)


def test_async_job_checks():
    def mutate(d):
        sdk_source(d)
        stream(d)["requests"] = [{"name": "raw_campaigns", "async_job": {
            "submit": {"sdk": "google_ads", "method": "Submit", "arguments": {"id": "{{ submit.result }}"}},
            "poll": {"method": "Poll", "arguments": {"id": "{{ submit.result }}"}, "every": "15 seconds",
                     "timeout": "30m", "done_when": {"path": "Status"}},
        }}]
    issues = check(mutate)
    assert codes(issues) == [(ERROR, "bad-value")] * 3 + [(ERROR, "unavailable-reference")]
    text = messages(issues)
    assert "exactly one of `download` or `results`" in text and "exactly one of `equals` or `in`" in text

    def without_sdk(d):
        mutate(d)
        request(d)["async_job"]["submit"].pop("sdk")
        request(d)["async_job"]["results"] = {"method": "Results"}
    issues = [issue for issue in check(without_sdk) if issue.code == "missing-key"]
    assert found(issues) == [
        ("missing-key", "streams[0].requests[0].async_job.submit", "`submit` needs `sdk`: async jobs run through a "
                                                                   "connector"),
        ("missing-key", "streams[0].requests[0].async_job.results", "`results` needs `sdk`: async jobs run through a "
                                                                    "connector"),
    ]


def test_paginator_checks():
    assert codes(check(lambda d: request(d).update(paginator={"type": "cursor", "param": "after"}))) == \
        [(ERROR, "bad-value")]
    assert check(lambda d: request(d).update(paginator={"type": "cursor", "next_url_path": "paging.next"})) == []
    assert codes(check(lambda d: request(d).update(paginator={"type": "offset"}))) == [(ERROR, "missing-key")] * 3
    assert check(lambda d: request(d).update(paginator={
        "type": "cursor", "token_path": "meta.next", "param": "{{ response.meta.next }}"})) == []


def test_query_builder_calls_are_data_with_checked_references():
    """Query builders are components (checked by `streamwright validate`); streamwright-validate checks their references."""
    def mutate(d):
        sdk_source(d)
        request(d)["arguments"]["query"]["gaql"].update(where=[
            {"field": "campaign.id", "op": "IN", "type": "integer", "value": "{{ config.account_id }}"},
            {"field": "segments.date", "op": "BETWEN", "type": "date", "value": ["{{ secrets.token }}"]},
        ])
    issues = check(mutate)
    assert codes(issues) == [(ERROR, "secret-outside-auth"), (ERROR, "unknown-reference")]
    assert "did you mean 'account_ids'" in messages(issues)


def test_sdk_request_headers():
    def headers(value):
        def mutate(d):
            sdk_source(d)
            request(d)["headers"] = value
        return mutate
    assert check(headers({"CustomerAccountId": "{{ partition.account_id }}", "CustomerId": 555})) == []
    assert codes(check(headers("x"))) == [(ERROR, "bad-value")]
    issues = check(headers({"CustomerAccountId": {"id": 1}, "CustomerId": True}))
    assert codes(issues) == [(ERROR, "bad-value")] * 2 and "expected text or a number" in messages(issues)
    issues = check(headers({"CustomerAccountId": "{{ secrets.token }}"}))
    assert codes(issues) == [(ERROR, "secret-outside-auth")]


# * -------------------------------------
# * streams: the one form and removed keys
# * -------------------------------------

@pytest.mark.parametrize("key,message", [
    ("requests", "a stream needs `requests`: streams read only their own requests"),  # no transform-only streams
    ("transform", "a stream needs `transform`: {mode: page or run, steps: [{name, select}, ...]}"),
    ("export", "a stream needs `export`: at least one export (export name -> {step, primary_key?})"),
])
def test_a_stream_needs_requests_transform_and_export(key, message):
    assert found(check(lambda d: stream(d).pop(key))) == [("missing-key", "streams[0]", message)]


@pytest.mark.parametrize("export", [{}, None])
def test_a_stream_needs_at_least_one_export(export):
    assert found(check(lambda d: stream(d).update(export=export))) == [
        ("bad-value", "streams[0].export", "a stream needs at least one export")]


@pytest.mark.parametrize("key,value,hint", [
    ("request", {"http": {"path": "/a"}}, "`request` was removed: use `requests` with one named item"),
    ("select", "SELECT record FROM records", "a stream-level `select` was removed: use `transform` steps; for "
                                             "records as returned: `SELECT record FROM <request>`"),
    ("raw", True, "`raw` was removed: use `transform` steps; for records as returned: `SELECT record FROM "
                  "<request>`"),
    ("primary_key", ["id"], "a stream-level `primary_key` was removed: put `primary_key` on the export"),
    ("transform_mode", "page", "`transform_mode` was removed: use `transform.mode`"),
    ("paginator", {"type": "none"}, "a stream-level `paginator` was removed: put `paginator` on the request item"),
    ("records", {"path": "data"}, "a stream-level `records` was removed: put `records` on the request item"),
    ("fields", [{"name": "id", "from": "id"}], "`fields` was replaced by SQL: `transform` steps"),
    ("on_record_error", "skip", "use TRY_CAST in a step's `select`"),
])
def test_removed_stream_keys_have_hints(key, value, hint):
    issues = check(lambda d: stream(d).update({key: value}))
    assert [(i.code, i.path) for i in issues] == [("removed-key", "streams[0].%s" % key)]
    assert hint in issues[0].message


def test_streams_of_the_previous_forms_get_a_hint_for_each_removed_key():
    shorthand = {"name": "ads", "primary_key": ["id"], "request": {"http": {"path": "/ads"}},
                 "paginator": {"type": "none"}, "records": {"path": "data"},
                 "select": "SELECT record->>'id' AS id FROM records"}
    issues = check(lambda d: d["streams"].append(shorthand))
    # `request` stands for `requests` and `select` for `transform`; nothing stood for `export`
    assert [(i.code, i.path) for i in issues] == [("missing-key", "streams[1]")] + [
        ("removed-key", "streams[1].%s" % key) for key in ("primary_key", "request", "paginator", "records", "select")]
    assert "a stream needs `export`" in issues[0].message

    transform_stream = {"name": "report", "transform_mode": "run",
                        "requests": [{"name": "raw_report", "http": {"path": "/report"}}],
                        "transform": [{"name": "rows", "select": "SELECT record FROM raw_report"}],
                        "export": {"report": {"step": "rows"}}}
    issues = check(lambda d: d["streams"].append(transform_stream))
    assert found(issues) == [  # and the export's step is not reported as unknown on top
        ("removed-key", "streams[1].transform_mode", "`transform_mode` was removed: use `transform.mode`"),
        ("bad-value", "streams[1].transform", "`transform` is a mapping {mode: page or run, steps: [...]}: move "
                                              "these steps to `transform.steps`"),
    ]


def test_the_fields_dsl_was_removed():
    def old(d):
        stream(d).pop("transform")
        stream(d).update(fields=[{"name": "id", "from": "id"}], on_record_error="skip")
    issues = check(old)
    assert codes(issues) == [(ERROR, "removed-key")] * 2  # and no missing `transform` on top
    assert "`fields` was replaced by SQL: `transform` steps (DuckDB SELECTs" in messages(issues)
    assert "`on_record_error` was removed with `fields`" in messages(issues)


def test_removed_models_key_and_folder(tmp_path):
    issues = check(lambda d: d.update(models=[{"name": "m", "primary_key": ["id"], "select": "SELECT 1"}]))
    assert codes(issues) == [(ERROR, "removed-key")]
    assert "models were replaced by `transform` steps: a stream's SQL steps join and aggregate its own " \
           "`requests`" in messages(issues)

    folder = write_folder(tmp_path)
    (folder / "models").mkdir()
    (folder / "models" / "m.yaml").write_text("select: SELECT 1\nprimary_key: [id]\n")
    issues = validate_source(str(folder))
    assert [(os.path.relpath(i.file, str(folder)), i.code) for i in issues] == [("models", "source-folder")]
    assert "models were replaced by `transform` steps" in messages(issues)


def test_stream_description():
    assert check(lambda d: stream(d).update(description="Each account's campaigns.")) == []
    assert found(check(lambda d: stream(d).update(description=["a"]))) == [
        ("bad-value", "streams[0].description", "expected text, got a list")]


# * ---------------------
# * transform and exports
# * ---------------------

def test_transform_is_a_mapping_with_mode_and_steps():
    assert found(check(lambda d: stream(d)["transform"].pop("mode"))) == [
        ("missing-key", "streams[0].transform", "`transform` needs `mode`")]
    assert found(check(lambda d: stream(d)["transform"].update(mode="pages"))) == [
        ("bad-value", "streams[0].transform.mode", "unknown transform mode 'pages' (did you mean 'page'?); one of: "
                                                   "page, run")]
    assert found(check(lambda d: stream(d)["transform"].update(steps=[]))) == [
        ("bad-value", "streams[0].transform.steps", "expected a non-empty list of steps, got a list")]
    issues = check(lambda d: stream(d)["transform"].update(step=stream(d)["transform"].pop("steps")))
    assert [(i.code, i.path) for i in issues] == [("missing-key", "streams[0].transform"),
                                                  ("unknown-key", "streams[0].transform.step")]
    assert "unknown key 'step' (did you mean 'steps'?); `transform` does not support it" in messages(issues)
    assert found(check(lambda d: stream(d).update(transform=None))) == [
        ("bad-value", "streams[0].transform", "expected a mapping, got null")]


def test_steps():
    def with_select(query):
        def mutate(d):
            d["spec"]["config"]["currency"] = {"type": "string"}  # read only in SQL: not reported as unused
            steps(d)[0]["select"] = query
        return mutate
    assert check(with_select("SELECT $currency AS currency, record->>'id' AS id FROM raw_campaigns")) == []
    cases = [
        (with_select(""), [(ERROR, "bad-value"), (WARNING, "unused-input")], "`select` must be a SQL query"),
        (with_select(None), [(ERROR, "bad-value"), (WARNING, "unused-input")],
         "`select` must be a SQL query, got null"),
        (with_select("SELECT '{{ config.currency }}' AS c FROM raw_campaigns"), [(ERROR, "bad-value")],
         "`select` is SQL: read config values as `$name` parameters (or the columns of the request tables: record, "
         "partition, config, window_start, window_end, today), not with `{{ ... }}` references"),
    ]
    for mutate, expected, message in cases:
        issues = check(mutate)
        assert codes(issues) == expected and message in messages(issues), issues
        assert [i.path for i in issues if i.severity == ERROR] == ["streams[0].transform.steps[0].select"]

    issues = check(lambda d: steps(d)[0].update(selct="SELECT 1", description=5))
    assert found(issues) == [
        ("unknown-key", "streams[0].transform.steps[0].selct", "unknown key 'selct' (did you mean 'select'?); steps "
                                                               "do not support it"),
        ("bad-value", "streams[0].transform.steps[0].description", "expected text, got a number"),
    ]
    assert found(check(lambda d: steps(d).append({"select": "SELECT 1"}))) == [
        ("missing-key", "streams[0].transform.steps[1]", "a step needs `name`")]
    assert found(check(lambda d: steps(d).append("SELECT 1"))) == [
        ("bad-value", "streams[0].transform.steps[1]", "expected a step mapping {name, select}, got text "
                                                       "'SELECT 1'")]


def test_braces_in_a_select_are_references_only_as_templates_read_them():
    def with_select(query):
        return lambda d: steps(d)[0].update(select=query)
    # `}}` ends a nested struct literal, and a lone `{{` is text: neither is a reference
    for query in ("SELECT {'meta': {'name': record->>'name'}} AS meta, record->>'id' AS id FROM raw_campaigns",
                  "SELECT replace(record->>'name', '{{', '') AS name, record->>'id' AS id FROM raw_campaigns"):
        assert check(with_select(query)) == [], query
    issues = check(with_select("SELECT {{ config.account_ids }} AS ids, record->>'id' AS id FROM raw_campaigns"))
    assert found(issues) == [("bad-value", "streams[0].transform.steps[0].select", "`select` is SQL: read config "
                              "values as `$name` parameters (or the columns of the request tables: record, partition, "
                              "config, window_start, window_end, today), not with `{{ ... }}` references")]


def test_export_checks():
    cases = [
        (lambda e: e["campaigns"].update(step="campaign"), "unknown-step", "streams[0].export.campaigns.step",
         "unknown step 'campaign' (did you mean 'campaigns'?)"),
        (lambda e: e["campaigns"].update(step="raw_campaigns"), "bad-value", "streams[0].export.campaigns.step",
         "'raw_campaigns' is a request: an export writes a step's rows (for the records as returned: a step "
         "`SELECT record FROM raw_campaigns`)"),
        (lambda e: e["campaigns"].update(step=["campaigns"]), "bad-value", "streams[0].export.campaigns.step",
         "expected a step name, got a list"),
        (lambda e: e["campaigns"].pop("step"), "missing-key", "streams[0].export.campaigns", "an export needs `step`"),
        (lambda e: e["campaigns"].update(primary_key=[]), "bad-value", "streams[0].export.campaigns.primary_key",
         "expected a non-empty list of column names, got a list"),
        (lambda e: e["campaigns"].update(primary_key=["id", 1]), "bad-value",
         "streams[0].export.campaigns.primary_key[1]", "expected a column name, got a number"),
        (lambda e: e["campaigns"].update(key=["id"]), "unknown-key", "streams[0].export.campaigns.key",
         "unknown key 'key'; exports do not support it"),
        (lambda e: e.update({"bad-name": {"step": "campaigns"}}), "bad-value", "streams[0].export['bad-name']",
         "'bad-name' is not a valid name (letters, digits and _; not starting with a digit)"),
        (lambda e: e.update(other="campaigns"), "bad-value", "streams[0].export.other",
         "expected an export mapping {step, primary_key?}, got text 'campaigns'"),
        (lambda e: e.update(CAMPAIGNS={"step": "campaigns"}), "duplicate-export", "streams[0].export.CAMPAIGNS",
         "export name 'CAMPAIGNS' is used twice (names ignore case)"),
    ]
    for mutate, code, path, message in cases:
        assert found(check(lambda d: mutate(stream(d)["export"]))) == [(code, path, message)]
    assert check(lambda d: stream(d)["export"]["campaigns"].update(step="Campaigns")) == []  # names ignore case


def test_keys_are_column_names():
    # which columns a step has is known when DuckDB compiles it (streamwright validate, streamwright run): see test_source_sql
    def mutate(d):
        stream(d)["export"]["campaigns"]["primary_key"] = ["idd"]
        stream(d)["incremental"] = {"cursor_field": "date", "start": "-30d"}
        request(d)["http"]["params"] = {"since": "{{ window.start }}"}
    assert check(mutate) == []


def test_page_mode_reads_one_request_without_request_partitions():
    def two_requests(d):
        stream(d)["requests"].append({"name": "raw_stats", "http": {"path": "/stats"}})
    assert found(check(two_requests)) == [
        ("bad-value", "streams[0].transform.mode", "`mode: page` runs the steps on each page of one request, and this "
                                                   "stream has 2: use `mode: run` to read several requests")]
    assert check(lambda d: (two_requests(d), stream(d)["transform"].update(mode="run"))) == []

    def request_partitions(d):
        request(d)["partitions"] = [{"name": "status", "values": ["active", "paused"]}]
    assert found(check(request_partitions)) == [
        ("bad-value", "streams[0].requests[0].partitions", "request-level partitions need `transform.mode: run`")]
    assert check(lambda d: (request_partitions(d), stream(d)["transform"].update(mode="run"))) == []


def test_incremental_needs_a_request_that_uses_the_window():
    def without_window(d):
        stream(d)["incremental"] = {"cursor_field": "date", "start": "2025-01-01", "window": "1d"}
    assert found(check(without_window)) == [
        ("bad-value", "streams[0].incremental", "`incremental` needs a request that uses `{{ window.start }}` or "
                                                "`{{ window.end }}`")]

    def with_window(d):
        without_window(d)
        request(d)["http"]["params"] = {"start": "{{ window.start }}"}
    assert check(with_window) == []

    def one_of_two_requests(d):  # run mode: the other request runs once per stream partition
        without_window(d)
        stream(d)["transform"]["mode"] = "run"
        stream(d)["requests"].append({"name": "raw_stats", "http": {"path": "/stats", "params": {
            "day": "{{ window.end | date('%Y%m%d') }}"}}})
    assert check(one_of_two_requests) == []

    def in_an_async_job(d):
        without_window(d)
        sdk_source(d)
        request(d).update(async_job={
            "submit": {"sdk": "google_ads", "method": "Submit", "arguments": {"since": "{{ window.start }}"}},
            "poll": {"method": "Poll", "every": "15s", "timeout": "30m", "done_when": {"path": "Status", "equals": 1}},
            "results": {"sdk": "google_ads", "method": "Results"}})
        for key in ("sdk", "service", "method", "arguments"):
            request(d).pop(key)
    assert check(in_an_async_job) == []


def test_incremental_and_window():
    def mutate(d):
        stream(d)["incremental"] = {"cursor_field": "id", "start": "{{ config.account_ids }}", "window": "1d",
                                    "lookback": "30d"}
        request(d)["http"]["params"] = {"since": "{{ window.start }}", "until": "{{ window.end }}"}
    assert check(mutate) == []

    def bad(d):
        mutate(d)
        stream(d)["incremental"].update(start="last week", window="1 day")
    assert codes(check(bad)) == [(ERROR, "bad-value")] * 2


@pytest.mark.parametrize("window,lookback,problem", [
    ("1d", "0d", None),
    ("0d", None, "`window` is whole days (at least 1d), got '0d'"),  # 0d never advances
    ("12h", None, "`window` is whole days (at least 1d), got '12h'"),
    ("24h", None, "`window` is whole days (at least 1d), got '24h'"),
    ("7d", "6h", "`lookback` is whole days, got '6h'"),
])
def test_incremental_durations_are_whole_days(window, lookback, problem):
    def mutate(d):
        incremental = {"cursor_field": "id", "start": "-30d", "window": window}
        if lookback:
            incremental["lookback"] = lookback
        stream(d)["incremental"] = incremental
        request(d)["http"]["params"] = {"day": "{{ window.start }}"}
    issues = check(mutate)
    assert messages(issues) == ("" if problem is None else "incremental " + problem)
    jsonschema = pytest.importorskip("jsonschema")
    doc = copy.deepcopy(BASE)
    mutate(doc)
    valid = jsonschema.Draft7Validator(schema.json_schema()).is_valid(doc)
    assert valid == (problem is None)


# * -----
# * names
# * -----

def test_request_and_step_names_share_one_namespace():
    def mutate(d):
        stream(d)["transform"]["mode"] = "run"
        stream(d)["requests"] += [{"name": "Raw_Campaigns", "http": {"path": "/b"}},
                                  {"name": "raw_stats", "http": {"path": "/c"}}]
        steps(d).extend([{"name": "CAMPAIGNS", "select": "SELECT 1 AS id"},
                         {"name": "raw_stats", "select": "SELECT 1 AS id"}])
    assert found(check(mutate)) == [
        ("duplicate-request", "streams[0].requests[1].name",
         "request name 'Raw_Campaigns' is used twice (names ignore case)"),
        ("duplicate-step", "streams[0].transform.steps[1].name", "step name 'CAMPAIGNS' is used twice (names ignore "
                                                                 "case)"),
        ("duplicate-step", "streams[0].transform.steps[2].name", "step 'raw_stats' has the name of request "
                                                                 "'raw_stats': a stream's requests and steps are its "
                                                                 "tables, so their names are unique"),
    ]


def test_records_is_not_a_reserved_name():
    assert check(lambda d: request(d).update(name="records")) == []  # the step's FROM: DuckDB checks it

    def step_named_records(d):
        steps(d)[0]["name"] = "records"
        stream(d)["export"]["campaigns"]["step"] = "records"
    assert check(step_named_records) == []


def test_tables_are_private_to_their_stream():
    """Request and step names may be other streams' names or exports: streams do not read each other's tables."""
    def mutate(d):
        d["streams"].append(new_stream("ads", export={"ad_rows": {"step": "ads"}}))
        stream(d)["transform"]["mode"] = "run"
        stream(d)["requests"].append({"name": "ads", "http": {"path": "/ads"}})
        steps(d).append({"name": "ad_rows", "select": "SELECT 1 AS id FROM ads"})
        steps(d, 1)[0]["name"] = "campaigns"
        stream(d, 1)["export"]["ad_rows"]["step"] = "campaigns"
    assert check(mutate) == []


def test_export_names():
    def own_names(d):  # an export may have its stream's name, or the name of one of its requests or steps
        stream(d)["export"].update(raw_campaigns={"step": "campaigns"})
    assert check(own_names) == []

    def clashes(d):
        d["streams"].append(new_stream("ads", export={"Campaigns": {"step": "ads"}, "ad_rows": {"step": "ads"}}))
        d["streams"].append(new_stream("stats", export={"AD_ROWS": {"step": "stats"}}))
    assert found(check(clashes)) == [
        ("duplicate-export", "streams[1].export.Campaigns", "export 'Campaigns' has the name of stream 'campaigns': "
                                                            "an export can have its own stream's name, not another "
                                                            "stream's"),
        ("duplicate-export", "streams[2].export.AD_ROWS", "export 'AD_ROWS' is also an export of stream 'ads' (names "
                                                          "ignore case): export names are unique in the source"),
    ]


def test_duplicate_streams_and_partitions():
    def mutate(d):
        d["streams"].append(copy.deepcopy(stream(d)))  # its export is not reported on top
        stream(d)["partitions"].append({"name": "account_id", "values": ["1"]})
    assert codes(check(mutate)) == [(ERROR, "duplicate-partition"), (ERROR, "duplicate-stream")]


# * ---------------------------------------------
# * request partitions: earlier requests and steps
# * ---------------------------------------------

@pytest.mark.parametrize("source,code,message", [
    ("raw_campaigns", None, None),  # an earlier request: `field` is a dotted path in its records
    ("campaigns", None, None),      # a step: `field` is a column
    ("CAMPAIGNS", None, None),      # names ignore case
    ("totals", None, None),         # any step: requests and steps run in dependency order
    ("raw_ads", "bad-value", "a request cannot partition on itself: use an earlier request or a step"),
    ("raw_later", "bad-value", "request partition source 'raw_later' is a later request: use an earlier request or "
                               "a step"),
    ("campaign_list", "bad-value", "'campaign_list' is this stream's export, which is written after its requests "
                                   "and steps run; read its step 'campaigns'"),
    ("ads", "bad-value", "'ads' is not a request or step of this stream: requests read only their own stream's "
                         "requests and steps; to partition by stream 'ads', use a stream partition `{from_stream: "
                         "ads, ...}`"),
    ("ad_list", "bad-value", "'ad_list' is not a request or step of this stream: requests read only their own "
                             "stream's requests and steps; to partition by stream 'ads', use a stream partition "
                             "`{from_stream: ads, ...}`"),
    ("campaignz", "unknown-source", "unknown request partition source 'campaignz' (did you mean 'campaigns'?): use an "
                                    "earlier request or a step of this stream"),
])
def test_request_partition_sources(source, code, message):
    def mutate(d):
        requests_stream(d, [{"name": "campaign_id", "from": source, "field": "id"}])
        stream(d)["requests"].append({"name": "raw_later", "http": {"path": "/later"}})
        steps(d).append({"name": "totals", "select": "SELECT count(*) AS id FROM campaigns"})
        stream(d)["export"] = {"campaign_list": {"step": "campaigns"}}
        d["streams"].append(new_stream("ads", export={"ad_list": {"step": "ads"}}))
    expected = [] if code is None else [(code, "streams[0].requests[1].partitions[0].from", message)]
    assert found(check(mutate)) == expected


def test_request_partition_sources_are_checked_once_the_steps_are_known():
    def mutate(d):
        requests_stream(d, [{"name": "campaign_id", "from": "campaign_rows", "field": "id"}])
        stream(d).pop("transform")
    assert found(check(mutate)) == [("missing-key", "streams[0]", "a stream needs `transform`: {mode: page or run, "
                                                                  "steps: [{name, select}, ...]}")]


def test_request_partitions_from_a_source_can_name_stream_partitions():
    # `account_id` names the stream partition: only the rows with its value are used
    assert check(lambda d: requests_stream(d, [{"from": "campaigns", "fields": ["account_id", "campaign_id"]}])) == []
    assert check(lambda d: requests_stream(d, [{"name": "account_id", "from": "campaigns", "field": "account_id"},
                                               {"name": "campaign_id", "from": "campaigns", "field": "id"}])) == []
    issues = check(lambda d: requests_stream(d, [{"from": "campaigns", "fields": ["account_id", "campaign_id"]},
                                                 {"name": "campaign_id", "from": "campaigns", "field": "id"}]))
    assert codes(issues) == [(ERROR, "duplicate-partition")]
    assert "partition 'campaign_id' is defined twice" in messages(issues)
    issues = check(lambda d: requests_stream(d, [{"name": "account_id", "values": ["a1"]},
                                                 {"name": "campaign_id", "from": "campaigns", "field": "id"}]))
    assert codes(issues) == [(ERROR, "duplicate-partition")]  # values are not rows to match


def test_request_partition_values_see_config_today_and_the_stream_partitions_only():
    # a run makes `values` once per stream partition, before the request: no window, nor the request's own partitions
    assert check(lambda d: requests_stream(d, [
        {"name": "kind", "values": ["x"]},
        {"name": "campaign_id", "values": "{{ partition.account_id }}-{{ config.account_ids | join('+') }}-{{ today }}"}
    ])) == []

    def unavailable(reference, why):
        return ("%s cannot be used in request partition `values`: a run makes them once per stream partition, with "
                "config, today and the stream's partitions only (%s)" % (reference, why))

    def incremental(d):
        requests_stream(d, [{"name": "campaign_id", "values": "{{ window.end }}"}])
        stream(d)["incremental"] = {"cursor_field": "id", "start": "2026-01-01"}
        request(d, item=1)["http"]["params"] = {"day": "{{ window.start }}"}  # the request itself can use it

    def own(d):
        requests_stream(d, [{"name": "kind", "values": ["x", "y"]},
                            {"name": "campaign_id", "values": "{{ partition.kind }}"}])

    def filtered(d):  # a stream without `incremental`: the same
        requests_stream(d, [{"name": "campaign_id", "values": "{{ window.end | date('%Y') }}"}])
    assert found(check(incremental)) == [("unavailable-reference", "streams[0].requests[1].partitions[0].values",
                                          unavailable("{{ window.end }}", "no window"))]
    assert found(check(filtered)) == [("unavailable-reference", "streams[0].requests[1].partitions[0].values",
                                       unavailable("{{ window.end | date('%Y') }}", "no window"))]
    assert found(check(own)) == [("unavailable-reference", "streams[0].requests[1].partitions[1].values",
                                  unavailable("{{ partition.kind }}", "'kind' is a partition of this request"))]

    # without stream partitions, `partition` cannot be used at all
    def unpartitioned(d):
        requests_stream(d, [{"name": "campaign_id", "values": "{{ partition.account_id }}"}])
        stream(d).pop("partitions")
        request(d)["http"]["path"] = "/campaigns"
    assert ("unavailable-reference", "streams[0].requests[1].partitions[0].values",
            "{{ partition.account_id }}: stream 'campaigns' has no partitions") in found(check(unpartitioned))


# * ---------------------------------
# * request partitions: batch_size
# * ---------------------------------

def test_batch_size_on_request_partitions():
    for item in ({"name": "campaign_id", "values": ["c1", "c2", "c3"], "batch_size": 2},
                 {"name": "campaign_id", "values": "{{ config.account_ids }}", "batch_size": 1},
                 # a request's records are those read under the stream partition: scoped already
                 {"name": "campaign_id", "from": "raw_campaigns", "field": "id", "batch_size": 1},
                 # a step's rows: `account_id` keeps the stream partition's, `campaign_id` is batched and holds the list
                 {"from": "campaigns", "fields": ["account_id", "campaign_id"], "batch_size": 200},
                 {"from": "CAMPAIGNS", "fields": ["campaign_id", "account_id"], "batch_size": 1},
                 {"from": "raw_campaigns", "fields": ["account_id", "campaign_id"], "batch_size": 10},
                 {"from": "raw_campaigns", "fields": ["campaign_id"], "batch_size": 10}):
        assert check(lambda d: requests_stream(d, [item])) == [], item


def test_batch_size_from_a_step_in_a_stream_without_partitions():
    def unpartitioned(item):
        def mutate(d):
            requests_stream(d, [item])
            stream(d).pop("partitions")
            del d["spec"]["config"]
            request(d)["http"]["path"] = "/campaigns"
            request(d, item=1)["http"]["path"] = "/ads"
        return mutate
    for item in ({"name": "campaign_ids", "from": "campaigns", "field": "id", "batch_size": 200},
                 {"from": "campaigns", "fields": ["id"], "batch_size": 200}):
        assert check(unpartitioned(item)) == [], item
    assert found(check(unpartitioned({"from": "campaigns", "fields": ["id", "account_id"], "batch_size": 2}))) == [
        ("bad-value", "streams[0].requests[1].partitions[0].batch_size",
         "`batch_size` on `{from, fields}` batches one field into one list, but 'id', 'account_id' are not stream "
         "partitions: list only the stream partitions (the stream has none) and the one field to batch")]


def test_batch_size_from_a_step_in_a_partitioned_stream_must_list_the_stream_partitions():
    def unscoped(fields):
        return ("`batch_size`: step 'campaigns' has the rows of every stream partition, so each list would mix them: "
                "batching a step's values in a partitioned stream must scope each partition: use `{from: campaigns, "
                "fields: [%s], batch_size: 200}` (the list is named after the field)" % fields)
    where = "streams[0].requests[1].partitions[0].batch_size"
    assert found(check(lambda d: requests_stream(d, [
        {"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": 200}]))) == [
        ("bad-value", where, unscoped("account_id, id"))]
    assert found(check(lambda d: requests_stream(d, [
        {"from": "campaigns", "fields": ["campaign_id"], "batch_size": 200}]))) == [
        ("bad-value", where, unscoped("account_id, campaign_id"))]
    # every field a stream partition: nothing of its own to batch
    assert found(check(lambda d: requests_stream(d, [{"from": "raw_campaigns", "fields": ["account_id"],
                                                      "batch_size": 9},
                                                     {"name": "campaign_id", "values": ["c1"]}]))) == [
        ("bad-value", where, "`batch_size`: 'account_id' is a stream partition, so this item only keeps the rows with "
                             "its value: it has no values of its own to batch; add the field to batch to `fields`")]

    def two_partitions(item):
        def mutate(d):
            requests_stream(d, [item])
            stream(d)["partitions"].append({"name": "kind", "values": ["x", "y"]})
        return mutate
    # every stream partition field: `kind` too, or each (account, kind) would list the other kind's values
    assert found(check(two_partitions({"from": "campaigns", "fields": ["account_id", "campaign_id"],
                                       "batch_size": 200}))) == [
        ("bad-value", where, unscoped("account_id, kind, campaign_id"))]
    assert check(two_partitions({"from": "campaigns", "fields": ["kind", "account_id", "campaign_id"],
                                 "batch_size": 200})) == []
    assert check(two_partitions({"name": "campaign_id", "from": "raw_campaigns", "field": "id",
                                 "batch_size": 200})) == []


@pytest.mark.parametrize("item,where,message", [
    ({"from": "campaigns", "fields": ["account_id", "campaign_id", "kind"], "batch_size": 10},
     "requests[1].partitions[0]", "`batch_size` on `{from, fields}` batches one field into one list, but "
     "'campaign_id', 'kind' are not stream partitions: list only the stream partitions (account_id) and the one field "
     "to batch"),
    ({"name": "campaign_id", "values": ["c1"], "batch_size": 0}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got 0"),
    ({"name": "campaign_id", "values": ["c1"], "batch_size": -3}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got -3"),
    ({"from": "campaigns", "fields": ["account_id", "campaign_id"], "batch_size": 0}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got 0"),
    ({"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": "200"}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got '200'"),
    ({"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": 2.5}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got 2.5"),
    ({"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": True}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got True"),
    ({"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": [2]}, "requests[1].partitions[0]",
     "`batch_size` must be a whole number of at least 1 (the most values in one request's list), got a list"),
])
def test_batch_size_rejected_on_request_partitions(item, where, message):
    assert found(check(lambda d: requests_stream(d, [item]))) == [
        ("bad-value", "streams[0].%s.batch_size" % where, message)]


def test_batch_size_rejected_on_items_that_name_a_stream_partition_and_on_stream_partitions():
    issues = check(lambda d: requests_stream(d, [
        {"name": "account_id", "from": "campaigns", "field": "account_id", "batch_size": 5},
        {"name": "campaign_id", "from": "campaigns", "field": "id"}]))
    assert found(issues) == [("bad-value", "streams[0].requests[1].partitions[0].batch_size", (
        "`batch_size`: 'account_id' is a stream partition, so this item only keeps the rows with its value: it has no "
        "values of its own to batch"))]

    def stream_level(form):
        return ("`batch_size` is not supported on %s: each stream partition is one value, with its own state; batch a "
                "request partition of a run-mode stream instead ({name, values, batch_size} or {name, from, field, "
                "batch_size})" % form)
    issues = check(lambda d: stream(d)["partitions"][0].update(batch_size=10))
    assert found(issues) == [("bad-value", "streams[0].partitions[0].batch_size", stream_level("stream partitions"))]

    def from_stream(d):
        d["streams"].append(new_stream("ads", partitions=[
            {"name": "campaign_id", "from_stream": "campaigns", "field": "id", "batch_size": 10}],
            path="/ads/{{ partition.campaign_id }}"))
        d["streams"].append(new_stream("ad_groups", partitions=[
            {"from_stream": "campaigns", "fields": ["id"], "batch_size": 10}], path="/ad_groups/{{ partition.id }}"))
    assert found(check(from_stream)) == [
        ("bad-value", "streams[1].partitions[0].batch_size", stream_level("a `from_stream` partition")),
        ("bad-value", "streams[2].partitions[0].batch_size", stream_level("a `from_stream` partition"))]


def test_schemas_take_batch_size_on_request_partitions_only():
    jsonschema = pytest.importorskip("jsonschema")

    def valid(mutate):
        doc = copy.deepcopy(BASE)
        mutate(doc)
        try:
            jsonschema.validate(doc, schema.json_schema(), cls=jsonschema.Draft7Validator)
        except jsonschema.ValidationError:
            return False
        return True
    assert valid(lambda d: requests_stream(d, [{"name": "campaign_id", "values": ["c1"], "batch_size": 1}]))
    assert valid(lambda d: requests_stream(d, [{"name": "campaign_id", "from": "campaigns", "field": "id",
                                                "batch_size": 200}]))
    assert valid(lambda d: requests_stream(d, [{"from": "campaigns", "fields": ["account_id", "campaign_id"],
                                                "batch_size": 200}]))
    for item in ({"from": "campaigns", "fields": ["account_id", "campaign_id"], "batch_size": 0},
                 {"from": "campaigns", "fields": ["campaign_id"], "batch_size": "2"},
                 {"name": "campaign_id", "values": ["c1"], "batch_size": 0},
                 {"name": "campaign_id", "from": "campaigns", "field": "id", "batch_size": "2"}):
        assert not valid(lambda d: requests_stream(d, [item])), item
    assert not valid(lambda d: stream(d)["partitions"][0].update(batch_size=2))
    assert not valid(lambda d: d["streams"].append(new_stream("ads", partitions=[
        {"name": "campaign_id", "from_stream": "campaigns", "field": "id", "batch_size": 2}])))


def test_request_partition_items():
    cases = [
        ([{"from": "campaigns", "fields": ["campaign_id"], "field": "id"}], "unknown-key",
         "unknown key 'field' (did you mean 'fields'?); a request partition with `fields` (named after the fields) "
         "does not support it"),
        ([{"from": "campaigns", "fields": []}], "bad-value", "expected a non-empty list of field names, got a list"),
        ([{"name": "campaign_id", "from": "campaigns"}], "missing-key", "a request partition needs `field`"),
        ([{"name": "campaign_id", "values": 5}], "bad-value",
         "expected a list or a reference such as \"{{ config.ids }}\", got a number"),
        ([], "bad-value", "expected a non-empty list of request partitions, got a list"),
    ]
    for partitions, code, message in cases:
        issues = [i for i in check(lambda d: requests_stream(d, partitions)) if i.code != "unknown-reference"]
        assert [(i.code, i.message) for i in issues] == [(code, message)], partitions


# * --------------------------------------------
# * stream partitions: from_stream, cycles, names
# * --------------------------------------------

def test_partitions_from_other_streams():
    def mutate(d):
        d["streams"].append(new_stream("ads", [{"name": "campaign_id", "from_stream": "campaigns", "field": "idd"}],
                                       "/campaigns/{{ partition.campaign_id }}/ads"))
    assert check(mutate) == []  # whether `idd` is a column of the parent's export: DuckDB checks it

    def cycle(d):
        mutate(d)
        stream(d, 1)["partitions"][0].update(from_stream="Campaigns", field="id")  # names ignore case
        stream(d)["partitions"] = [{"name": "account_id", "from_stream": "ADS", "field": "id"}]
    issues = check(cycle)
    assert codes(issues) == [(ERROR, "partition-cycle"), (WARNING, "unused-input")]
    assert "partitions form a cycle: ads -> campaigns -> ads" in messages(issues)

    def unknown(d):
        stream(d)["partitions"] = [{"name": "account_id", "from_stream": "campaign", "field": "id"}]
    assert "unknown stream 'campaign' (did you mean 'campaigns'?)" in messages(check(unknown))


def test_from_stream_reads_a_stream_with_exactly_one_export():
    def child(parent):
        def mutate(d):
            d["streams"].append(new_stream("ads", [{"name": "campaign_id", "from_stream": parent, "field": "id"}],
                                           "/campaigns/{{ partition.campaign_id }}/ads"))
        return mutate
    assert check(child("campaigns")) == []

    def two_exports(d):
        child("campaigns")(d)
        stream(d)["export"]["campaign_totals"] = {"step": "campaigns"}
    assert found(check(two_exports)) == [
        ("bad-value", "streams[1].partitions[0].from_stream", "stream 'campaigns' has 2 exports (campaigns, "
                                                              "campaign_totals): `from_stream` reads a stream with "
                                                              "exactly one export")]

    def an_export(d):
        child("campaign_list")(d)
        stream(d)["export"] = {"campaign_list": {"step": "campaigns"}}
    assert found(check(an_export)) == [
        ("unknown-stream", "streams[1].partitions[0].from_stream", "unknown stream 'campaign_list'; 'campaign_list' is "
                                                                   "an export of stream 'campaigns': use `from_stream: "
                                                                   "campaigns`")]

    def itself(d):  # and no cycle on top
        stream(d)["partitions"].append({"name": "parent_id", "from_stream": "campaigns", "field": "id"})
    assert found(check(itself)) == [
        ("bad-value", "streams[0].partitions[1].from_stream", "a stream cannot partition on itself")]


def test_partitions_from_several_fields_of_a_parent():
    def child(partitions, path="/campaigns/{{ partition.id }}/ads?account={{ partition.account_id }}"):
        def mutate(d):
            d["streams"].append(new_stream("ads", partitions, path))
        return mutate

    assert check(child([{"from_stream": "campaigns", "fields": ["id", "account_id"]}])) == []
    cases = [
        ([{"from_stream": "campaigns", "fields": ["id", "account_id", "id"]}], [(ERROR, "duplicate-partition")],
         "partition 'id' is defined twice"),
        ([{"from_stream": "campaigns", "fields": ["id", "account_id"]}, {"name": "id", "values": ["1"]}],
         [(ERROR, "duplicate-partition")], "partition 'id' is defined twice"),
        ([{"from_stream": "campaigns", "fields": ["id", "account_id"], "name": "id"}], [(ERROR, "unknown-key")],
         "a partition with `fields` (named after the fields) does not support it"),
        ([{"from_stream": "campaigns", "fields": ["id", "account_id"], "field": "id"}], [(ERROR, "unknown-key")],
         "unknown key 'field' (did you mean 'fields'?)"),
        ([{"from_stream": "campaigns", "fields": []}], [(ERROR, "bad-value")],
         "expected a non-empty list of field names"),
    ]
    for partitions, expected, message in cases:
        issues = check(child(partitions))  # and the partitions the path then misses, for the last case
        assert [c for c in codes(issues) if not c[1].endswith("-reference")] == expected, (partitions, issues)
        assert message in messages(issues), (partitions, messages(issues))
    issues = check(child([{"from_stream": "campaigns", "fields": ["id"]}]))  # the path also uses account_id
    assert codes(issues) == [(ERROR, "unknown-reference")] and "partition.account_id" in messages(issues)

    def cycle(d):
        child([{"from_stream": "campaigns", "fields": ["id", "account_id"]}])(d)
        stream(d)["partitions"] = [{"from_stream": "ads", "fields": ["id"]}]
    issues = check(cycle)
    assert (ERROR, "partition-cycle") in codes(issues) and "ads -> campaigns -> ads" in messages(issues)


# * ---------
# * YAML, CLI
# * ---------

def test_unquoted_template_hint():
    issues = validate_text(dedent("""\
        kind: source
        name: x
        streams:
          - name: s
            requests:
              - {name: raw_s, http: {path: /a, params: {q: {{ config.x }}}}}
    """), "s.yaml")
    assert codes(issues) == [(ERROR, "yaml-syntax")]
    assert "quote template values" in issues[0].message


def test_control_characters_are_yaml_errors(tmp_path):
    assert codes(validate_text("kind: source\nname: \x07\n", "s.yaml")) == [(ERROR, "yaml-syntax")]
    folder = write_folder(tmp_path, files={"accounts.yaml": ACCOUNTS + "# \x07\n"})
    issues = validate_source(str(folder))
    assert [(os.path.basename(i.file), i.code) for i in issues] == [("accounts.yaml", "yaml-syntax")]


def test_cli_allow_connector(capsys):
    path = os.path.join(SOURCES, "ads", "google_ads")
    assert main([path]) == 0
    assert main(["--allow-connector", "facebook", path]) == 1
    assert "connector 'google_ads' is not in the allowed list" in capsys.readouterr().out


SOURCE_TEXT = yaml.safe_dump(BASE, sort_keys=False)


def test_yaml_syntax_error_reports_line():
    issues = validate_text(dedent("""\
        kind: source
        http:
          base_url: https://api.example.com
         headers: {}
    """), "s.yaml")
    assert codes(issues) == [(ERROR, "yaml-syntax")]
    assert issues[0].line == 4


def test_duplicate_key_is_an_error():
    issues = validate_text(SOURCE_TEXT.replace("name: demo\n", "name: demo\nname: other\n", 1), "s.yaml")
    assert codes(issues) == [(ERROR, "duplicate-key")]
    assert issues[0].line == 3


def test_unused_anchor_in_a_single_file():
    issues = validate_text(SOURCE_TEXT.replace("name: demo\n", "name: demo\nx-defaults: &unused {a: 1}\n", 1),
                           "s.yaml")
    assert codes(issues) == [(WARNING, "unused-anchor")]
    assert issues[0].line == 3


def test_issue_in_merged_anchor_points_at_the_anchor():
    text = SOURCE_TEXT.replace("name: demo\n", "name: demo\nx-records: &records\n  path: elements\n  pth: x\n", 1)
    text = text.replace("records:\n      path: elements\n", "records:\n      <<: *records\n", 1)
    assert "<<: *records" in text
    issues = validate_text(text, "s.yaml")
    assert codes(issues) == [(ERROR, "unknown-key")]
    assert issues[0].line == 5


def test_single_files_must_be_sources():
    issues = validate_text("kind: conector\nname: x\n", "s.yaml")
    assert codes(issues) == [(ERROR, "unknown-kind")]
    assert issues[0].path == "kind" and "(did you mean 'source'?)" not in issues[0].message
    issues = validate_text("kind: sources\nname: x\n", "s.yaml")
    assert codes(issues) == [(ERROR, "unknown-kind")] and "(did you mean 'source'?)" in issues[0].message
    issues = validate_text(SOURCE_TEXT.replace("kind: source\n", "", 1), "s.yaml")
    assert codes(issues) == [(ERROR, "missing-key")] and "missing `kind: source`" in issues[0].message
    assert validate_text(SOURCE_TEXT.replace("kind: source\n", "", 1), "s.yaml", kind="source") == []
    assert codes(validate_text("", "s.yaml")) == [(ERROR, "empty-document")]
    assert codes(validate_text("- a\n", "s.yaml")) == [(ERROR, "bad-value")]


def test_source_folder_request_item_and_step_errors_report_stream_file_line(tmp_path):
    folder = tmp_path / "demo"
    (folder / "streams").mkdir(parents=True)
    (folder / "source.yaml").write_text("""\
kind: source
name: demo
auth: {provider: google_ads, refresh_token: "{{ secrets.token }}"}
spec: {secrets: {token: {type: string}}}
""")
    stream_file = folder / "streams" / "report.yaml"
    stream_file.write_text("""\
requests:
  - name: customer
    sdk: google_ads
transform:
  mode: run
  steps:
    - {name: shaped, select: "SELECT 1 AS id FROM customer"}
    - {name: totals, select: ""}
export:
  report: {step: shaped}
""")
    issues = validate_file(str(folder))
    assert codes(issues) == [(ERROR, "bad-value"), (ERROR, "missing-key")]
    assert [(issue.file, issue.line, issue.code, issue.path) for issue in issues] == [
        (str(stream_file), 2, "missing-key", "requests[0]"),
        (str(stream_file), 8, "bad-value", "transform.steps[1].select"),
    ]


def test_cli_exit_codes_and_json(tmp_path, capsys, monkeypatch):
    good = tmp_path / "good.yaml"
    good.write_text(SOURCE_TEXT)
    warn = tmp_path / "nested" / "warn.yaml"
    warn.parent.mkdir()
    warn.write_text(SOURCE_TEXT.replace("name: demo\n", "name: demo\nx-defaults: &unused {a: 1}\n", 1))
    bad = tmp_path / "bad.yaml"
    bad.write_text(SOURCE_TEXT.replace("name: demo\n", "name: demo\nstream: []\n", 1))

    assert main([str(good)]) == 0
    assert main([str(warn)]) == 0
    assert main(["--strict", str(warn)]) == 1
    assert main([str(bad)]) == 1
    capsys.readouterr()

    assert main(["--format", "json", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["files"], report["errors"], report["warnings"]) == (3, 1, 1)
    assert [issue["code"] for issue in report["issues"]] == ["unknown-key", "unused-anchor"]

    with pytest.raises(SystemExit):
        main(["--kind", "connector", str(good)])
    capsys.readouterr()

    monkeypatch.delenv("STREAMWRIGHT_CONFIGS", raising=False)
    assert main([]) == 2
    monkeypatch.setenv("STREAMWRIGHT_CONFIGS", str(tmp_path))
    assert main([]) == 1


def test_cli_github_annotations(tmp_path, capsys):
    bad = tmp_path / "bad,1.yaml"
    bad.write_text(SOURCE_TEXT.replace("name: demo\n", "name: demo\nstream: []\n", 1))
    assert main(["--format", "github", str(bad)]) == 1
    annotation = capsys.readouterr().out.splitlines()[0]
    assert annotation.startswith("::error file=%s,line=3,col=" % str(bad).replace(":", "%3A").replace(",", "%2C"))
    assert ",title=streamwright-validate unknown-key::stream: unknown top-level key 'stream'" in annotation


def test_export_schemas_writes_source_and_stream_only(tmp_path, capsys):
    assert main(["--export-schema", str(tmp_path)]) == 0
    assert sorted(os.listdir(str(tmp_path))) == ["source.schema.json", "stream.schema.json"]
    with open(str(tmp_path / "source.schema.json")) as stream_:
        assert json.load(stream_) == schema.json_schema()


# * ---------------------------------------
# * regressions from the review of phase 1
# * ---------------------------------------

def _refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _all_schemas():
    return [("source", schema.json_schema()), ("stream", schema.stream_json_schema())]


@pytest.mark.parametrize("name,schema", _all_schemas(), ids=[n for n, _ in _all_schemas()])
def test_every_schema_reference_resolves(name, schema):
    missing = sorted({ref for ref in _refs(schema) if ref.split("/")[-1] not in schema["definitions"]})
    assert missing == []


def test_schema_accepts_editor_views():
    jsonschema = pytest.importorskip("jsonschema")
    doc = copy.deepcopy(BASE)
    stream(doc)["<<"] = {"on_partition_error": "skip"}  # what an editor in YAML 1.2 mode sees for `<<: *defaults`
    request(doc)["<<"] = {"records": {"path": "data"}}
    stream(doc)["transform"]["<<"] = {"mode": "page"}
    doc["spec"]["secrets"] = None                      # every secret commented out
    jsonschema.validate(doc, schema.json_schema(), cls=jsonschema.Draft7Validator)


@pytest.mark.parametrize("mutate", [
    lambda d: stream(d).update(partitions=True),
    lambda d: d.update(streams=None),
    lambda d: stream(d).update(requests={"name": "r"}),
    lambda d: stream(d).update(requests=[["raw_campaigns"]]),
    lambda d: request(d).update(name=["raw_campaigns"]),
    lambda d: stream(d).update(transform="SELECT 1"),
    lambda d: stream(d)["transform"].update(steps={"name": "s"}),
    lambda d: stream(d)["transform"].update(mode=["page"]),
    lambda d: steps(d)[0].update(name={"a": 1}),
    lambda d: stream(d).update(export=["campaigns"]),
    lambda d: stream(d)["export"].update(campaigns=None),
    lambda d: stream(d)["partitions"].append({"name": "p", "from_stream": ["campaigns"], "field": "id"}),
    lambda d: requests_stream(d, [{"name": "p", "from": ["raw_campaigns"], "field": "id"}]),
])
def test_non_list_values_do_not_crash(mutate):
    assert (ERROR, "bad-value") in codes(check(mutate))


def test_filter_arguments_must_be_literals_and_keys_cannot_reference():
    issues = check(lambda d: request(d)["http"].update(params={
        "r": "{{ config.account_ids | default(secrets.token) }}"}))
    assert codes(issues) == [(ERROR, "template-syntax")] and "quoted text or numbers" in issues[0].message
    issues = check(lambda d: request(d)["http"].update(params={"{{ secrets.token }}": "x"}))
    assert codes(issues) == [(ERROR, "template-syntax")] and "mapping keys" in issues[0].message
    assert check(lambda d: request(d)["http"].update(params={
        "ids": "{{ config.account_ids | join('|') }}", "n": "{{ config.account_ids | default(5) }}"})) == []


def test_unquoted_dates_and_non_date_starts():
    text = yaml.safe_dump(BASE, sort_keys=False).replace(
        "  secrets:", "    start_date: {type: date, default: 2025-01-01}\n  secrets:", 1)
    issues = validate_text(text, "s.yaml")
    assert codes(issues) == [(WARNING, "unused-input")]  # the date is accepted; only "never used" remains

    def start(value):
        def mutate(d):
            stream(d)["incremental"] = {"cursor_field": "id", "start": value}
            request(d)["http"]["params"] = {"since": "{{ window.start }}"}
        return mutate
    for value in (-30, {"days": 30}):
        assert found(check(start(value)))[0][:2] == ("bad-value", "streams[0].incremental.start")
        assert codes(check(start(value))) == [(ERROR, "bad-value")]


@pytest.mark.parametrize("mutate", [
    lambda d: request(d).update(paginator={"type": "offset", "offset_param": None, "limit_param": "count",
                                           "page_size": 10}),
    lambda d: d["streams"].append(new_stream("ads", [{"name": "p", "from_stream": None, "field": "id"}])),
    lambda d: d.update(auth={"type": "basic", "username": ["a"], "password": "{{ secrets.token }}"}),
])
def test_required_values_are_type_checked(mutate):
    assert (ERROR, "bad-value") in codes(check(mutate))


def test_async_job_values_are_type_checked():
    def mutate(d):
        sdk_source(d)
        stream(d)["requests"] = [{"name": "raw_campaigns", "async_job": {
            "submit": {"sdk": "google_ads", "method": "Submit"},
            "poll": {"method": None, "every": "15s", "timeout": "30m", "done_when": {"path": None, "equals": 1}},
            "download": {"url": None, "format": "csv"},
        }}]
    assert codes(check(mutate)) == [(ERROR, "bad-value")] * 3


def test_async_jobs_and_http_requests_need_the_right_auth():
    def http_job(d):
        stream(d)["requests"] = [{"name": "raw_campaigns", "async_job": {
            "submit": {"http": {"path": "/reports", "method": "POST"}},
            "poll": {"method": "Poll", "every": "15s", "timeout": "30m", "done_when": {"path": "Status",
                                                                                       "equals": "Success"}},
            "results": {"http": {"path": "/reports/{{ submit.id }}"}},
        }}]
    issues = check(http_job)
    assert codes(issues) == [(ERROR, "bad-value")] * 2
    assert "`submit` must be an sdk request" in messages(issues) and "`results` must be" in messages(issues)

    def http_with_provider(d):
        d["auth"] = {"provider": "google_ads", "refresh_token": "{{ secrets.token }}"}
    issues = check(http_with_provider)
    assert codes(issues) == [(ERROR, "bad-value")]
    assert "auth provider 'google_ads' authenticates sdk requests only" in messages(issues)


def test_bare_equals_operator_loads():
    text = yaml.safe_dump(BASE, sort_keys=False)
    assert "\n  transform:\n" in text
    issues = validate_text(text.replace("\n  transform:\n", "\n  x: {op: =}\n  transform:\n", 1), "s.yaml")
    assert [i.code for i in issues] == ["unknown-key"]  # loads fine; `x` itself is just not a stream key


# * --------------
# * source folders
# * --------------

FOLDER_SOURCE = dedent("""\
    kind: source
    name: demo
    spec:
      config:
        account_ids: {type: list, items: string}
      secrets:
        token: {type: string}
    auth: {type: bearer, token: "{{ secrets.token }}"}
    http: {base_url: "https://api.example.com"}
""")

ACCOUNTS = dedent("""\
    requests:
      - name: raw_accounts
        http: {path: /accounts, params: {ids: "{{ config.account_ids | join(',') }}"}}
        records: {path: data}
    transform:
      mode: page
      steps:
        - {name: accounts, select: "SELECT record->>'id' AS account_id FROM raw_accounts"}
    export:
      accounts: {step: accounts}
""")

CAMPAIGNS = dedent("""\
    partitions:
      - {name: account_id, from_stream: accounts, field: account_id}
    requests:
      - name: raw_campaigns
        http: {path: "/accounts/{{ partition.account_id }}/campaigns"}
        records: {path: data}
    transform:
      mode: page
      steps:
        - name: campaigns
          select: |
            SELECT record->>'id' AS campaign_id, partition->>'account_id' AS account_id
            FROM raw_campaigns
    export:
      campaigns:
        step: campaigns
        primary_key: [campaign_id]
""")


def write_folder(tmp_path, source=FOLDER_SOURCE, files=None):
    """A source folder `demo`: source.yaml and `files` ({file name: text}) in streams/."""
    folder = tmp_path / "demo"
    (folder / "streams").mkdir(parents=True)
    (folder / "source.yaml").write_text(source)
    files = files if files is not None else {"accounts.yaml": ACCOUNTS, "campaigns.yaml": CAMPAIGNS}
    for name, text in files.items():
        (folder / "streams" / name).write_text(text)
    return folder


def where(issues, folder):
    return [(os.path.relpath(i.file, str(folder)), i.line, i.code, i.path) for i in issues]


def test_a_source_folder_is_one_source(tmp_path, monkeypatch):
    folder = write_folder(tmp_path)
    source = load_source(str(folder))
    assert [s["name"] for s in source["streams"]] == ["accounts", "campaigns"]
    assert source["streams"][1]["partitions"][0]["from_stream"] == "accounts"
    # the folder, its source.yaml and its stream files all stand for the folder
    for path in (folder, folder / "source.yaml", folder / "streams" / "campaigns.yaml"):
        assert find_source(str(path)).folder == str(folder)
        assert validate_file(str(path)) == [] and validate_source(str(path)) == []
    monkeypatch.chdir(str(folder / "streams"))  # relative paths, from inside the folder
    assert find_source("campaigns.yaml").folder == ".." and validate_file("campaigns.yaml") == []
    monkeypatch.chdir(str(folder))
    assert [find_source(path).folder for path in (".", "source.yaml", "streams/campaigns.yaml")] == ["."] * 3


def test_a_stream_can_be_named_source(tmp_path):
    """streams/source.yaml is a stream like any other, whichever path stands for its folder."""
    folder = write_folder(tmp_path, files={"accounts.yaml": ACCOUNTS, "source.yaml": CAMPAIGNS})
    streams, stream_file = folder / "streams", folder / "streams" / "source.yaml"
    for path in (folder, streams, stream_file):
        layout = find_source(str(path))
        assert layout.folder == str(folder) and layout.stream_of(str(stream_file)) == "source"
        assert validate_source(str(path)) == []
    assert validate_file(str(stream_file)) == []
    assert [s["name"] for s in load_source(str(streams))["streams"]] == ["accounts", "source"]
    files, issues = validate_paths([str(streams), str(stream_file)])
    assert issues == [] and [os.path.relpath(f, str(folder)) for f in files] == [
        "source.yaml", os.path.join("streams", "accounts.yaml"), os.path.join("streams", "source.yaml")]


def test_folder_findings_point_to_their_file_and_line(tmp_path):
    folder = write_folder(tmp_path, source=FOLDER_SOURCE.replace("    account_ids:", "    extra: {type: string}\n"
                                                                 "    account_ids:"), files={
        "accounts.yaml": ACCOUNTS,
        "campaigns.yaml": CAMPAIGNS.replace("[campaign_id]", "[campaign_id, 7]").replace("from_stream: accounts",
                                                                                         "from_stream: acounts"),
    })
    issues = validate_source(str(folder))
    assert where(issues, folder) == [
        ("source.yaml", 5, "unused-input", "spec.config.extra"),
        ("streams/campaigns.yaml", 2, "unknown-stream", "partitions[0].from_stream"),
        ("streams/campaigns.yaml", 17, "bad-value", "export.campaigns.primary_key[1]"),
    ]
    assert "unknown stream 'acounts' (did you mean 'accounts'?)" in messages(issues)
    assert str(issues[2]).startswith("%s:17:32: error [bad-value] export.campaigns.primary_key[1]: expected a column "
                                     "name" % (folder / "streams" / "campaigns.yaml"))


def test_folder_findings_in_steps_and_request_items_point_to_their_line(tmp_path):
    ads = dedent("""\
        requests:
          - name: raw_ads
            http: {path: /ads}
          - name: raw_stats
            http: {path: /stats, method: get}
            partitions:
              - {name: ad_id, from: ad_rows, field: id}
        transform:
          mode: run
          steps:
            - {name: ads, select: "SELECT record->>'id' AS id FROM raw_ads"}
            - name: ADS
              select: "SELECT '{{ config.account_ids }}' AS ids FROM raw_stats"
        export:
          ads: {step: ads}
    """)
    folder = write_folder(tmp_path, files={"accounts.yaml": ACCOUNTS, "ads.yaml": ads})
    issues = validate_source(str(folder))
    assert where(issues, folder) == [
        ("streams/ads.yaml", 5, "bad-value", "requests[1].http.method"),
        ("streams/ads.yaml", 7, "unknown-source", "requests[1].partitions[0].from"),
        ("streams/ads.yaml", 12, "duplicate-step", "transform.steps[1].name"),
        ("streams/ads.yaml", 13, "bad-value", "transform.steps[1].select"),
    ]
    assert "unknown request partition source 'ad_rows' (did you mean 'ads'?)" in messages(issues)
    assert "step name 'ADS' is used twice (names ignore case)" in messages(issues)


def test_folder_layout_problems(tmp_path):
    folder = write_folder(tmp_path, source=FOLDER_SOURCE + "streams: []\n", files={
        "accounts.yaml": "name: accounts\n" + ACCOUNTS,
        "accounts.yml": ACCOUNTS,
        "bad-name.yaml": ACCOUNTS.replace("  accounts: {step", "  bad_name: {step"),
        "campaigns.yaml": CAMPAIGNS,
        "empty.yaml": "",
        "list.yaml": "- a\n",
        "README.md": "not a stream",
    })
    (folder / "streams" / "more").mkdir()
    issues = validate_source(str(folder))
    assert where(issues, folder) == [
        ("streams/accounts.yml", None, "source-folder", ""),
        ("streams/more", None, "source-folder", ""),
        ("source.yaml", 10, "source-folder", ""),
        ("streams/accounts.yaml", 1, "source-folder", ""),
        ("streams/bad-name.yaml", 1, "bad-value", ""),
        ("streams/empty.yaml", None, "source-folder", ""),
        ("streams/list.yaml", 1, "source-folder", ""),
    ]
    text = messages(issues)
    for message in ("stream 'accounts' already has the file accounts.yaml", "folders inside streams/ are not read",
                    "in a source folder each stream is its own file: move these streams to streams/<name>.yaml",
                    "remove `name`: the file name is the stream name (accounts)",
                    "'bad-name' is not a valid stream name", "the stream file is empty",
                    "a stream file holds one stream (a mapping), got a list"):
        assert message in text
    with pytest.raises(SourceFilesError, match="already has the file accounts.yaml"):
        load_source(str(folder))


def test_a_source_folder_needs_kind_and_stream_files(tmp_path):
    folder = write_folder(tmp_path, files={"README.md": "no streams yet"})
    issues = validate_source(str(folder))
    assert (ERROR, "missing-key") in codes(issues)
    assert "a source folder needs a file for each stream in %s" % (folder / "streams") in messages(issues)
    with pytest.raises(SourceFilesError, match="no stream files"):
        load_source(str(folder))

    (folder / "streams" / "accounts.yaml").write_text(ACCOUNTS)
    (folder / "source.yaml").write_text(FOLDER_SOURCE.replace("kind: source\n", ""))
    issues = validate_source(str(folder))
    assert codes(issues) == [(ERROR, "missing-key")] and "missing `kind: source`" in messages(issues)
    assert validate_source(str(folder), kind="source") == []
    (folder / "source.yaml").write_text(FOLDER_SOURCE.replace("kind: source", "kind: connector"))
    issues = validate_source(str(folder))
    assert codes(issues) == [(ERROR, "bad-value")] and "`kind: source`, got 'connector'" in messages(issues)


def test_yaml_problems_in_stream_files(tmp_path):
    folder = write_folder(tmp_path, files={"accounts.yaml": ACCOUNTS.replace("records: {", "records: &r {"),
                                           "campaigns.yaml": CAMPAIGNS + "x-broken: [\n"})
    issues = validate_source(str(folder))
    assert [(os.path.basename(i.file), i.code) for i in issues] == [("campaigns.yaml", "yaml-syntax")]
    (folder / "streams" / "campaigns.yaml").write_text(CAMPAIGNS)
    issues = validate_source(str(folder))  # anchors belong to their file
    assert where(issues, folder) == [("streams/accounts.yaml", 4, "unused-anchor", "")]


def test_find_source(tmp_path):
    with pytest.raises(SourceFilesError, match="no such file or folder"):
        find_source(str(tmp_path / "missing"))
    with pytest.raises(SourceFilesError, match="is a folder without a source.yaml"):
        find_source(str(tmp_path))
    # a source.yaml without a streams/ folder is a single-file source
    (tmp_path / "source.yaml").write_text(yaml.safe_dump(BASE))
    layout = find_source(str(tmp_path))
    assert layout.folder is None and layout.source_file == str(tmp_path / "source.yaml")
    assert validate_source(str(tmp_path)) == [] and validate_file(str(tmp_path / "source.yaml")) == []

    folder = write_folder(tmp_path)
    (folder / "source.yml").write_text(FOLDER_SOURCE)
    with pytest.raises(SourceFilesError, match="has both source.yaml and source.yml"):
        find_source(str(folder))
    assert codes(validate_source(str(folder))) == [(ERROR, "source-folder")]


def test_old_single_file_paths_name_the_source_folder(tmp_path, capsys):
    folder = write_folder(tmp_path)
    old = str(folder) + ".yaml"  # e.g. examples/sources/ads/google_ads.yaml, from before the source became a folder
    hint = "no such file or folder; did you mean the source folder %s?" % folder
    with pytest.raises(SourceFilesError, match=re.escape(hint)):
        find_source(old)
    assert [i.message for i in validate_source(old)] == [hint]
    assert main([old]) == 1 and hint in capsys.readouterr().out
    assert [i.message for i in validate_source(str(tmp_path / "other.yaml"))] == ["no such file or folder"]


def test_directories_check_each_source_folder_once(tmp_path, capsys):
    folder = write_folder(tmp_path)
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(BASE))
    files, issues = validate_paths([str(tmp_path), str(folder / "streams" / "campaigns.yaml")])
    assert issues == []
    assert [os.path.relpath(f, str(tmp_path)) for f in files] == [
        "other.yaml", "demo/source.yaml", "demo/streams/accounts.yaml", "demo/streams/campaigns.yaml"]
    files, issues = validate_paths([str(folder / "streams")])  # a streams/ folder stands for its source
    assert len(files) == 3 and issues == []
    assert main([str(tmp_path)]) == 0
    assert "4 file(s) checked: 0 error(s), 0 warning(s)" in capsys.readouterr().out


def test_stream_schema_is_for_stream_files():
    jsonschema = pytest.importorskip("jsonschema")
    validate = functools.partial(jsonschema.validate, cls=jsonschema.Draft7Validator)
    for text in (ACCOUNTS, CAMPAIGNS):
        validate(yaml.safe_load(text), schema.stream_json_schema())
    with pytest.raises(jsonschema.ValidationError):
        validate(dict(yaml.safe_load(CAMPAIGNS), name="campaigns"), schema.stream_json_schema())
    with pytest.raises(jsonschema.ValidationError):  # the previous form
        validate(dict(yaml.safe_load(CAMPAIGNS), transform_mode="page"), schema.stream_json_schema())
    validate(yaml.safe_load(FOLDER_SOURCE), schema.json_schema())  # source.yaml of a folder: no streams
