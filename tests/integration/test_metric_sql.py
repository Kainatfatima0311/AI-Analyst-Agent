"""Every approved metric, executed against the real database and run through the guard.

Two properties are asserted for all twelve, not a sample:

* **It runs, and returns a usable number.** A definition that no longer executes is worse than a
  missing one — it looks approved while being broken.
* **It passes ``sql_guard``.** The metric layer and the safety layer have to agree, or the
  registry would be producing statements the runner then refuses. This is the seam where the
  two halves of the project meet.
"""

from __future__ import annotations

import psycopg
import pytest

from analyst_agent.config import get_settings
from analyst_agent.metrics.registry import MetricRegistry, get_registry
from analyst_agent.sql_guard import STATIC_CATALOG, validate

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def registry() -> MetricRegistry:
    return get_registry()


def _metric_names() -> list[str]:
    return list(get_registry().names)


@pytest.mark.parametrize("name", _metric_names())
def test_every_metric_passes_the_guard(registry: MetricRegistry, name: str) -> None:
    definition = registry.get(name)
    dimensions = list(definition.dimensions)[:1]
    rendered = registry.render(
        name, dimensions=dimensions, date_from="2018-01-01", date_to="2018-09-01"
    )
    verdict = validate(rendered.sql, catalog=STATIC_CATALOG, settings=get_settings())
    assert verdict.verdict == "allowed", (
        f"{name} produced SQL the guard would not run: {list(verdict.messages)}"
    )


@pytest.mark.parametrize("name", _metric_names())
def test_every_metric_executes_and_returns_a_number(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None, name: str
) -> None:
    rendered = registry.render(name)
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        rows = cur.fetchall()

    assert rows, f"{name} returned no rows over the whole dataset"
    value = rows[0][name] if name in rows[0] else next(iter(rows[0].values()))
    assert value is not None, f"{name} returned null over the whole dataset"
    assert float(value) == float(value)  # not NaN


@pytest.mark.parametrize("name", _metric_names())
def test_every_metric_executes_with_a_date_window(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None, name: str
) -> None:
    rendered = registry.render(name, date_from="2018-01-01", date_to="2018-04-01")
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        assert cur.fetchall(), f"{name} returned no rows for 2018 Q1"


@pytest.mark.parametrize("name", _metric_names())
def test_every_declared_dimension_actually_works(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None, name: str
) -> None:
    """A dimension whose join or expression is wrong would only surface mid-run otherwise."""
    definition = registry.get(name)
    for dimension in definition.dimensions:
        rendered = registry.render(name, dimensions=[dimension])
        with ro_conn.cursor() as cur:
            cur.execute(rendered.sql, rendered.params)
            rows = cur.fetchall()
        assert rows, f"{name} by {dimension} returned nothing"
        assert dimension in rows[0], f"{name} by {dimension} did not label the column"


def test_revenue_reproduces_the_planted_shock_month(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None
) -> None:
    """The approved metric must see what the generator planted, or the eval ground truth is
    measuring something other than what the agent will measure."""
    rendered = registry.render("revenue", dimensions=["month"])
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        by_month = {row["month"]: float(row["revenue"]) for row in cur.fetchall()}

    assert by_month["2018-03"] < by_month["2018-02"] * 0.8
    assert by_month["2018-04"] > by_month["2018-03"] * 1.2


def test_the_category_mix_shift_is_visible_through_the_metric_layer(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None
) -> None:
    """One of the two planted causes, reached only through approved definitions."""
    premium = {"relogios_presentes", "informatica_acessorios", "eletrodomesticos"}

    def premium_share(month: str) -> float:
        rendered = registry.render(
            "revenue", dimensions=["product_category"], dimension_filters={"month": month}
        )
        with ro_conn.cursor() as cur:
            cur.execute(rendered.sql, rendered.params)
            rows = cur.fetchall()
        total = sum(float(r["revenue"]) for r in rows)
        top = sum(float(r["revenue"]) for r in rows if r["product_category"] in premium)
        return top / total

    assert premium_share("2018-03") < premium_share("2018-02") * 0.7


def test_the_cancellation_spike_is_visible_through_the_metric_layer(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None
) -> None:
    """The other planted cause. Note this metric deliberately keeps cancelled orders in."""
    rendered = registry.render("cancellation_rate", dimensions=["month"])
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        by_month = {row["month"]: float(row["cancellation_rate"]) for row in cur.fetchall()}

    assert by_month["2018-03"] > by_month["2018-02"] * 3


def test_the_decoy_also_moves_which_is_why_it_needs_refuting(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None
) -> None:
    """Review scores drop in the shock month too. They are a consequence of the delays, not a
    cause of the revenue drop — an agent that stops at this correlation is wrong, so the data
    must genuinely contain the trap."""
    rendered = registry.render("avg_review_score", dimensions=["month"])
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        by_month = {row["month"]: float(row["avg_review_score"]) for row in cur.fetchall()}

    assert by_month["2018-03"] < by_month["2018-02"]


def test_a_bound_filter_value_cannot_inject_sql(
    registry: MetricRegistry, ro_conn: psycopg.Connection, seeded: None
) -> None:
    """End-to-end proof of the parameterisation: a hostile value returns no rows, not damage."""
    rendered = registry.render(
        "revenue", dimension_filters={"month": "2018-03'; DROP TABLE analytics.orders; --"}
    )
    with ro_conn.cursor() as cur:
        cur.execute(rendered.sql, rendered.params)
        rows = cur.fetchall()
    assert float(rows[0]["revenue"] or 0) == 0

    # The table is still there.
    with ro_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM analytics.orders")
        assert int(cur.fetchone()["n"]) > 0
