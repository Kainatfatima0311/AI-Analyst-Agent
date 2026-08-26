"""Tool 3: execute an analytical query, and only ever a validated one.

The order of operations is the whole point:

1. Audit the statement **before** deciding anything, so a rejected attempt is recorded rather
   than vanishing.
2. Run ``sql_guard.check`` - static validation, column policy, then the EXPLAIN cost gate on a
   read-only connection.
3. Execute only if the verdict is ``allowed``. A ``rejected`` verdict returns a refusal; an
   ``escalated`` one returns a refusal naming the pending approval, and the graph pauses.
4. Record the outcome against the same ``query_id``, and keep the frame for follow-up analysis.

``purpose`` is a required argument. It costs the model one sentence, and it means a reviewer
reading ``sql_audit`` can see *why* each statement ran, not only what it was.

The model gets a **preview** of the rows, not all of them. The full frame stays in the frame
store for ``python_analysis`` and ``chart_builder``. Sending five thousand rows into the context
would be expensive and would invite the model to eyeball data instead of aggregating it.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from decimal import Decimal

import pandas as pd
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from analyst_agent.config import get_settings
from analyst_agent.db import repository as repo
from analyst_agent.db.engine import ro_conn
from analyst_agent.observability.logging import get_logger
from analyst_agent.sql_guard import check, load_catalog
from analyst_agent.tools.base import Tool, ToolResult
from analyst_agent.tools.frames import FrameMeta, get_store

log = get_logger(__name__)

PREVIEW_ROWS = 50


class SqlRunnerInput(BaseModel):
    model_config = {"extra": "forbid"}

    sql: str = Field(
        description=(
            "A single SELECT statement against the analytics schema. No DDL, no DML, no "
            "semicolon-separated statements, no information_schema or pg_catalog."
        )
    )
    purpose: str = Field(
        description=(
            "One sentence on what this query is for and what you expect it to show. It is "
            "recorded in the audit trail, so write it for a reviewer rather than for yourself."
        )
    )
    row_limit: int | None = Field(
        default=None,
        description=(
            "Optional row cap. A LIMIT is injected if you omit one, and clamped if yours is "
            "too large."
        ),
    )
    approval_id: str | None = Field(
        default=None,
        description=(
            "Only for re-running a statement a human has approved. Pass the approval_id you "
            "were given. You cannot approve your own query: the id is checked against the "
            "recorded decision and against the exact statement the reviewer saw."
        ),
    )


class SqlRunnerTool(Tool[SqlRunnerInput]):
    name = "sql_runner"
    description = """
Validate and execute one SELECT statement against the analytics schema.

Every statement is parsed and checked before it runs. It must be a single SELECT; DDL, DML,
statement stacking, catalog access, dangerous functions and unconstrained cross joins are all
refused. A LIMIT is added if you omit one.

You get back a query_id, the column names, a preview of the first rows, and the true row count.
Pass the query_id to python_analysis or chart_builder to work with the full result - do not
re-query for data you already have.

Two kinds of refusal, needing different responses:
- refused with verdict 'rejected': the statement broke a rule. Read the reasons and write a
  different statement. Do not retry the same one.
- refused with verdict 'escalated': the statement is valid but needs human approval, because it
  is expensive or touches a restricted column. Stop and wait; do not look for a workaround.

An empty result is a finding, not a failure. Check whether your filter is wrong before you
conclude there is no data.
"""
    input_model = SqlRunnerInput

    def run(
        self, payload: SqlRunnerInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        settings = get_settings()

        with ro_conn() as conn:
            verdict = check(
                payload.sql,
                conn=conn,
                catalog=load_catalog(),
                settings=settings,
                row_limit=payload.row_limit,
            )

            # An escalated statement that a human has cleared is recorded as `approved`, not as
            # `allowed`: the audit should say who permitted it. The escalation stays visible.
            cleared, why_not = (
                self._approval_clears(payload, run_id)
                if verdict.requires_approval
                else (False, None)
            )
            recorded_verdict = "approved" if cleared else verdict.verdict

            # Audited before anything is decided, so a blocked attempt is part of the record.
            query_id = repo.record_sql_audit(
                run_id=run_id,
                purpose=payload.purpose,
                sql_text=payload.sql,
                verdict=recorded_verdict,
                reasons=[str(r) for r in verdict.reasons],
                rewritten_sql=verdict.rewritten_sql,
                referenced_objects=list(verdict.referenced_objects),
                sensitive_columns=list(verdict.sensitive_columns),
                estimated_cost=verdict.estimated_cost,
                step_id=step_id,
            )

            if not verdict.allowed:
                return ToolResult.refuse(
                    "the query was rejected by the SQL guard and did not run",
                    query_id=str(query_id),
                    verdict="rejected",
                    reasons=[str(r) for r in verdict.reasons],
                    guidance="Write a different statement; retrying this one will fail again.",
                )

            if verdict.requires_approval:
                if not cleared:
                    return ToolResult.refuse(
                        "the query is valid but needs human approval before it can run",
                        query_id=str(query_id),
                        verdict="escalated",
                        reasons=[str(r) for r in verdict.reasons],
                        sensitive_columns=list(verdict.sensitive_columns),
                        estimated_cost=verdict.estimated_cost,
                        approval_error=why_not,
                        guidance="Stop and wait for the decision. Do not attempt a workaround.",
                    )
                log.info(
                    "running under an approval",
                    run_id=str(run_id),
                    approval_id=payload.approval_id,
                    query_id=str(query_id),
                )

            statement = verdict.rewritten_sql or payload.sql
            started = time.monotonic()
            try:
                with conn.cursor() as cur:
                    cur.execute(statement)
                    rows = cur.fetchall()
            except pg_errors.QueryCanceled:
                elapsed = int((time.monotonic() - started) * 1000)
                return ToolResult.fail(
                    "the query exceeded the statement timeout and was cancelled",
                    kind="QueryTimeout",
                    message=f"cancelled after {elapsed} ms",
                    query_id=str(query_id),
                    guidance="Narrow the date range, aggregate earlier, or filter to fewer rows.",
                )
            duration_ms = int((time.monotonic() - started) * 1000)

        return self._deliver(
            payload, run_id, query_id, rows, duration_ms, verdict.row_limit, verdict.reasons,
            verdict.estimated_cost,
        )

    @staticmethod
    def _approval_clears(
        payload: SqlRunnerInput, run_id: uuid.UUID
    ) -> tuple[bool, str | None]:
        """Whether a human has cleared this exact statement.

        Checked against the stored decision, never against a flag the caller supplied. A bug in
        a node, or a model that has learned to pass an id, must not be able to manufacture
        consent - so the id has to name a row that a person actually approved, on this run, for
        this statement.
        """
        if not payload.approval_id:
            return False, None
        try:
            approval_id = uuid.UUID(payload.approval_id)
        except ValueError:
            return False, f"{payload.approval_id!r} is not a valid approval id"

        from analyst_agent.agent.approvals import approved_statement

        return approved_statement(run_id, approval_id, payload.sql)

    def _deliver(
        self,
        payload: SqlRunnerInput,
        run_id: uuid.UUID,
        query_id: uuid.UUID,
        rows: list[dict],
        duration_ms: int,
        row_limit: int | None,
        reasons: tuple,
        estimated_cost: float | None,
    ) -> ToolResult:
        """Record the execution, keep the frame, and shape what the model sees."""
        frame = pd.DataFrame(rows)
        row_count = len(frame)
        truncated = row_limit is not None and row_count >= row_limit

        repo.mark_sql_executed(
            query_id, row_count=row_count, truncated=truncated, duration_ms=duration_ms
        )
        get_store().put(
            query_id,
            frame,
            FrameMeta(
                query_id=query_id,
                purpose=payload.purpose,
                truncated=truncated,
                row_count=row_count,
                columns=tuple(str(c) for c in frame.columns),
            ),
        )
        repo.add_run_usage(run_id, repo.Usage(), queries=1)

        if row_count == 0:
            # An empty result is information. Saying so plainly is what stops the model
            # concluding "there is no data" when the filter was simply wrong.
            return ToolResult.succeed(
                "the query ran and returned no rows - check whether the filter is right before "
                "concluding there is no data",
                query_id=str(query_id),
                columns=[],
                rows=[],
                row_count=0,
                duration_ms=duration_ms,
                empty=True,
            )

        preview = frame.head(PREVIEW_ROWS)
        summary = f"{row_count} row(s) in {duration_ms} ms"
        if truncated:
            summary += (
                f"; truncated at the {row_limit} row cap - aggregate rather than reasoning over "
                "a partial sample"
            )
        elif row_count > PREVIEW_ROWS:
            summary += f"; showing the first {PREVIEW_ROWS}"

        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "query_id": str(query_id),
                "columns": [str(c) for c in frame.columns],
                "rows": _jsonable(preview),
                "row_count": row_count,
                "preview_rows": len(preview),
                "truncated": truncated,
                "duration_ms": duration_ms,
                "notes": [str(r) for r in reasons] or None,
            },
            audit={
                "query_id": str(query_id),
                "row_count": row_count,
                "truncated": truncated,
                "estimated_cost": estimated_cost,
            },
        )


def _jsonable(frame: pd.DataFrame) -> list[dict]:
    """Rows as plain JSON-safe values.

    Dates, Decimals and numpy scalars all need coercing. Sent raw they would either fail to
    serialise or reach the model as an opaque repr that it then reasons about incorrectly.
    """
    return [
        {str(key): _scalar(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _scalar(value: object) -> object:
    """Coerce one cell to a JSON-native value.

    ``Decimal`` is handled explicitly and first. Postgres returns every ``numeric`` column as a
    Decimal, and a Decimal has neither ``isoformat`` nor ``item``, so without this it fell through
    to ``str()`` and every monetary figure reached the model as the *string* ``"139184.93"``. The
    model then either compares numbers lexically or spends a turn parsing them - exactly the
    failure this function exists to prevent.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)
