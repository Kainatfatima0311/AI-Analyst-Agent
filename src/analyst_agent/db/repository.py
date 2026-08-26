"""Persistence for run state, traces and audit.

Every write the agent makes about *itself* goes through here, on the ``app_rw`` pool. Nothing
in this module executes agent-authored SQL — that is ``tools/sql_runner.py``, on the read-only
pool, after ``sql_guard`` has approved it.

The operative test for this layer (standard 4 in the design document): for any finished run, a
reviewer who was not present must be able to explain why it succeeded or failed from
``get_trace`` alone.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from analyst_agent.db.engine import rw_conn
from analyst_agent.observability.logging import bound, get_logger

log = get_logger(__name__)

RunStatus = str  # constrained by a CHECK in db/migrations/001_agent_state.sql
Verdict = str
ApprovalKind = str


@dataclass
class Usage:
    """Token accounting for one model call or one node."""

    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cost_usd + other.cost_usd,
        )


@dataclass
class StepHandle:
    step_id: uuid.UUID
    run_id: uuid.UUID
    seq: int
    node: str
    started_monotonic: float = field(default_factory=time.monotonic)
    finished: bool = False
    """Set once the step has been closed, so the ``step`` context manager does not close it
    again. Without this, a node that recorded its own summary had that summary overwritten with
    NULL by the automatic close on the way out - silently emptying the one human-readable
    column in the whole trace."""

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


# --- runs -------------------------------------------------------------------


def create_run(
    question: str,
    requested_by: str | None = None,
    thread_id: str | None = None,
    organization_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Start a run, owned by an organisation.

    `COALESCE` to the default organisation rather than raising: the column is NOT NULL with a
    default in the schema, and a caller that has not been taught about tenancy yet should produce
    a row owned by the organisation that was implicitly there before Phase 3 - not a 500.
    """
    run_id = uuid.uuid4()
    thread = thread_id or f"run-{run_id}"
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.runs (run_id, thread_id, question, requested_by, organization_id) "
            "VALUES (%s, %s, %s, %s, "
            "COALESCE(%s, '00000000-0000-0000-0000-000000000001'::uuid))",
            (run_id, thread, question, requested_by, organization_id),
        )
    log.info("run created", run_id=str(run_id), thread_id=thread)
    return run_id


def set_run_status(run_id: uuid.UUID, status: RunStatus) -> None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.runs SET status = %s, "
            "started_at = COALESCE(started_at, CASE WHEN %s = 'investigating' "
            "                                       THEN now() ELSE NULL END) "
            "WHERE run_id = %s",
            (status, status, run_id),
        )
    log.info("run status", run_id=str(run_id), status=status)


def finish_run(
    run_id: uuid.UUID,
    status: RunStatus,
    answer: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Close a run. `truncated` is a legitimate terminal state, not a failure."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.runs SET status = %s, answer = %s, error = %s, finished_at = now(), "
            "duration_ms = (EXTRACT(EPOCH FROM (now() - COALESCE(started_at, created_at))) "
            "               * 1000)::integer "
            "WHERE run_id = %s",
            (status, Jsonb(answer) if answer else None, Jsonb(error) if error else None, run_id),
        )
    log.info("run finished", run_id=str(run_id), status=status)


def add_run_usage(run_id: uuid.UUID, usage: Usage, queries: int = 0, iterations: int = 0) -> None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.runs SET tokens_in = tokens_in + %s, tokens_out = tokens_out + %s, "
            "cache_read_tokens = cache_read_tokens + %s, cost_usd = cost_usd + %s, "
            "queries_used = queries_used + %s, iterations = iterations + %s WHERE run_id = %s",
            (
                usage.tokens_in,
                usage.tokens_out,
                usage.cache_read_tokens,
                usage.cost_usd,
                queries,
                iterations,
                run_id,
            ),
        )


# --- steps ------------------------------------------------------------------


def start_step(run_id: uuid.UUID, node: str, effort: str | None = None, attempt: int = 1) -> StepHandle:
    step_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(max(seq), 0) + 1 AS next FROM agent.run_steps WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        seq = int(row["next"]) if row else 1
        cur.execute(
            "INSERT INTO agent.run_steps (step_id, run_id, seq, node, effort, attempt) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (step_id, run_id, seq, node, effort, attempt),
        )
    return StepHandle(step_id=step_id, run_id=run_id, seq=seq, node=node)


def finish_step(
    handle: StepHandle,
    status: str = "ok",
    summary: str | None = None,
    error: dict[str, Any] | None = None,
    usage: Usage | None = None,
) -> None:
    usage = usage or Usage()
    handle.finished = True
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.run_steps SET status = %s, summary = %s, error = %s, "
            "finished_at = now(), duration_ms = %s, tokens_in = %s, tokens_out = %s, "
            "cache_read_tokens = %s WHERE step_id = %s",
            (
                status,
                summary,
                Jsonb(error) if error else None,
                handle.elapsed_ms,
                usage.tokens_in,
                usage.tokens_out,
                usage.cache_read_tokens,
                handle.step_id,
            ),
        )


@contextmanager
def step(run_id: uuid.UUID, node: str, effort: str | None = None, attempt: int = 1) -> Iterator[StepHandle]:
    """Run a node inside a recorded, log-bound step.

    An exception is recorded as an `error` step and re-raised: the graph decides whether it is
    recoverable, but the trace keeps the evidence either way.
    """
    handle = start_step(run_id, node, effort=effort, attempt=attempt)
    with bound(run_id=str(run_id), step_id=str(handle.step_id), node=node):
        log.debug("node started", seq=handle.seq, attempt=attempt)
        try:
            yield handle
        except Exception as exc:
            # An error closes the step even if the node already closed it: how a step ended
            # matters more than the summary it managed to write before failing.
            finish_step(
                handle,
                status="error",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            log.error("node failed", error=str(exc), error_type=type(exc).__name__)
            raise
        else:
            # Only if the node did not close it itself. A node that recorded a summary has
            # already said something more useful than "ok".
            if not handle.finished:
                finish_step(handle, status="ok")
            log.debug("node finished", duration_ms=handle.elapsed_ms)


# --- tool calls -------------------------------------------------------------


def record_tool_call(
    run_id: uuid.UUID,
    tool: str,
    arguments: dict[str, Any],
    ok: bool,
    step_id: uuid.UUID | None = None,
    result_summary: dict[str, Any] | None = None,
    refusal: str | None = None,
    error: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> uuid.UUID:
    """Record one tool invocation.

    A refusal is passed as ``refusal`` with ``ok=True``: the tool did its job by declining,
    and conflating that with an error would hide the difference in the trace.
    """
    tool_call_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.tool_calls (tool_call_id, run_id, step_id, tool, arguments, "
            "result_summary, ok, refusal, error, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                tool_call_id,
                run_id,
                step_id,
                tool,
                Jsonb(arguments),
                Jsonb(result_summary) if result_summary else None,
                ok,
                refusal,
                Jsonb(error) if error else None,
                duration_ms,
            ),
        )
    log.info(
        "tool call",
        run_id=str(run_id),
        tool=tool,
        ok=ok,
        refused=refusal is not None,
        duration_ms=duration_ms,
    )
    return tool_call_id


# --- SQL audit --------------------------------------------------------------


def record_sql_audit(
    run_id: uuid.UUID,
    purpose: str,
    sql_text: str,
    verdict: Verdict,
    reasons: list[str] | None = None,
    rewritten_sql: str | None = None,
    referenced_objects: list[str] | None = None,
    sensitive_columns: list[str] | None = None,
    estimated_cost: float | None = None,
    step_id: uuid.UUID | None = None,
    tool_call_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record a query *before* it runs, whatever the verdict.

    Rejected and escalated queries are recorded too. A run where the guard blocked three
    attempts is more informative than one where those attempts vanished.
    """
    query_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.sql_audit (query_id, run_id, step_id, tool_call_id, purpose, "
            "sql_text, rewritten_sql, verdict, reasons, referenced_objects, sensitive_columns, "
            "estimated_cost) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                query_id,
                run_id,
                step_id,
                tool_call_id,
                purpose,
                sql_text,
                rewritten_sql,
                verdict,
                Jsonb(reasons or []),
                Jsonb(referenced_objects or []),
                Jsonb(sensitive_columns or []),
                estimated_cost,
            ),
        )
    log.info(
        "sql audited",
        run_id=str(run_id),
        query_id=str(query_id),
        verdict=verdict,
        purpose=purpose,
        sql=sql_text,
        reasons=reasons or [],
    )
    return query_id


def mark_sql_executed(
    query_id: uuid.UUID, row_count: int, truncated: bool, duration_ms: int
) -> None:
    """Mark an audited query as actually executed.

    The database refuses this for anything the guard did not allow, via the
    ``sql_audit_executed_implies_allowed`` constraint — so a bug in the tool layer cannot
    produce an execution record for a rejected query.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.sql_audit SET executed = true, row_count = %s, truncated = %s, "
            "duration_ms = %s WHERE query_id = %s",
            (row_count, truncated, duration_ms, query_id),
        )


# --- approvals --------------------------------------------------------------


def create_approval(
    run_id: uuid.UUID,
    kind: ApprovalKind,
    reason: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> uuid.UUID:
    approval_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.approvals (approval_id, run_id, kind, reason, payload, "
            "expires_at) VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (approval_id, run_id, kind, reason, Jsonb(payload), timeout_seconds),
        )
    log.info("approval requested", run_id=str(run_id), approval_id=str(approval_id), kind=kind)
    return approval_id


def decide_approval(
    approval_id: uuid.UUID, status: str, decided_by: str | None, decision_reason: str | None = None
) -> bool:
    """Record a decision. Returns False if the approval was already decided.

    The guard against re-deciding is in the WHERE clause rather than in a read-then-write, so
    two concurrent decisions cannot both succeed.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.approvals SET status = %s, decided_at = now(), decided_by = %s, "
            "decision_reason = %s WHERE approval_id = %s AND status = 'pending'",
            (status, decided_by, decision_reason, approval_id),
        )
        changed = cur.rowcount == 1
    log.info(
        "approval decided", approval_id=str(approval_id), status=status, applied=changed
    )
    return changed


def expire_stale_approvals() -> int:
    """Auto-reject approvals past their deadline. A timeout is recorded, never inferred."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.approvals SET status = 'timed_out', decided_at = now(), "
            "decision_reason = 'no decision before the deadline' "
            "WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < now()"
        )
        return cur.rowcount


def pending_approvals(run_id: uuid.UUID) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent.approvals WHERE run_id = %s AND status = 'pending' "
            "ORDER BY requested_at",
            (run_id,),
        )
        return list(cur.fetchall())


# --- findings and hypotheses ------------------------------------------------


def record_finding(
    run_id: uuid.UUID,
    statement: str,
    evidence_query_ids: list[uuid.UUID],
    material: bool = False,
) -> uuid.UUID:
    """Record a finding. The database rejects one with no evidence.

    That constraint (``findings_require_evidence``) is the enforcement point for the design
    document's traceability invariant. Keeping it in the schema means a future node cannot
    forget it.
    """
    finding_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.findings (finding_id, run_id, statement, material, "
            "evidence_query_ids) VALUES (%s, %s, %s, %s, %s)",
            (finding_id, run_id, statement, material, list(evidence_query_ids)),
        )
    log.info(
        "finding recorded",
        run_id=str(run_id),
        finding_id=str(finding_id),
        material=material,
        evidence_count=len(evidence_query_ids),
    )
    return finding_id


def record_hypothesis(
    run_id: uuid.UUID, finding_id: uuid.UUID, statement: str, test_design: str
) -> uuid.UUID:
    hypothesis_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.hypotheses (hypothesis_id, run_id, finding_id, statement, "
            "test_design) VALUES (%s, %s, %s, %s, %s)",
            (hypothesis_id, run_id, finding_id, statement, test_design),
        )
    return hypothesis_id


def update_hypothesis(
    hypothesis_id: uuid.UUID,
    status: str,
    test_query_ids: list[uuid.UUID] | None = None,
    reasoning: str | None = None,
) -> None:
    """Move a hypothesis along. Leaving `proposed` without a test query is rejected by the
    ``hypotheses_require_a_test`` constraint — an untested hypothesis cannot become a verdict.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.hypotheses SET status = %s, "
            "test_query_ids = COALESCE(%s, test_query_ids), "
            "reasoning = COALESCE(%s, reasoning), updated_at = now() "
            "WHERE hypothesis_id = %s",
            (status, list(test_query_ids) if test_query_ids is not None else None, reasoning, hypothesis_id),
        )
    log.info("hypothesis updated", hypothesis_id=str(hypothesis_id), status=status)


def hypotheses_for_finding(finding_id: uuid.UUID) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent.hypotheses WHERE finding_id = %s ORDER BY created_at",
            (finding_id,),
        )
        return list(cur.fetchall())


def terminal_hypothesis_count(finding_id: uuid.UUID) -> int:
    """How many hypotheses for this finding have actually been tested to a conclusion.

    The graph reads this to decide whether the edge to `synthesize` is available. It counts
    only terminal statuses, so a finding cannot be closed on the strength of hypotheses that
    were merely proposed.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM agent.hypotheses WHERE finding_id = %s "
            "AND status IN ('supported', 'refuted', 'inconclusive')",
            (finding_id,),
        )
        row = cur.fetchone()
        return int(row["n"]) if row else 0


# --- charts -----------------------------------------------------------------


def record_chart(
    run_id: uuid.UUID,
    query_id: uuid.UUID,
    chart_type: str,
    spec: dict[str, Any],
    title: str | None = None,
    png: bytes | None = None,
) -> uuid.UUID:
    chart_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.charts (chart_id, run_id, query_id, chart_type, title, spec, png) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (chart_id, run_id, query_id, chart_type, title, Jsonb(spec), png),
        )
    return chart_id


# --- reading it back --------------------------------------------------------


def get_run(run_id: uuid.UUID) -> dict[str, Any] | None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM agent.runs WHERE run_id = %s", (run_id,))
        return cur.fetchone()


def get_trace(run_id: uuid.UUID, organization_id: uuid.UUID | None = None) -> dict[str, Any]:
    """The full reconstruction of a run.

    This is what `GET /v1/runs/{id}/trace` returns and what the UI's evidence drawer is built
    on. It includes rejected queries and undecided approvals on purpose: the question a
    reviewer asks is usually "what did it try", not only "what did it do".
    """
    with rw_conn() as conn, conn.cursor() as cur:
        if organization_id is None:
            cur.execute("SELECT * FROM agent.runs WHERE run_id = %s", (run_id,))
        else:
            # Scoped in the WHERE clause, and a miss is indistinguishable from "no such run".
            # Answering 403 for another tenant's run would confirm that it exists, which is how a
            # probe turns into an enumeration of somebody else's questions.
            cur.execute(
                "SELECT * FROM agent.runs WHERE run_id = %s AND organization_id = %s",
                (run_id, organization_id),
            )
        run = cur.fetchone()
        if run is None:
            raise KeyError(f"no such run: {run_id}")

        def fetch(sql: str) -> list[dict[str, Any]]:
            cur.execute(sql, (run_id,))
            return list(cur.fetchall())

        trace = {
            "run": run,
            "steps": fetch("SELECT * FROM agent.run_steps WHERE run_id = %s ORDER BY seq"),
            "tool_calls": fetch(
                "SELECT * FROM agent.tool_calls WHERE run_id = %s ORDER BY started_at"
            ),
            "queries": fetch(
                "SELECT * FROM agent.sql_audit WHERE run_id = %s ORDER BY created_at"
            ),
            "approvals": fetch(
                "SELECT * FROM agent.approvals WHERE run_id = %s ORDER BY requested_at"
            ),
            "findings": fetch(
                "SELECT * FROM agent.findings WHERE run_id = %s ORDER BY created_at"
            ),
            "hypotheses": fetch(
                "SELECT * FROM agent.hypotheses WHERE run_id = %s ORDER BY created_at"
            ),
            "charts": fetch(
                "SELECT chart_id, run_id, query_id, chart_type, title, spec, created_at "
                "FROM agent.charts WHERE run_id = %s ORDER BY created_at"
            ),
        }

    queries = trace["queries"]
    trace["summary"] = {
        "steps": len(trace["steps"]),
        "tool_calls": len(trace["tool_calls"]),
        "queries_considered": len(queries),
        "queries_executed": sum(1 for q in queries if q["executed"]),
        "queries_rejected": sum(1 for q in queries if q["verdict"] == "rejected"),
        "queries_escalated": sum(1 for q in queries if q["verdict"] == "escalated"),
        "approvals_pending": sum(1 for a in trace["approvals"] if a["status"] == "pending"),
        "hypotheses_refuted": sum(1 for h in trace["hypotheses"] if h["status"] == "refuted"),
    }
    return trace


def recent_runs(
    limit: int = 50, organization_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Recent runs, scoped to one organisation when given.

    The filter is in the SQL rather than applied to the result: filtering afterwards means the
    rows were fetched, and a fetched row is one a later refactor can forget to drop.
    """
    columns = (
        "SELECT run_id, thread_id, question, status, created_at, finished_at, duration_ms, "
        "queries_used, tokens_in, tokens_out, cost_usd FROM agent.runs "
    )
    with rw_conn() as conn, conn.cursor() as cur:
        if organization_id is None:
            cur.execute(columns + "ORDER BY created_at DESC LIMIT %s", (limit,))
        else:
            cur.execute(
                columns + "WHERE organization_id = %s ORDER BY created_at DESC LIMIT %s",
                (organization_id, limit),
            )
        return list(cur.fetchall())


def resumable_runs() -> list[dict[str, Any]]:
    """Runs left mid-flight by a crash or restart, for the API to pick back up on startup."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, thread_id, status FROM agent.runs "
            "WHERE status IN ('received', 'clarifying', 'investigating', 'awaiting_approval') "
            "ORDER BY created_at"
        )
        return list(cur.fetchall())

# --- dashboard ---------------------------------------------------------------


def dashboard_summary(
    recent: int = 6, organization_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Everything the dashboard shows, in one round trip.

    One function and one connection rather than six endpoints: a dashboard that fires six
    requests shows six different moments, and the totals then disagree with the list beneath
    them for no reason a reader can see.

    "Most used metrics" comes from ``tool_calls`` where the tool was ``metric_query`` — the only
    place a metric is invoked *by name*. Parsing metric names out of SQL text would be guessing;
    the tool call recorded exactly which approved definition was asked for.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE status = 'completed') AS completed, "
            "count(*) FILTER (WHERE status = 'failed') AS failed, "
            "count(*) FILTER (WHERE status = 'truncated') AS truncated, "
            "count(*) FILTER (WHERE status = 'clarifying') AS clarifying, "
            "count(*) FILTER (WHERE status = 'awaiting_approval') AS awaiting_approval, "
            "count(*) FILTER (WHERE status IN ('received', 'investigating')) AS in_flight, "
            "coalesce(sum(tokens_in + tokens_out), 0) AS tokens, "
            "coalesce(sum(queries_used), 0) AS queries, "
            "coalesce(sum(cost_usd), 0) AS cost_usd "
            "FROM agent.runs WHERE (%s::uuid IS NULL OR organization_id = %s)",
            (organization_id, organization_id),
        )
        totals = cur.fetchone() or {}

        cur.execute(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS median_ms "
            "FROM agent.runs WHERE duration_ms IS NOT NULL "
            "AND (%s::uuid IS NULL OR organization_id = %s)",
            (organization_id, organization_id),
        )
        median = (cur.fetchone() or {}).get("median_ms")

        cur.execute(
            "SELECT count(*) AS n FROM agent.reports "
            "WHERE (%s::uuid IS NULL OR organization_id = %s)",
            (organization_id, organization_id),
        )
        reports = (cur.fetchone() or {}).get("n", 0)

        cur.execute(
            "SELECT run_id, question, status, created_at, duration_ms "
            "FROM agent.runs WHERE (%s::uuid IS NULL OR organization_id = %s) "
            "ORDER BY created_at DESC LIMIT %s",
            (organization_id, organization_id, recent),
        )
        questions = list(cur.fetchall())

        cur.execute(
            "SELECT t.arguments ->> 'metric' AS metric, count(*) AS uses, "
            "max(t.started_at) AS last_used "
            "FROM agent.tool_calls t JOIN agent.runs r ON r.run_id = t.run_id "
            "WHERE t.tool = 'metric_query' AND t.arguments ->> 'metric' IS NOT NULL "
            "AND (%s::uuid IS NULL OR r.organization_id = %s) "
            "GROUP BY 1 ORDER BY uses DESC, metric LIMIT 8",
            (organization_id, organization_id),
        )
        metrics = list(cur.fetchall())

        # Findings, newest first - the closest thing the system has to "what it noticed".
        cur.execute(
            "SELECT f.finding_id, f.run_id, f.statement, f.material, f.created_at, r.question "
            "FROM agent.findings f JOIN agent.runs r ON r.run_id = f.run_id "
            "WHERE (%s::uuid IS NULL OR r.organization_id = %s) "
            "ORDER BY f.created_at DESC LIMIT %s",
            (organization_id, organization_id, recent),
        )
        insights = list(cur.fetchall())

    total = int(totals.get("total") or 0)
    completed = int(totals.get("completed") or 0)
    failed = int(totals.get("failed") or 0)
    truncated = int(totals.get("truncated") or 0)
    # Rate over *finished* runs only. Counting a run still in flight as a failure would make the
    # number drop every time somebody asks a question.
    finished = completed + failed + truncated

    return {
        "totals": {
            "analyses": total,
            "completed": completed,
            "failed": failed,
            "truncated": truncated,
            "clarifying": int(totals.get("clarifying") or 0),
            "awaiting_approval": int(totals.get("awaiting_approval") or 0),
            "in_flight": int(totals.get("in_flight") or 0),
            "saved_reports": int(reports or 0),
            "queries": int(totals.get("queries") or 0),
            "tokens": int(totals.get("tokens") or 0),
            "cost_usd": float(totals.get("cost_usd") or 0),
            "median_duration_ms": int(median) if median is not None else None,
        },
        "outcomes": {
            "finished": finished,
            "success_rate": round(100 * completed / finished, 1) if finished else None,
            "failure_rate": round(100 * failed / finished, 1) if finished else None,
        },
        "recent_questions": questions,
        "top_metrics": metrics,
        "recent_insights": insights,
    }


# --- saved reports -----------------------------------------------------------


def save_report(
    run_id: uuid.UUID,
    name: str,
    snapshot: dict[str, Any],
    created_by: str | None = None,
    organization_id: uuid.UUID | None = None,
    saved_by_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Freeze a run as a report. See db/migrations/004 for why it is a snapshot."""
    report_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.reports (report_id, run_id, name, created_by, snapshot, "
            "organization_id, saved_by_user_id) VALUES (%s, %s, %s, %s, %s, "
            "COALESCE(%s, '00000000-0000-0000-0000-000000000001'::uuid), %s)",
            (
                report_id,
                run_id,
                name.strip(),
                created_by,
                Jsonb(snapshot),
                organization_id,
                saved_by_user_id,
            ),
        )
    log.info("report saved", report_id=str(report_id), run_id=str(run_id), name=name.strip())
    return report_id


def list_reports(
    limit: int = 50,
    organization_id: uuid.UUID | None = None,
    viewer_user_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Report rows without their snapshots.

    The snapshot of a long run is large, and a list of forty of them would be megabytes to render
    a page of names. The counts a list needs are read out of the snapshot in SQL instead.
    """
    columns = (
        "SELECT report_id, run_id, name, created_by, created_at, updated_at, visibility, "
        "snapshot ->> 'question' AS question, "
        "snapshot -> 'confidence' ->> 'score' AS confidence_score, "
        "jsonb_array_length(coalesce(snapshot -> 'charts', '[]'::jsonb)) AS charts, "
        "jsonb_array_length(coalesce(snapshot -> 'evidence', '[]'::jsonb)) AS queries "
        "FROM agent.reports "
    )
    with rw_conn() as conn, conn.cursor() as cur:
        if organization_id is None:
            cur.execute(columns + "ORDER BY created_at DESC LIMIT %s", (limit,))
        else:
            # A private report is the saver's; team and public belong to the organisation. Both
            # halves of that rule are in the SQL, so a route cannot implement only one of them.
            cur.execute(
                columns
                + "WHERE organization_id = %s AND (visibility <> 'private' "
                "OR saved_by_user_id IS NULL OR saved_by_user_id = %s) "
                "ORDER BY created_at DESC LIMIT %s",
                (organization_id, viewer_user_id, limit),
            )
        return list(cur.fetchall())


def get_report(
    report_id: uuid.UUID, organization_id: uuid.UUID | None = None
) -> dict[str, Any] | None:
    with rw_conn() as conn, conn.cursor() as cur:
        if organization_id is None:
            cur.execute("SELECT * FROM agent.reports WHERE report_id = %s", (report_id,))
        else:
            cur.execute(
                "SELECT * FROM agent.reports WHERE report_id = %s AND organization_id = %s",
                (report_id, organization_id),
            )
        return cur.fetchone()


def rename_report(
    report_id: uuid.UUID, name: str, organization_id: uuid.UUID | None = None
) -> bool:
    """The only mutable field. False if there is no such report *for this organisation*."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.reports SET name = %s, updated_at = now() WHERE report_id = %s "
            "AND (%s::uuid IS NULL OR organization_id = %s)",
            (name.strip(), report_id, organization_id, organization_id),
        )
        renamed = cur.rowcount == 1
    if renamed:
        log.info("report renamed", report_id=str(report_id), name=name.strip())
    return renamed


def delete_report(report_id: uuid.UUID, organization_id: uuid.UUID | None = None) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent.reports WHERE report_id = %s "
            "AND (%s::uuid IS NULL OR organization_id = %s)",
            (report_id, organization_id, organization_id),
        )
        deleted = cur.rowcount == 1
    if deleted:
        log.info("report deleted", report_id=str(report_id))
    return deleted


def chart_png(
    chart_id: uuid.UUID, organization_id: uuid.UUID | None = None
) -> tuple[bytes, str] | None:
    """The stored PNG for one chart, with its title.

    Rendered once when the chart was built rather than on demand: the frame it came from may have
    been evicted by then, and re-rendering from a spec that no longer has its data would produce
    a different picture under the same id.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        # Joined to runs for the organisation: a chart id is guessable in principle, and an
        # image is data.
        cur.execute(
            "SELECT c.png, c.title FROM agent.charts c JOIN agent.runs r ON r.run_id = c.run_id "
            "WHERE c.chart_id = %s AND (%s::uuid IS NULL OR r.organization_id = %s)",
            (chart_id, organization_id, organization_id),
        )
        row = cur.fetchone()
    if row is None or row["png"] is None:
        return None
    return bytes(row["png"]), str(row["title"] or "chart")
