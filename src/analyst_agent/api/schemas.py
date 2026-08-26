"""Request and response shapes for the API.

Separate from the agent's internal state on purpose: the state is what the graph needs, and this
is what a caller is promised. Coupling them would make an internal refactor a breaking API change.
"""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


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


# --- confidence ---------------------------------------------------------------


class ConfidenceFactorOut(BaseModel):
    """One reason the score is what it is.

    Returned alongside the number rather than folded into it: a confidence figure nobody can
    decompose is decoration. `weight` is 0 for a factor that does not apply to this run — a
    factual question has no hypotheses to test, and scoring it as a failure would be wrong.
    """

    key: str
    label: str
    passed: bool
    earned: float
    weight: float
    detail: str = ""


class ConfidenceOut(BaseModel):
    score: int = Field(ge=0, le=100)
    band: Literal["high", "medium", "low"]
    capped_by: str | None = Field(
        default=None,
        description=(
            "The agent's own stated band, when it held the score below what the factors earned. "
            "Stated confidence is a ceiling, never a floor."
        ),
    )
    factors: list[ConfidenceFactorOut] = Field(default_factory=list)


class AnswerOut(BaseModel):
    conclusion: str
    confidence: Literal["high", "medium", "low"]
    """The band the agent stated. Kept beside the score rather than replaced by it."""

    confidence_score: int = Field(default=0, ge=0, le=100)
    confidence_detail: ConfidenceOut | None = Field(
        default=None,
        description="The score and the factors behind it. Computed from the trace on read.",
    )
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


# --- reports ------------------------------------------------------------------


class SaveReportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: uuid.UUID
    name: str | None = Field(
        default=None,
        max_length=200,
        description="Left out, the question becomes the name.",
    )
    saved_by: str | None = Field(default=None, max_length=200)


class RenameReportRequest(BaseModel):
    """The only mutable field on a report.

    Renaming is a labelling decision; editing what a report *reports* would defeat the point of
    saving it, so there is no endpoint for that.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _not_only_whitespace(cls, value: str) -> str:
        """`min_length` counts spaces, and the database refuses a blank name.

        Without this, "   " passes validation, reaches the CHECK constraint, and comes back as a
        500 — a client error reported as a server fault, which sends whoever is debugging it to
        entirely the wrong place.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("a report name cannot be blank")
        return stripped


class ReportSummaryOut(BaseModel):
    report_id: uuid.UUID
    run_id: uuid.UUID
    name: str
    visibility: Literal["private", "team", "public"] = "private"
    question: str | None = None
    created_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    confidence_score: int | None = None
    charts: int = 0
    queries: int = 0


class ReportOut(BaseModel):
    report_id: uuid.UUID
    run_id: uuid.UUID
    name: str
    created_by: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    snapshot: dict[str, Any] = Field(
        description=(
            "The report as it read when it was saved: question, answer, confidence, findings, "
            "evidence with its SQL, the metric definition versions used, and the charts. Frozen "
            "on purpose - see db/migrations/004."
        )
    )


# --- dashboard ----------------------------------------------------------------


class DashboardTotals(BaseModel):
    analyses: int
    completed: int
    failed: int
    truncated: int
    clarifying: int
    awaiting_approval: int
    in_flight: int
    saved_reports: int
    queries: int
    tokens: int
    cost_usd: float
    median_duration_ms: int | None = None


class DashboardOutcomes(BaseModel):
    finished: int
    success_rate: float | None = Field(
        default=None,
        description=(
            "Over finished runs only. Counting a run still in flight as a failure would make the "
            "number drop every time somebody asks a question."
        ),
    )
    failure_rate: float | None = None


class RecentQuestionOut(BaseModel):
    run_id: uuid.UUID
    question: str
    status: str
    created_at: dt.datetime
    duration_ms: int | None = None


class MetricUsageOut(BaseModel):
    metric: str
    uses: int
    last_used: dt.datetime | None = None


class InsightOut(BaseModel):
    finding_id: uuid.UUID
    run_id: uuid.UUID
    question: str
    statement: str
    material: bool
    created_at: dt.datetime


class DashboardOut(BaseModel):
    totals: DashboardTotals
    outcomes: DashboardOutcomes
    recent_questions: list[RecentQuestionOut] = Field(default_factory=list)
    top_metrics: list[MetricUsageOut] = Field(default_factory=list)
    recent_insights: list[InsightOut] = Field(default_factory=list)


# --- tenancy ------------------------------------------------------------------


class OrganizationOut(BaseModel):
    organization_id: uuid.UUID
    name: str
    slug: str
    created_at: dt.datetime
    members: int = 0


class WhoAmIOut(BaseModel):
    """What the caller is, as the server sees it.

    ``authenticated`` is reported rather than implied: with ``REQUIRE_AUTHENTICATION=false`` a
    request with no key is the default organisation's owner, and a page that could not tell the
    difference would show a demo as though it were a tenant.
    """

    organization: OrganizationOut
    user_id: uuid.UUID
    email: str
    role: Literal["owner", "admin", "analyst", "viewer"]
    authenticated: bool
    permissions: list[str] = Field(
        default_factory=list, description="The named actions this role may take."
    )


class CreateOrganizationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    owner_email: EmailStr


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    role: Literal["owner", "admin", "analyst", "viewer"]
    joined_at: dt.datetime
    invited_by_email: str | None = None
    analyses: int = 0
    reports: int = 0
    last_active_at: dt.datetime | None = None


class TeamOut(BaseModel):
    organization: OrganizationOut
    members: list[MemberOut] = Field(default_factory=list)


class InviteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr
    role: Literal["admin", "analyst", "viewer"] = Field(
        default="analyst",
        description=(
            "Owner is not invitable: it is transferred by promoting an existing member, so an "
            "organisation cannot acquire a second owner by a typo in an invitation."
        ),
    )


class InviteOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    created: bool
    message: str


class UpdateMemberRequest(BaseModel):
    """A role change, or a removal. One endpoint, because both are "change this membership"."""

    model_config = {"extra": "forbid"}

    role: Literal["owner", "admin", "analyst", "viewer"] | None = None
    remove: bool = False


class ApiKeyOut(BaseModel):
    key_id: uuid.UUID
    name: str
    prefix: str
    email: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None


class IssuedApiKeyOut(ApiKeyOut):
    token: str = Field(description="Shown once. Only its hash is stored, so this cannot be re-read.")


class IssueApiKeyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = Field(
        default=None, description="Whose key this is. Defaults to the caller."
    )


# --- data sources -------------------------------------------------------------


class DataSourceOut(BaseModel):
    """A source, without its credentials.

    ``summary`` is an allowlisted redaction — host, database, user, filename. A password is not
    starred out and returned; it is absent, because a masked value in a response is still a
    statement about its length.
    """

    data_source_id: uuid.UUID
    name: str
    type: Literal["postgres", "csv", "excel"]
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
    last_checked_at: dt.datetime | None = None
    last_status: str | None = None


class CreateDataSourceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    type: Literal["postgres", "csv", "excel"]
    config: dict[str, Any] = Field(
        description=(
            "Connection details. Encrypted at rest with SECRETS_KEY and never returned by any "
            "endpoint - only the allowlisted summary comes back."
        )
    )


# --- sharing ------------------------------------------------------------------


class ShareRequest(BaseModel):
    model_config = {"extra": "forbid"}

    audience: Literal["team", "public"] = "team"
    expires_in_hours: int | None = Field(
        default=None,
        ge=1,
        le=24 * 365,
        description="Left out, the link does not expire. A link with no end is a decision, so it is the explicit one.",
    )


class ShareOut(BaseModel):
    share_id: uuid.UUID
    audience: str
    prefix: str
    url: str
    created_at: dt.datetime
    expires_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    use_count: int = 0
    created_by_email: str | None = None


class IssuedShareOut(ShareOut):
    token: str = Field(description="Shown once; only its hash is stored.")


class VisibilityRequest(BaseModel):
    model_config = {"extra": "forbid"}

    visibility: Literal["private", "team", "public"]


class SharedReportOut(BaseModel):
    """A report read through a share link.

    Deliberately narrower than `ReportOut`: no organisation id, no run id, no saver. Somebody
    holding a link is not a member, and the answer is what was shared - not the identifiers around
    it.
    """

    name: str
    audience: str
    created_at: dt.datetime
    expires_at: dt.datetime | None = None
    snapshot: dict[str, Any]


# --- alerts -------------------------------------------------------------------


class CreateAlertRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    metric: str = Field(description="An approved metric name. An alert never watches free SQL.")
    comparison: Literal["drop", "spike", "below", "above"]
    threshold: float = Field(
        gt=0,
        description=(
            "For drop/spike, a percentage against the recent baseline. For below/above, an "
            "absolute value."
        ),
    )
    dimension: str | None = Field(
        default=None, description="The period dimension to watch. Defaults to month."
    )
    window_periods: int = Field(
        default=6,
        ge=2,
        le=48,
        description=(
            "How many periods form the baseline. At least two: a one-period window compares a "
            "value to itself and can never fire."
        ),
    )


class AlertEventOut(BaseModel):
    event_id: uuid.UUID
    triggered: bool
    observed: float | None = None
    baseline: float | None = None
    change_pct: float | None = None
    period: str | None = None
    detail: str
    query_id: uuid.UUID | None = None
    created_at: dt.datetime


class AlertOut(BaseModel):
    alert_id: uuid.UUID
    name: str
    metric: str
    dimension: str | None = None
    comparison: str
    threshold: float
    window_periods: int
    status: Literal["active", "paused", "triggered"]
    created_at: dt.datetime
    last_checked_at: dt.datetime | None = None
    last_triggered_at: dt.datetime | None = None
    last_detail: str | None = None
    times_triggered: int = 0


class UpdateAlertRequest(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["active", "paused"]


class AlertCheckOut(BaseModel):
    alert: AlertOut
    event: AlertEventOut


# --- audit --------------------------------------------------------------------


class AuditEntryOut(BaseModel):
    entry_id: int
    actor_label: str
    actor_user_id: uuid.UUID | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
