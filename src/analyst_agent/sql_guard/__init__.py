"""The SQL safety layer.

``check`` is the only entry point the rest of the system uses. It runs the layers in order, and
the order is the point:

1. **Static validation** (``validator.validate``) — parse, one statement, SELECT root, no denied
   node anywhere in the tree, no denied function, every object allow-listed, no unconstrained
   cartesian product, and a clamped or injected LIMIT.
2. **Column policy** (``column_policy``) — restricted columns escalate rather than being
   silently stripped.
3. **Cost gate** (``explain_gate``) — only for statements the first two layers cleared, and only
   when a connection is supplied.

Below all of it sits the layer that holds if every one of these fails: the ``analyst_ro``
Postgres role, which physically cannot write (control C1).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg

from analyst_agent.config import Settings, get_settings
from analyst_agent.sql_guard.catalog import STATIC_CATALOG, Catalog, load_catalog
from analyst_agent.sql_guard.errors import GuardError, GuardVerdict, Reason, Verdict
from analyst_agent.sql_guard.explain_gate import estimate_cost, gate
from analyst_agent.sql_guard.validator import validate

__all__ = [
    "STATIC_CATALOG",
    "Catalog",
    "GuardError",
    "GuardVerdict",
    "Reason",
    "Verdict",
    "check",
    "estimate_cost",
    "load_catalog",
    "validate",
]


def check(
    sql: str,
    *,
    conn: psycopg.Connection[Any] | None = None,
    catalog: Catalog | None = None,
    settings: Settings | None = None,
    row_limit: int | None = None,
) -> GuardVerdict:
    """Validate a statement, and cost-gate it when a connection is available.

    Pass ``conn`` (a read-only connection) to include the EXPLAIN gate. Without it the verdict
    is static only — which is what the unit test suite uses, so the security regression net
    runs with no database.
    """
    settings = settings or get_settings()
    verdict = validate(sql, catalog=catalog, settings=settings, row_limit=row_limit)

    if conn is None or not verdict.allowed:
        return verdict

    target = verdict.rewritten_sql or sql
    cost, reasons = gate(target, conn, settings)
    if not reasons:
        return replace(verdict, estimated_cost=cost)

    return replace(
        verdict,
        estimated_cost=cost,
        requires_approval=True,
        reasons=(*verdict.reasons, *reasons),
    )
