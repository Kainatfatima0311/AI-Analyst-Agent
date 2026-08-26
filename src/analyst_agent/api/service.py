"""Turning stored run state into the shapes the API promises.

Kept out of the route handlers so that "what a run looks like to a caller" is one function rather
than something reassembled slightly differently in each endpoint — which is how a trace and a run
view end up disagreeing about the same run.
"""

from __future__ import annotations

import uuid
from typing import Any

from analyst_agent.agent import confidence
from analyst_agent.api import schemas
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)


def _approval(row: dict[str, Any]) -> schemas.ApprovalOut:
    return schemas.ApprovalOut(
        approval_id=row["approval_id"],
        kind=row["kind"],
        reason=row["reason"],
        payload=row["payload"],
        status=row["status"],
        requested_at=row["requested_at"],
        expires_at=row.get("expires_at"),
        decided_at=row.get("decided_at"),
        decided_by=row.get("decided_by"),
        decision_reason=row.get("decision_reason"),
    )


def run_view(run_id: uuid.UUID) -> schemas.RunOut:
    """A run as a caller sees it, findings and hypotheses stitched together."""
    trace = repo.get_trace(run_id)
    run = trace["run"]

    queries = {q["query_id"]: q for q in trace["queries"]}
    answer = None
    if run.get("answer"):
        stored = run["answer"]
        evidence = []
        for item in stored.get("evidence", []):
            query = queries.get(uuid.UUID(item["query_id"]))
            if query is None:
                continue
            evidence.append(
                schemas.EvidenceOut(
                    query_id=query["query_id"],
                    purpose=query["purpose"],
                    row_count=query.get("row_count"),
                    sql=query.get("rewritten_sql") or query["sql_text"],
                )
            )
        # Computed here rather than frozen when the run finished, so a change to the scoring
        # applies to every run in the history instead of only to new ones - and so there is no
        # stored number that can disagree with the trace it came from.
        score = confidence.from_trace(trace, stored)
        answer = schemas.AnswerOut(
            conclusion=stored["conclusion"],
            confidence=stored.get("confidence", "low"),
            confidence_score=score.score,
            confidence_detail=schemas.ConfidenceOut(**score.as_dict()),
            caveats=stored.get("caveats", []),
            refuted=stored.get("refuted", []),
            evidence=evidence,
        )

    hypotheses_by_finding: dict[uuid.UUID, list[schemas.HypothesisOut]] = {}
    for hypothesis in trace["hypotheses"]:
        hypotheses_by_finding.setdefault(hypothesis["finding_id"], []).append(
            schemas.HypothesisOut(
                statement=hypothesis["statement"],
                status=hypothesis["status"],
                reasoning=hypothesis.get("reasoning"),
                test_query_ids=list(hypothesis.get("test_query_ids") or []),
            )
        )

    findings = [
        schemas.FindingOut(
            statement=finding["statement"],
            material=finding["material"],
            evidence_query_ids=list(finding.get("evidence_query_ids") or []),
            hypotheses=hypotheses_by_finding.get(finding["finding_id"], []),
        )
        for finding in trace["findings"]
    ]

    charts = [
        schemas.ChartOut(
            chart_id=chart["chart_id"],
            query_id=chart["query_id"],
            title=chart.get("title"),
            chart_type=chart["chart_type"],
            spec=chart["spec"],
        )
        for chart in trace["charts"]
    ]

    return schemas.RunOut(
        run_id=run["run_id"],
        thread_id=run["thread_id"],
        question=run["question"],
        status=run["status"],
        created_at=run["created_at"],
        finished_at=run.get("finished_at"),
        duration_ms=run.get("duration_ms"),
        queries_used=run.get("queries_used", 0),
        tokens_in=run.get("tokens_in", 0),
        tokens_out=run.get("tokens_out", 0),
        answer=answer,
        findings=findings,
        charts=charts,
        clarifications=[],
        pending_approvals=[
            _approval(a) for a in trace["approvals"] if a["status"] == "pending"
        ],
        error=run.get("error"),
    )


def trace_view(run_id: uuid.UUID) -> schemas.TraceOut:
    """The full reconstruction, including what the guard refused."""
    trace = repo.get_trace(run_id)
    return schemas.TraceOut(
        run_id=trace["run"]["run_id"],
        summary=trace["summary"],
        steps=[
            schemas.StepOut(
                seq=s["seq"],
                node=s["node"],
                status=s["status"],
                effort=s.get("effort"),
                duration_ms=s.get("duration_ms"),
                summary=s.get("summary"),
                error=s.get("error"),
            )
            for s in trace["steps"]
        ],
        tool_calls=[
            schemas.ToolCallOut(
                tool=c["tool"],
                ok=c["ok"],
                refusal=c.get("refusal"),
                duration_ms=c.get("duration_ms"),
                result_summary=c.get("result_summary"),
            )
            for c in trace["tool_calls"]
        ],
        queries=[
            schemas.QueryAuditOut(
                query_id=q["query_id"],
                purpose=q["purpose"],
                verdict=q["verdict"],
                sql=q["sql_text"],
                rewritten_sql=q.get("rewritten_sql"),
                reasons=q.get("reasons") or [],
                referenced_objects=q.get("referenced_objects") or [],
                sensitive_columns=q.get("sensitive_columns") or [],
                estimated_cost=float(q["estimated_cost"]) if q.get("estimated_cost") else None,
                executed=q["executed"],
                row_count=q.get("row_count"),
                truncated=q.get("truncated", False),
                duration_ms=q.get("duration_ms"),
            )
            for q in trace["queries"]
        ],
        approvals=[_approval(a) for a in trace["approvals"]],
    )


def metrics_view() -> list[schemas.MetricOut]:
    from analyst_agent.metrics.registry import get_registry

    return [
        schemas.MetricOut(
            name=d.name,
            title=d.title,
            unit=d.unit,
            grain=d.grain,
            shape=d.shape,
            owner=d.owner,
            aliases=sorted(d.aliases),
            dimensions=sorted(d.dimensions),
            caveats=[" ".join(c.split()) for c in d.caveats],
            definition_version=d.qualified_version,
        )
        for d in get_registry().all()
    ]
