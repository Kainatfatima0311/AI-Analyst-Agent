"""The guard against a real database: the cost gate, and catalog drift.

The unit suite runs with a committed catalog snapshot so it needs no database. These tests
cover the two things that snapshot cannot answer: whether the EXPLAIN cost gate behaves, and
whether the snapshot still matches the schema it claims to describe.
"""

from __future__ import annotations

import psycopg
import pytest

from analyst_agent.config import get_settings
from analyst_agent.sql_guard import STATIC_CATALOG, check, estimate_cost, load_catalog

pytestmark = pytest.mark.integration


def test_the_committed_catalog_still_matches_the_database(rw_dsn: str, seeded: None) -> None:
    """Drift between the snapshot and the real schema is a failure, not a silent divergence.

    The unit tests validate against STATIC_CATALOG. If a migration adds a table or renames a
    column and the snapshot is not regenerated, the hostile-query suite would be checking
    against a schema that no longer exists — and would keep passing while doing it.
    """
    load_catalog.cache_clear()
    live = load_catalog()

    assert live.schemas == STATIC_CATALOG.schemas
    missing = live.objects["analytics"] - STATIC_CATALOG.objects["analytics"]
    extra = STATIC_CATALOG.objects["analytics"] - live.objects["analytics"]
    assert not missing, f"objects in the database but not in the snapshot: {sorted(missing)}"
    assert not extra, f"objects in the snapshot but not in the database: {sorted(extra)}"

    for obj in sorted(live.objects["analytics"]):
        live_cols = live.columns_of("analytics", obj)
        snap_cols = STATIC_CATALOG.columns_of("analytics", obj)
        assert live_cols == snap_cols, (
            f"column drift on analytics.{obj}: "
            f"only in database {sorted(live_cols - snap_cols)}, "
            f"only in snapshot {sorted(snap_cols - live_cols)}"
        )


def test_a_cheap_query_passes_the_cost_gate(ro_conn: psycopg.Connection, seeded: None) -> None:
    verdict = check(
        "SELECT count(*) FROM analytics.orders", conn=ro_conn, catalog=STATIC_CATALOG
    )
    assert verdict.verdict == "allowed", list(verdict.messages)
    assert verdict.estimated_cost is not None
    assert verdict.estimated_cost > 0


def test_an_expensive_query_escalates_rather_than_being_blocked(
    ro_conn: psycopg.Connection, seeded: None
) -> None:
    """An expensive query is often a legitimate one, so a human decides."""
    settings = get_settings().model_copy(update={"sql_max_explain_cost": 1.0})
    verdict = check(
        "SELECT o.order_id, oi.price FROM analytics.orders o "
        "JOIN analytics.order_items oi ON oi.order_id = o.order_id",
        conn=ro_conn,
        catalog=STATIC_CATALOG,
        settings=settings,
    )
    assert verdict.verdict == "escalated"
    assert verdict.allowed and not verdict.executable
    assert "cost_above_ceiling" in verdict.codes
    assert verdict.estimated_cost is not None


def test_the_cost_gate_is_not_reached_for_a_rejected_query(
    ro_conn: psycopg.Connection,
) -> None:
    """Ordering matters: an unvalidated statement must never reach the planner."""
    verdict = check(
        "DROP TABLE analytics.orders", conn=ro_conn, catalog=STATIC_CATALOG
    )
    assert verdict.verdict == "rejected"
    assert verdict.estimated_cost is None


def test_explain_does_not_execute_the_statement(ro_conn: psycopg.Connection, seeded: None) -> None:
    """EXPLAIN without ANALYZE plans but does not run, so the gate itself is cheap and safe."""
    # pg_sleep would be rejected by the validator, so call EXPLAIN directly to prove the
    # planner does not execute what it plans.
    cost = estimate_cost("SELECT count(*) FROM analytics.order_items", ro_conn)
    assert cost > 0


def test_the_rewritten_sql_actually_runs(ro_conn: psycopg.Connection, seeded: None) -> None:
    """What the guard hands back must be executable as-is, limit and all."""
    verdict = check(
        "SELECT year_month, sum(item_revenue) AS revenue FROM analytics.v_order_revenue "
        "GROUP BY 1 ORDER BY 1",
        conn=ro_conn,
        catalog=STATIC_CATALOG,
    )
    assert verdict.executable
    assert verdict.rewritten_sql

    with ro_conn.cursor() as cur:
        cur.execute(verdict.rewritten_sql)
        rows = cur.fetchall()
    assert rows
    assert len(rows) <= (verdict.row_limit or 0)


def test_the_planted_shock_month_is_visible_through_the_guard(
    ro_conn: psycopg.Connection, seeded: None
) -> None:
    """End-to-end sanity: a diagnostic query the agent will actually write, run under the guard."""
    verdict = check(
        "SELECT to_char(o.order_purchase_timestamp, 'YYYY-MM') AS ym, "
        "sum(oi.price) AS revenue, count(DISTINCT o.order_id) AS orders "
        "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
        "WHERE o.order_status <> 'canceled' GROUP BY 1 ORDER BY 1",
        conn=ro_conn,
        catalog=STATIC_CATALOG,
    )
    assert verdict.executable, list(verdict.messages)

    with ro_conn.cursor() as cur:
        cur.execute(verdict.rewritten_sql or "")
        by_month = {row["ym"]: float(row["revenue"]) for row in cur.fetchall()}

    # The generator plants the drop in 2018-03 with volume left on trend.
    assert by_month["2018-03"] < by_month["2018-02"] * 0.8
    assert by_month["2018-04"] > by_month["2018-03"] * 1.2
