import datetime
from decimal import Decimal

import pytest

pytest.importorskip("adapt.connectors.google_ads.gaql")

from adapt.connectors.google_ads.gaql import GaqlBuilder, OPERATORS, check, gaql  # noqa: E402
from adapt.core.runtime import components  # noqa: E402
from adapt.core.engine.queries import QueryError  # noqa: E402
from adapt.core.runtime.templates import render  # noqa: E402

TODAY = datetime.date(2026, 10, 3)


def test_gaql_is_an_installed_query_builder():
    assert isinstance(components.load("gaql", kind="query builder"), GaqlBuilder)


def test_gaql_follows_the_grammar():
    query = gaql({"select": ["campaign.id", "metrics.cost_micros"], "from": "campaign", "where": [
        {"field": "segments.date", "op": "BETWEEN", "type": "date", "value": [TODAY, "2026-10-04"]},
        {"field": "campaign.status", "op": "in", "type": "enum", "value": ["ENABLED", "PAUSED"]},
        {"field": "campaign.name", "op": "LIKE", "type": "string", "value": "O'Brien \\ \"x\"\n%"},
        {"field": "campaign.id", "op": "NOT  IN", "type": "int", "value": ["1", 2, 3.0]},
        {"field": "campaign.end_date", "op": "IS NULL", "type": "date"},
        {"field": "campaign.labels", "op": "CONTAINS ANY", "type": "string", "value": None, "skip_if_empty": True},
        {"field": "segments.date", "op": "BETWEEN", "type": "date", "value": [None, TODAY], "skip_if_empty": True},
    ], "order_by": ["metrics.cost_micros desc", "campaign.id"], "limit": 50})
    assert query == ("SELECT campaign.id, metrics.cost_micros FROM campaign WHERE segments.date BETWEEN "
                     "'2026-10-03' AND '2026-10-04' AND campaign.status IN (ENABLED, PAUSED) AND campaign.name LIKE "
                     "'O\\'Brien \\\\ \"x\"\\n%' AND campaign.id NOT IN (1, 2, 3) AND campaign.end_date IS NULL "
                     "ORDER BY metrics.cost_micros DESC, campaign.id LIMIT 50")


@pytest.mark.parametrize("spec,problem", [
    ({"select": ["campaign.id FROM x --"], "from": "campaign"}, "not a valid field name"),
    ({"select": ["campaign.id"], "from": "campaign WHERE 1"}, "not a valid resource name"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "=", "type": "int", "value": "1 OR 1"}]},
     "expects a whole number"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "=", "type": "int", "value": True}]},
     "expects a whole number"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "=", "type": "enum", "value": "A) OR (B"}]},
     "expects an enum value"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "=", "type": "date", "value": "soon"}]},
     "invalid date"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "=", "type": "string", "value": None}]},
     "has an empty value"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "IN", "type": "string", "value": []}]},
     "needs at least one value"),
    ({"select": ["a"], "from": "c", "where": [{"field": "a", "op": "= 1 OR a =", "type": "int", "value": 1}]},
     "not a supported operator"),
    ({"select": ["a"], "from": "c", "order_by": "a; DROP"}, "not a valid ordering"),
    ({"select": ["a"], "from": "c", "order_by": ["a;b desc"]}, "not a valid field name"),
    ({"select": ["a"], "from": "c", "limit": 0}, "positive whole number"),
])
def test_gaql_rejects_unsafe_input(spec, problem):
    with pytest.raises(QueryError, match=problem):
        gaql(spec)


def test_a_batched_partition_list_renders_in_like_a_config_list():
    """A request partition with `batch_size` holds a list: a sole `{{ partition.ids }}` reference stays one."""
    def query(op, value, scopes):
        return gaql(render({"select": ["ad_group.id"], "from": "ad_group", "where": [
            {"field": "campaign.id", "op": op, "type": "int", "value": value}]}, scopes))
    ids = ["10", 11, Decimal("12")]
    batched = query("IN", "{{ partition.ids }}", {"partition": {"customer_id": "1", "ids": ids}})
    assert batched == query("IN", "{{ config.ids }}", {"config": {"ids": ids}}) == query("IN", ids, {})
    assert batched == "SELECT ad_group.id FROM ad_group WHERE campaign.id IN (10, 11, 12)"
    assert query("NOT IN", "{{ partition.ids }}", {"partition": {"ids": ["7"]}}).endswith("campaign.id NOT IN (7)")
    with pytest.raises(QueryError, match="expects a whole number"):  # a list where one value goes
        query("=", "{{ partition.ids }}", {"partition": {"ids": ids}})


def test_checks_before_a_run_allow_references():
    assert check({"select": ["campaign.id", "{{ config.extra_field }}"], "from": "campaign", "where": [
        {"field": "segments.date", "op": "BETWEEN", "type": "date",
         "value": ["{{ window.start }}", "{{ window.end }}"]},
        {"field": "campaign.id", "op": "in", "type": "int", "value": "{{ config.ids }}", "skip_if_empty": True},
        {"field": "campaign.end_date", "op": "IS NULL", "type": "date"}], "limit": "{{ config.limit }}"}) == []


def test_checks_before_a_run():
    problems = check({"select": [], "from": "campaign WHERE 1", "order": ["campaign.id"], "limit": 0, "where": [
        {"field": "segments.date", "op": "BETWEEN", "type": "date", "value": ["2025-01-01"]},
        {"field": "campaign.id", "op": "IN", "type": "int", "value": 5},
        {"field": "campaign.name", "op": "IS NULL", "type": "string", "value": "x"},
        {"field": "campaign.id", "op": "BETWEN", "type": "integer", "value": 1},
        {"field": None, "op": "=", "type": "int", "value": 1},
        {"field": "campaign.id", "op": "=", "type": "int"},
        {"field": "campaign.id", "op": "=", "type": "int", "value": 1, "skip_if_empty": "yes", "extra": 1},
        "campaign.id = 1"]})
    assert problems == [
        (("order",), "unknown key 'order' (did you mean 'order_by'?); `gaql` does not support it"),
        (("select",), "`select` must be a non-empty list of fields"),
        (("from",), "'campaign WHERE 1' is not a resource name"),
        (("where", 0, "value"), "`BETWEEN` needs a list of two values"),
        (("where", 1, "value"), "`IN` needs a list or a list reference"),
        (("where", 2, "value"), "`IS NULL` takes no value"),
        (("where", 3, "op"), "unknown operator 'BETWEN' (did you mean 'BETWEEN'?); one of: %s" % ", ".join(OPERATORS)),
        (("where", 3, "type"), "unknown value type 'integer' (did you mean 'int'?); one of: int, string, enum, date"),
        (("where", 4, "field"), "None is not a field name"),
        (("where", 5), "`=` needs a `value`"),
        (("where", 6, "extra"), "unknown key 'extra'; a where item does not support it"),
        (("where", 6, "skip_if_empty"), "`skip_if_empty` must be true or false"),
        (("where", 7), "a where item must be a mapping with `field`, `op` and `type`"),
        (("limit",), "`limit` must be a positive whole number, got 0"),
    ]
    assert check("SELECT campaign.id FROM campaign") == [((), "`gaql` must be a mapping with `select` and `from`")]
    assert check({}) == [((), "`gaql` needs `select`"), ((), "`gaql` needs `from`")]
