"""Request and response shapes for the API.

Separate from the agent's internal state on purpose: the state is what the graph needs, and this
is what a caller is promised. Coupling them would make an internal refactor a breaking API change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    model_config = {"extra": "forbid"}

    question: str = Field(min_length=3, max_length=2000, description="The business question.")
    requested_by: str | None = Field(default=None, max_length=200)


class AskResponse(BaseModel):
    run_id: uuid.UUID
    thread_id: str
    status: str
    message: str


class ClarificationOut(BaseModel):
    question: str
    answer: str | None = None


class EvidenceOut(BaseModel):
    query_id: uuid.UUID
    purpose: str
    row_count: int | None = None
    sql: str


class AnswerOut(BaseModel):
    conclusion: str
    confidence: Literal["high", "medium", "low"]
    caveats: list[str] = Field(default_factory=list)
    refuted: list[str] = Field(default_factory=list)
    evidence: list[EvidenceOut] = Field(default_factory=list)


class HypothesisOut(BaseModel):
    statement: str
    status: str
    reasoning: str | None = None
    test_query_ids: list[uuid.UUID] = Field(default_factory=list)


class FindingOut(BaseModel):
    statement: str
    material: bool
    evidence_query_ids: list[uuid.UUID] = Field(default_factory=list)
    hypotheses: list[HypothesisOut] = Field(default_factory=list)


class ChartOut(BaseModel):
    chart_id: uuid.UUID
    query_id: uuid.UUID
    title: str | None = None
    chart_type: str
    spec: dict[str, Any]


class RunOut(BaseModel):
    """What a caller sees while a run is in flight and after it finishes."""

    run_id: uuid.UUID
    thread_id: str
    question: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    queries_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    answer: AnswerOut | None = None
    findings: list[FindingOut] = Field(default_factory=list)
    charts: list[ChartOut] = Field(default_factory=list)
    clarifications: list[ClarificationOut] = Field(default_factory=list)
    pending_approvals: list[ApprovalOut] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class StepOut(BaseModel):
    seq: int
    node: str
    status: str
    effort: str | None = None
    duration_ms: int | None = None
    summary: str | None = None
    error: dict[str, Any] | None = None


class ToolCallOut(BaseModel):
    tool: str
    ok: bool
    refusal: str | None = None
    duration_ms: int | None = None
    result_summary: dict[str, Any] | None = None


class QueryAuditOut(BaseModel):
    """Every query considered, including the ones that never ran.

    Rejected and escalated attempts are part of the trace by design: what the agent *tried* is
    usually what a reviewer wants to know.
    """

    query_id: uuid.UUID
    purpose: str
    verdict: str
    sql: str
    rewritten_sql: str | None = None
    reasons: list[str] = Field(default_factory=list)
    referenced_objects: list[str] = Field(default_factory=list)
    sensitive_columns: list[str] = Field(default_factory=list)
    estimated_cost: float | None = None
    executed: bool = False
    row_count: int | None = None
    truncated: bool = False
    duration_ms: int | None = None


class TraceOut(BaseModel):
    run_id: uuid.UUID
    summary: dict[str, Any]
    steps: list[StepOut] = Field(default_factory=list)
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    queries: list[QueryAuditOut] = Field(default_factory=list)
    approvals: list[ApprovalOut] = Field(default_factory=list)


class ApprovalOut(BaseModel):
    approval_id: uuid.UUID
    kind: str
    reason: str
    payload: dict[str, Any]
    status: str
    requested_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class DecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    decided_by: str = Field(min_length=1, max_length=200, description="Who is deciding.")
    reason: str | None = Field(default=None, max_length=1000)


class AnswerRequest(BaseModel):
    """A reply to a clarification the agent asked for."""

    model_config = {"extra": "forbid"}

    answer: str = Field(min_length=1, max_length=2000)


class MetricOut(BaseModel):
    name: str
    title: str
    unit: str
    grain: str
    shape: str
    owner: str
    aliases: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    definition_version: str


class ErrorOut(BaseModel):
    """One error shape for the whole API, so a client has one thing to parse."""

    error: str
    detail: str
    request_id: str | None = None


RunOut.model_rebuild()
TraceOut.model_rebuild()
