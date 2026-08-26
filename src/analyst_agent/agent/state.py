"""The state a run carries through the graph.

Written as a ``TypedDict`` because that is what LangGraph checkpoints and merges. The
append-only lists use ``operator.add`` as their reducer, so a node returns only what it *adds*
rather than rebuilding the whole list - which is also what makes a partial update safe to replay
after a restart.

Two invariants from the design document appear here as predicates, and the same two are enforced
by CHECK constraints in the database (Step 3). Stating them in both places is deliberate: the
constraint is what cannot be forgotten, and the predicate is what lets the graph *route* on them
rather than discovering the violation at write time.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypedDict, cast

RunStatus = Literal[
    "received",
    "clarifying",
    "investigating",
    "awaiting_approval",
    "completed",
    "failed",
    "truncated",
]
HypothesisStatus = Literal["proposed", "testing", "supported", "refuted", "inconclusive"]


def merge_by_id[T: dict[str, Any]](key: str) -> Callable[[list[T] | None, list[T] | None], list[T]]:
    """A reducer that upserts by an id field instead of appending.

    ``operator.add`` is right for a log of things that happened, and wrong for anything a later
    node has to *revise*. A hypothesis is created as `proposed` and then moved to a terminal
    status by the node that tests it; with an append-only reducer both versions would sit in
    state at once, and the "at least two tested hypotheses" gate would count the stale one.

    This is the same shape of bug that duplicated the metric placeholders in Step 7 - which is
    why it is fixed as a reducer here rather than worked around at each call site.
    """

    def reduce(existing: list[T] | None, incoming: list[T] | None) -> list[T]:
        merged = list(existing or [])
        index = {item.get(key): position for position, item in enumerate(merged)}
        for item in incoming or []:
            position = index.get(item.get(key))
            if position is None:
                index[item.get(key)] = len(merged)
                merged.append(item)
            else:
                # Cast: the merge of two T dicts is a T, but the type system sees a plain dict.
                merged[position] = cast("T", {**merged[position], **item})
        return merged

    return reduce


TERMINAL_HYPOTHESIS_STATUSES: frozenset[str] = frozenset({"supported", "refuted", "inconclusive"})


class Clarification(TypedDict, total=False):
    question: str
    answer: str | None
    asked_at: str


class ResolvedMetric(TypedDict, total=False):
    term: str
    metric: str | None
    definition_version: str | None
    approved: bool
    note: str


class PlanStep(TypedDict, total=False):
    step_id: str
    intent: str
    status: Literal["pending", "done", "skipped", "failed"]


class QueryRecord(TypedDict, total=False):
    query_id: str
    purpose: str
    verdict: Literal["allowed", "rejected", "escalated", "approved"]
    row_count: int
    truncated: bool
    reasons: list[str]


class Finding(TypedDict, total=False):
    finding_id: str
    statement: str
    material: bool
    evidence_query_ids: list[str]


class Hypothesis(TypedDict, total=False):
    hypothesis_id: str
    finding_id: str
    statement: str
    test_design: str
    test_query_ids: list[str]
    status: HypothesisStatus
    reasoning: str
    test_sql: str
    """The statement its test ran, so a sibling can be checked for testing the same thing."""


class ChartRecord(TypedDict, total=False):
    chart_id: str
    query_id: str
    title: str
    chart_type: str


class ErrorRecord(TypedDict, total=False):
    node: str
    kind: str
    message: str
    recoverable: bool
    attempt: int


class Answer(TypedDict, total=False):
    conclusion: str
    confidence: Literal["high", "medium", "low"]
    caveats: list[str]
    evidence: list[dict[str, Any]]
    refuted: list[str]


class AnalystState(TypedDict, total=False):
    """Everything one run knows about itself."""

    run_id: str
    thread_id: str
    question: str
    requested_by: str | None
    status: RunStatus

    clarifications: Annotated[list[Clarification], operator.add]
    resolved_metrics: Annotated[list[ResolvedMetric], operator.add]
    plan: Annotated[list[PlanStep], operator.add]
    queries: Annotated[list[QueryRecord], operator.add]
    findings: Annotated[list[Finding], merge_by_id("finding_id")]
    # Upsert rather than append: a hypothesis is revised from `proposed` to a terminal
    # status by the node that tests it.
    hypotheses: Annotated[list[Hypothesis], merge_by_id("hypothesis_id")]
    charts: Annotated[list[ChartRecord], operator.add]
    errors: Annotated[list[ErrorRecord], operator.add]

    # Not append-only: running totals and the single answer are replaced, not accumulated.
    budget: dict[str, Any]
    answer: Answer | None
    truncation_reason: str | None

    # Scratch passed between adjacent nodes rather than accumulated. Underscored because it is
    # working state, not part of what a run reports; it is checkpointed like everything else, so
    # a restart mid-pair resumes correctly.
    _metric_terms: list[str]
    _investigating_finding_id: str | None
    _hypothesis_queue: list[str]
    _pending_approval: dict[str, Any] | None
    _context_notes: str | None
    _analysis_notes: str | None
    _reconciliations: Annotated[list[dict[str, Any]], operator.add]
    _under_tested: Annotated[list[dict[str, Any]], operator.add]
    _draft: dict[str, Any] | None
    _last_result: dict[str, Any] | None
    _needs_more_data: bool


def initial_state(
    run_id: str, thread_id: str, question: str, requested_by: str | None = None
) -> AnalystState:
    return AnalystState(
        run_id=run_id,
        thread_id=thread_id,
        question=question,
        requested_by=requested_by,
        status="received",
        clarifications=[],
        resolved_metrics=[],
        plan=[],
        queries=[],
        findings=[],
        hypotheses=[],
        charts=[],
        errors=[],
        budget={},
        answer=None,
        truncation_reason=None,
    )


# --- predicates the graph routes on ----------------------------------------


EXECUTABLE_VERDICTS: frozenset[str] = frozenset({"allowed", "approved"})


def executed_query_ids(state: AnalystState) -> list[str]:
    """Queries that actually ran. Rejected and undecided-escalated ones are not evidence.

    `approved` counts. It means the guard escalated the statement and a named human cleared it,
    so it ran and its result is evidence like any other - leaving it out would quietly drop the
    evidence for exactly the queries a reviewer looked at most closely.
    """
    return [
        q["query_id"]
        for q in state.get("queries", [])
        if q.get("verdict") in EXECUTABLE_VERDICTS and q.get("row_count") is not None
    ]


def unapproved_metrics(state: AnalystState) -> list[str]:
    return [m["term"] for m in state.get("resolved_metrics", []) if not m.get("approved")]


def open_clarifications(state: AnalystState) -> list[Clarification]:
    return [c for c in state.get("clarifications", []) if c.get("answer") is None]


def material_findings(state: AnalystState) -> list[Finding]:
    return [f for f in state.get("findings", []) if f.get("material")]


def tested_hypotheses(state: AnalystState, finding_id: str) -> list[Hypothesis]:
    """Hypotheses for a finding that have reached a terminal status.

    Step 8's edge condition reads this: while a material finding has fewer than two of these, the
    route to synthesis is unavailable. Counting only terminal statuses is the point - a merely
    *proposed* hypothesis is not a test.
    """
    return [
        h
        for h in state.get("hypotheses", [])
        if h.get("finding_id") == finding_id and h.get("status") in TERMINAL_HYPOTHESIS_STATUSES
    ]


def every_finding_has_evidence(state: AnalystState) -> bool:
    """The traceability invariant, as a predicate the graph can route on."""
    return all(f.get("evidence_query_ids") for f in state.get("findings", []))
