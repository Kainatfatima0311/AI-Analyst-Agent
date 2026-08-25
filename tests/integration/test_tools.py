"""The five tools, against the real database.

The chain these tests care about: metric_lookup resolves a definition, schema_inspector describes
the tables, sql_runner validates and executes, python_analysis and chart_builder work on that
result by ``query_id``. Every step is audited, so the last assertion is always that the trace can
account for what happened.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from analyst_agent.db import repository as repo
from analyst_agent.tools.frames import get_store, reset_store
from analyst_agent.tools.registry import ToolRegistry, get_tool_registry

pytestmark = pytest.mark.integration

Q = "'"
MONTHLY_REVENUE_SQL = (
    f"SELECT to_char(o.order_purchase_timestamp, {Q}YYYY-MM{Q}) AS ym, "
    "sum(oi.price) AS revenue, count(DISTINCT o.order_id) AS orders "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    f"WHERE o.order_status <> {Q}canceled{Q} GROUP BY 1 ORDER BY 1"
)


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return get_tool_registry()


@pytest.fixture
def run_id(rw_dsn: str):
    rid = repo.create_run("tool integration", requested_by="pytest")
    yield rid
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (rid,))


@pytest.fixture
def revenue_query(registry: ToolRegistry, run_id: uuid.UUID, seeded: None) -> str:
    result = registry.invoke(
        "sql_runner",
        {"sql": MONTHLY_REVENUE_SQL, "purpose": "monthly net revenue", "row_limit": None},
        run_id,
    )
    assert result.ok and not result.refused, result.summary
    return str(result.data["query_id"])


# --- metric_lookup ----------------------------------------------------------


def test_metric_lookup_resolves_a_business_term(registry: ToolRegistry, run_id: uuid.UUID) -> None:
    result = registry.invoke("metric_lookup", {"term": "AOV", "include_all": None}, run_id)
    assert result.ok and not result.refused
    assert result.data["metric"] == "aov"
    assert result.data["definition_version"] == "aov@v1"
    assert result.data["caveats"]


def test_metric_lookup_refuses_an_unapproved_term(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke(
        "metric_lookup", {"term": "customer lifetime value", "include_all": None}, run_id
    )
    assert result.refused
    assert result.data["approved_metrics"]
    assert "ad-hoc" in result.data["guidance"]

    call = repo.get_trace(run_id)["tool_calls"][0]
    assert call["ok"] is True, "a refusal is a result, not an error"
    assert call["refusal"]


def test_metric_lookup_can_list_the_whole_catalogue(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke("metric_lookup", {"term": "", "include_all": True}, run_id)
    assert len(result.data["metrics"]) >= 12


# --- schema_inspector -------------------------------------------------------


def test_schema_inspector_describes_a_table_with_its_joins(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    result = registry.invoke(
        "schema_inspector", {"tables": ["order_items"], "include_samples": None}, run_id
    )
    assert result.ok
    obj = result.data["objects"][0]
    assert obj["name"] == "analytics.order_items"
    assert obj["estimated_rows"] and obj["estimated_rows"] > 0
    targets = {fk["references_table"] for fk in obj["foreign_keys"]}
    assert {"analytics.orders", "analytics.products", "analytics.sellers"} <= targets


def test_schema_inspector_flags_restricted_columns_and_withholds_their_values(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    result = registry.invoke(
        "schema_inspector", {"tables": ["customer_contact"], "include_samples": True}, run_id
    )
    columns = {c["name"]: c for c in result.data["objects"][0]["columns"]}
    for name in ("full_name", "email", "phone", "street_address"):
        assert columns[name]["restricted"] is True
        assert "sample_values" not in columns[name], "restricted columns must not leak values"
        assert columns[name]["usage"], "the agent needs to know the terms of use"


def test_schema_inspector_offers_samples_for_categorical_columns_only(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    result = registry.invoke(
        "schema_inspector", {"tables": ["orders"], "include_samples": True}, run_id
    )
    columns = {c["name"]: c for c in result.data["objects"][0]["columns"]}
    assert "sample_values" in columns["order_status"]
    assert "delivered" in columns["order_status"]["sample_values"]
    # A high-cardinality key is not a category, so it is not sampled.
    assert "sample_values" not in columns["order_id"]


def test_schema_inspector_refuses_an_unknown_table(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke(
        "schema_inspector", {"tables": ["nonexistent"], "include_samples": None}, run_id
    )
    assert result.refused
    assert result.data["available"]


# --- sql_runner -------------------------------------------------------------


def test_sql_runner_executes_and_previews(registry: ToolRegistry, run_id: uuid.UUID, seeded: None) -> None:
    result = registry.invoke(
        "sql_runner",
        {"sql": MONTHLY_REVENUE_SQL, "purpose": "monthly net revenue", "row_limit": None},
        run_id,
    )
    assert result.ok and not result.refused
    assert result.data["row_count"] == 24
    assert result.data["columns"] == ["ym", "revenue", "orders"]
    assert result.data["rows"][0]["ym"] == "2016-09"
    # Values arrive JSON-safe, not as Decimal or numpy reprs the model would misread.
    assert isinstance(result.data["rows"][0]["revenue"], (int, float))


def test_sql_runner_previews_rather_than_dumping_every_row(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    """Five thousand rows in the context would be expensive and would invite eyeballing."""
    result = registry.invoke(
        "sql_runner",
        {"sql": "SELECT order_id FROM analytics.orders", "purpose": "many rows", "row_limit": 500},
        run_id,
    )
    assert result.data["row_count"] == 500
    assert result.data["preview_rows"] == 50
    assert len(result.data["rows"]) == 50


def test_sql_runner_refuses_a_hostile_statement_and_records_the_attempt(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke(
        "sql_runner",
        {"sql": "DROP TABLE analytics.orders", "purpose": "hostile", "row_limit": None},
        run_id,
    )
    assert result.refused
    assert result.data["verdict"] == "rejected"
    assert "not_a_select" in " ".join(result.data["reasons"])

    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_rejected"] == 1
    assert trace["summary"]["queries_executed"] == 0


def test_sql_runner_escalates_a_restricted_column_without_running_it(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke(
        "sql_runner",
        {
            "sql": "SELECT email FROM analytics.customer_contact",
            "purpose": "who are our customers",
            "row_limit": None,
        },
        run_id,
    )
    assert result.refused
    assert result.data["verdict"] == "escalated"
    assert "analytics.customer_contact.email" in result.data["sensitive_columns"]
    assert "workaround" in result.data["guidance"]

    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_escalated"] == 1
    assert trace["summary"]["queries_executed"] == 0


def test_sql_runner_treats_an_empty_result_as_a_finding(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    result = registry.invoke(
        "sql_runner",
        {
            "sql": f"SELECT order_id FROM analytics.orders WHERE order_status = {Q}nope{Q}",
            "purpose": "impossible filter",
            "row_limit": None,
        },
        run_id,
    )
    assert result.ok and not result.refused
    assert result.data["empty"] is True
    assert "filter" in result.summary


def test_sql_runner_reports_truncation(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    result = registry.invoke(
        "sql_runner",
        {"sql": "SELECT order_id FROM analytics.orders", "purpose": "capped", "row_limit": 10},
        run_id,
    )
    assert result.data["truncated"] is True
    assert "aggregate" in result.summary, "the model must be told not to reason over a sample"


# --- python_analysis --------------------------------------------------------


def test_python_analysis_finds_the_planted_shock_month(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    """The tool has to surface the drop the generator planted, unprompted."""
    result = registry.invoke(
        "python_analysis",
        {
            "query_id": revenue_query,
            "operation": "period_over_period",
            "value": "revenue",
            "order_column": "ym",
            "by": None,
            "agg": None,
            "window": None,
            "columns": None,
            "top_n": None,
        },
        run_id,
    )
    assert result.ok and not result.refused
    rows = {row["ym"]: row for row in result.data["rows"]}
    assert rows["2018-03"]["pct_change"] < -0.25


def test_python_analysis_warns_that_correlation_is_not_cause(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    """The summary is the only thing the model reliably reads, so the caveat lives there."""
    result = registry.invoke(
        "python_analysis",
        {
            "query_id": revenue_query,
            "operation": "correlation",
            "columns": ["revenue", "orders"],
            "by": None,
            "value": None,
            "agg": None,
            "order_column": None,
            "window": None,
            "top_n": None,
        },
        run_id,
    )
    assert result.ok
    assert "refute" in result.summary.lower()


def test_python_analysis_refuses_an_unknown_column(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    result = registry.invoke(
        "python_analysis",
        {
            "query_id": revenue_query,
            "operation": "group_by",
            "by": ["nonexistent"],
            "value": "revenue",
            "agg": None,
            "order_column": None,
            "window": None,
            "columns": None,
            "top_n": None,
        },
        run_id,
    )
    assert result.refused
    assert "ym" in result.data["available_columns"]


def test_python_analysis_refuses_an_unknown_query_id(
    registry: ToolRegistry, run_id: uuid.UUID
) -> None:
    result = registry.invoke(
        "python_analysis",
        {
            "query_id": str(uuid.uuid4()),
            "operation": "describe",
            "by": None,
            "value": None,
            "agg": None,
            "order_column": None,
            "window": None,
            "columns": None,
            "top_n": None,
        },
        run_id,
    )
    assert result.refused
    assert "sql_runner" in result.data["guidance"]


def test_numeric_columns_reach_the_model_as_numbers(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    """Postgres returns every `numeric` column as a Decimal.

    Before that was handled explicitly, monetary figures arrived as the *string* "139184.93",
    which leaves the model either comparing numbers lexically or spending a turn parsing them.
    """
    result = registry.invoke(
        "sql_runner",
        {
            "sql": "SELECT sum(oi.price) AS revenue, count(*) AS lines, "
            "min(o.order_purchase_timestamp) AS first_order "
            "FROM analytics.orders o JOIN analytics.order_items oi "
            "ON oi.order_id = o.order_id",
            "purpose": "type coercion check",
            "row_limit": None,
        },
        run_id,
    )
    row = result.data["rows"][0]
    assert isinstance(row["revenue"], float)
    assert isinstance(row["lines"], int)
    assert isinstance(row["first_order"], str), "timestamps arrive ISO-formatted"


# --- frame reuse and rehydration -------------------------------------------


def test_a_frame_survives_eviction_by_being_rebuilt_from_the_audit(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    """A run must survive a restart, so a lost frame is rebuilt rather than fatal."""
    reset_store()
    assert len(get_store()) == 0

    result = registry.invoke(
        "python_analysis",
        {
            "query_id": revenue_query,
            "operation": "describe",
            "by": None,
            "value": None,
            "agg": None,
            "order_column": None,
            "window": None,
            "columns": None,
            "top_n": None,
        },
        run_id,
    )
    assert result.ok and not result.refused, result.summary
    assert len(get_store()) == 1


def test_a_rejected_query_cannot_be_rehydrated(run_id: uuid.UUID) -> None:
    """Rehydration must not become a route to run something the guard refused."""
    from analyst_agent.tools.frames import FrameNotAvailableError

    query_id = repo.record_sql_audit(
        run_id, purpose="blocked", sql_text="DROP TABLE analytics.orders", verdict="rejected"
    )
    reset_store()
    with pytest.raises(FrameNotAvailableError, match="cannot be rebuilt"):
        get_store().get(query_id)


# --- chart_builder ----------------------------------------------------------


def test_chart_builder_records_a_chart_against_its_query(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    """The chart carries its query_id, which is what makes a picture one click from its SQL."""
    result = registry.invoke(
        "chart_builder",
        {
            "query_id": revenue_query,
            "chart_type": "line",
            "x": "ym",
            "y": "revenue",
            "series": None,
            "title": "Net revenue by month",
        },
        run_id,
    )
    assert result.ok and not result.refused, result.summary

    charts = repo.get_trace(run_id)["charts"]
    assert len(charts) == 1
    assert str(charts[0]["query_id"]) == revenue_query
    assert charts[0]["spec"]["data"], "the stored spec must be renderable"
    # With one series the title already names it, so a legend box would be noise.
    assert charts[0]["spec"]["layout"]["showlegend"] is False


def test_too_many_series_fold_into_other_and_say_so(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    """A silent cap reads as 'this is everything' when it is not."""
    result = registry.invoke(
        "sql_runner",
        {
            "sql": f"SELECT to_char(o.order_purchase_timestamp, {Q}YYYY-MM{Q}) AS ym, "
            "p.product_category_name AS category, sum(oi.price) AS revenue "
            "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
            "JOIN analytics.products p ON p.product_id = oi.product_id "
            f"WHERE o.order_status <> {Q}canceled{Q} GROUP BY 1, 2",
            "purpose": "revenue by month and category",
            "row_limit": None,
        },
        run_id,
    )
    chart = registry.invoke(
        "chart_builder",
        {
            "query_id": str(result.data["query_id"]),
            "chart_type": "line",
            "x": "ym",
            "y": "revenue",
            "series": "category",
            "title": "Revenue by category",
        },
        run_id,
    )
    assert chart.ok
    assert chart.data["series_folded"] == 2, "ten categories, eight slots"
    assert chart.data["series"][-1] == "Other"
    assert "folded" in chart.summary, "the fold must be reported, not silent"


def test_scatter_caps_at_three_series(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    """Scatter compares every pair of colours at once, so the palette caps lower there."""
    result = registry.invoke(
        "sql_runner",
        {
            "sql": "SELECT p.product_category_name AS category, oi.price AS price, "
            "oi.freight_value AS freight FROM analytics.order_items oi "
            "JOIN analytics.products p ON p.product_id = oi.product_id",
            "purpose": "price against freight by category",
            "row_limit": 2000,
        },
        run_id,
    )
    chart = registry.invoke(
        "chart_builder",
        {
            "query_id": str(result.data["query_id"]),
            "chart_type": "scatter",
            "x": "price",
            "y": "freight",
            "series": "category",
            "title": "Freight against price",
        },
        run_id,
    )
    assert chart.ok
    assert len(chart.data["series"]) == 4, "three slots plus Other"
    assert "capped" in chart.summary


def test_chart_builder_refuses_a_text_column_on_the_value_axis(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    result = registry.invoke(
        "chart_builder",
        {
            "query_id": revenue_query,
            "chart_type": "line",
            "x": "revenue",
            "y": "ym",
            "series": None,
            "title": "nonsense",
        },
        run_id,
    )
    assert result.refused
    assert "numeric" in result.summary


def test_chart_builder_refuses_a_categorical_scatter_axis(
    registry: ToolRegistry, run_id: uuid.UUID, revenue_query: str
) -> None:
    result = registry.invoke(
        "chart_builder",
        {
            "query_id": revenue_query,
            "chart_type": "scatter",
            "x": "ym",
            "y": "revenue",
            "series": None,
            "title": "nonsense",
        },
        run_id,
    )
    assert result.refused
    assert "bar" in result.data["guidance"], "a refusal should suggest what would work"


def test_the_whole_chain_is_accounted_for_in_the_trace(
    registry: ToolRegistry, run_id: uuid.UUID, seeded: None
) -> None:
    """metric_lookup, then sql_runner, then python_analysis, then chart_builder - all audited."""
    registry.invoke("metric_lookup", {"term": "revenue", "include_all": None}, run_id)
    query = registry.invoke(
        "sql_runner",
        {"sql": MONTHLY_REVENUE_SQL, "purpose": "monthly net revenue", "row_limit": None},
        run_id,
    )
    query_id = str(query.data["query_id"])
    registry.invoke(
        "python_analysis",
        {
            "query_id": query_id,
            "operation": "period_over_period",
            "value": "revenue",
            "order_column": "ym",
            "by": None,
            "agg": None,
            "window": None,
            "columns": None,
            "top_n": None,
        },
        run_id,
    )
    registry.invoke(
        "chart_builder",
        {
            "query_id": query_id,
            "chart_type": "line",
            "x": "ym",
            "y": "revenue",
            "series": None,
            "title": "Net revenue by month",
        },
        run_id,
    )

    trace = repo.get_trace(run_id)
    assert [c["tool"] for c in trace["tool_calls"]] == [
        "metric_lookup",
        "sql_runner",
        "python_analysis",
        "chart_builder",
    ]
    assert trace["summary"]["queries_executed"] == 1
    assert len(trace["charts"]) == 1
    # Every tool call recorded a duration, so a slow step is visible in the trace.
    assert all(c["duration_ms"] is not None for c in trace["tool_calls"])
