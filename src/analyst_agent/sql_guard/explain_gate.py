"""Control C5: ask the planner what a query will cost before letting it run.

``EXPLAIN`` without ``ANALYZE`` plans the statement but does not execute it, so this is a cheap
question to ask. A plan above the configured ceiling is **escalated**, not rejected: an
expensive query is often a legitimate one, and the right answer is a human deciding rather than
the agent being silently blocked.

Only statements the static validator has already allowed are explained. That ordering matters —
handing an unvalidated statement to the planner would mean parsing it with Postgres rather than
with our own rules.
"""

from __future__ import annotations

from typing import Any

import psycopg

from analyst_agent.config import Settings, get_settings
from analyst_agent.observability.logging import get_logger
from analyst_agent.sql_guard.errors import Reason

log = get_logger(__name__)


def _total_cost(plan: Any) -> float:
    """Pull the top-level total cost out of an EXPLAIN (FORMAT JSON) result."""
    if isinstance(plan, list) and plan:
        plan = plan[0]
    if isinstance(plan, dict):
        node = plan.get("Plan", plan)
        if isinstance(node, dict):
            value = node.get("Total Cost")
            if isinstance(value, (int, float)):
                return float(value)
    raise ValueError("EXPLAIN output did not contain a Total Cost")


def estimate_cost(sql: str, conn: psycopg.Connection[Any]) -> float:
    """Return the planner's estimated total cost for an already-validated statement."""
    with conn.cursor() as cur:
        # Interpolation is safe here and unavoidable: EXPLAIN takes a statement, not a
        # parameter, and this statement has already been parsed and allowed by the validator.
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        row = cur.fetchone()
    if row is None:
        raise ValueError("EXPLAIN returned no rows")
    # row_factory is dict_row on our pools, but be tolerant of a tuple cursor too.
    plan = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return _total_cost(plan)


def gate(
    sql: str, conn: psycopg.Connection[Any], settings: Settings | None = None
) -> tuple[float | None, list[Reason]]:
    """Estimate the cost and decide whether a human should see it first.

    A failure to explain is itself a reason to escalate rather than to proceed: if the planner
    cannot make sense of the statement, running it is not the safe default.
    """
    settings = settings or get_settings()
    ceiling = settings.sql_max_explain_cost

    try:
        cost = estimate_cost(sql, conn)
    except (psycopg.Error, ValueError) as exc:
        log.warning("explain failed", error=str(exc), sql=sql)
        return None, [
            Reason(
                "explain_failed",
                "the query plan could not be estimated, so it needs review before running",
                str(exc).split("\n")[0],
            )
        ]

    if cost > ceiling:
        return cost, [
            Reason(
                "cost_above_ceiling",
                f"estimated plan cost {cost:,.0f} exceeds the ceiling of {ceiling:,.0f}",
                "narrow the date range, aggregate earlier, or ask for approval",
            )
        ]

    log.debug("explain within ceiling", estimated_cost=cost, ceiling=ceiling)
    return cost, []
