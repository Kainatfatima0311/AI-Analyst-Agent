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
from analyst_agent.reports.snapshot import VERSION_IN_PURPOSE

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


def run_view(
    run_id: uuid.UUID, organization_id: uuid.UUID | None = None
) -> schemas.RunOut:
    """A run as a caller sees it, findings and hypotheses stitched together.

    The organisation is passed down to the trace query rather than checked here: a filter
    applied after the rows are fetched is one a later refactor can drop.
    """
    trace = repo.get_trace(run_id, organization_id=organization_id)
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
        # Key findings are filtered against the queries that actually ran, the same way citations
        # are: a headline pointing at a query that never executed is an unverifiable number in the
        # most prominent place on the page.
        executed_ids = {str(q["query_id"]) for q in trace["queries"] if q.get("executed")}
        answer = schemas.AnswerOut(
            conclusion=stored["conclusion"],
            confidence=stored.get("confidence", "low"),
            confidence_score=score.score,
            confidence_detail=schemas.ConfidenceOut(**score.as_dict()),
            caveats=stored.get("caveats", []),
            refuted=stored.get("refuted", []),
            evidence=evidence,
            key_findings=[
                schemas.KeyFindingOut(
                    title=finding.get("title", ""),
                    impact=finding.get("impact", ""),
                    severity=finding.get("severity", "medium"),
                    evidence_query_ids=[
                        uuid.UUID(q)
                        for q in finding.get("evidence_query_ids", [])
                        if q in executed_ids
                    ],
                )
                for finding in stored.get("key_findings", [])
            ],
            recommendations=[
                schemas.RecommendationOut(
                    action=item.get("action", ""),
                    rationale=item.get("rationale", ""),
                    priority=item.get("priority", "medium"),
                )
                for item in stored.get("recommendations", [])
            ],
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
        investigation=investigation_view(trace),
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


def investigation_view(trace: dict[str, Any]) -> schemas.InvestigationOut:
    """What the agent looked at, read off the trace.

    Derived rather than narrated. Asking the model to describe its own process would produce a
    fluent paragraph that may or may not match what happened; reading the audit trail produces the
    record. The four lists come from four different places on purpose:

    * **metrics** from the definition versions `metric_query` writes into a query's purpose, so
      the list names approved definitions rather than the model's memory of them;
    * **tables** from `referenced_objects`, which the guard resolved while parsing - not from the
      SQL text, which would mean re-parsing it here;
    * **questions** from the hypotheses that reached a terminal state, because an untested
      hypothesis was not investigated;
    * **steps** from the nodes that ran, which is the shape of the investigation itself.
    """
    queries = trace.get("queries") or []
    hypotheses = trace.get("hypotheses") or []

    metrics: list[str] = []
    for query in queries:
        version = VERSION_IN_PURPOSE.search(query.get("purpose") or "")
        if version and version.group(1) not in metrics:
            metrics.append(version.group(1))

    tables: list[str] = []
    for query in queries:
        for obj in query.get("referenced_objects") or []:
            if obj not in tables:
                tables.append(obj)

    questions = [
        hypothesis["statement"]
        for hypothesis in hypotheses
        if hypothesis.get("status") in ("supported", "refuted", "inconclusive")
    ]

    # The nodes a reader would recognise. `intake` and `respond` are bookkeeping, so they are
    # left out: a process list that includes them describes the software, not the analysis.
    interesting = {
        "gather_context": "Explored the schema and resolved the business terms",
        "compute_metrics": "Computed the approved metrics",
        "author_sql": "Wrote and validated SQL",
        "interpret": "Read the results",
        "analyse": "Derived further views from the results",
        "materiality_check": "Judged whether the finding needed explaining",
        "generate_hypotheses": "Proposed competing explanations",
        "design_test": "Designed a falsifying test",
        "evaluate": "Tested an explanation",
        "reconcile": "Weighed the explanations against each other",
        "synthesize": "Wrote the answer",
        "visualize": "Built the charts",
    }
    steps: list[str] = []
    for step in trace.get("steps") or []:
        label = interesting.get(step.get("node", ""))
        if label and label not in steps and step.get("status") != "error":
            steps.append(label)

    return schemas.InvestigationOut(
        metrics_checked=metrics,
        tables_analyzed=tables,
        questions_tested=questions,
        steps=steps,
        queries_executed=sum(1 for q in queries if q.get("executed")),
        queries_blocked=sum(1 for q in queries if q.get("verdict") == "rejected"),
    )


def trace_view(
    run_id: uuid.UUID, organization_id: uuid.UUID | None = None
) -> schemas.TraceOut:
    """The full reconstruction, including what the guard refused."""
    trace = repo.get_trace(run_id, organization_id=organization_id)
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
