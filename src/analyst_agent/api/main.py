"""The FastAPI application.

A question is started in the background and the caller gets a ``run_id`` immediately. An
investigation takes minutes and can pause for a human, so making the caller hold an HTTP
connection open for it would be wrong twice over: it would time out, and it would make the
approval flow impossible.

Every response carries a request id, and every log line inside the request carries it too, so a
report of "my run failed" leads straight to the lines that describe it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import UniqueViolation
from sse_starlette.sse import EventSourceResponse

from analyst_agent.agent.graph import build_graph, resume_after_decision, resume_run
from analyst_agent.alerts import detect as alerts
from analyst_agent.api import schemas, service
from analyst_agent.config import get_settings
from analyst_agent.db import repository as repo
from analyst_agent.db import tenancy
from analyst_agent.db.engine import assert_read_only, close_pools
from analyst_agent.metrics.registry import get_registry
from analyst_agent.observability.logging import (
    bound,
    configure_logging,
    get_logger,
    set_redaction_secrets,
)
from analyst_agent.reports import to_excel, to_pdf
from analyst_agent.reports.snapshot import (
    build_snapshot,
    default_name,
    safe_filename,
)
from analyst_agent.security.crypto import SecretsUnavailableError, redact
from analyst_agent.security.principal import (
    NEEDS,
    AccessDeniedError,
    Principal,
    default_principal,
)

log = get_logger(__name__)

STREAM_POLL_SECONDS = 0.75
TERMINAL_STATUSES = {"completed", "failed", "truncated"}
PARKED_STATUSES = {"clarifying", "awaiting_approval"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    set_redaction_secrets(settings.secret_values)
    configure_logging(settings.log_level, settings.log_format)

    # Refuse to start if the read-only pool is not actually read-only: a mis-pasted DSN should
    # be a startup failure, not an incident discovered later (control C1).
    assert_read_only()

    expired = repo.expire_stale_approvals()
    if expired:
        log.info("expired stale approvals on startup", count=expired)

    resumable = repo.resumable_runs()
    if resumable:
        # Reported rather than auto-resumed. A run parked on a human decision should wait for
        # that human, not be restarted because the service happened to reboot.
        log.info("resumable runs present", count=len(resumable))

    log.info("api ready", environment=settings.environment)
    yield
    close_pools()


app = FastAPI(
    title="AI Data Analyst Agent",
    version="0.5.0",
    summary="Ask a business question; get a conclusion traceable to the queries behind it.",
    lifespan=lifespan,
)


# The interface itself, served from the same origin as the API it calls. Same-origin means no
# CORS to configure and no second process to run - and it keeps the rule that the UI reaches the
# database only through these endpoints, because it has no other way to reach anything.
STATIC = Path(__file__).parent / "static"
if STATIC.is_dir():
    app.mount("/app", StaticFiles(directory=STATIC, html=True), name="app")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Land on the interface rather than on a 404."""
    return RedirectResponse(url="/app/")


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    with bound(request_id=request_id):
        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("unhandled error", path=request.url.path)
            return JSONResponse(
                status_code=500,
                content=schemas.ErrorOut(
                    error="internal_error",
                    detail=str(exc),
                    request_id=request_id,
                ).model_dump(),
                headers={"x-request-id": request_id},
            )
        response.headers["x-request-id"] = request_id
        return response


def _run_or_404(run_id: uuid.UUID, principal: Principal | None = None) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if principal is not None and not principal.in_organization(
        run.get("organization_id") if run else None
    ):
        # 404, not 403: a 403 would confirm the run exists in somebody else's account.
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
    return run


# --- health -----------------------------------------------------------------


# --- who is asking -----------------------------------------------------------


def current_principal(request: Request) -> Principal:
    """Resolve the caller from an ``Authorization: Bearer`` key.

    Two modes, and the difference is a setting rather than a branch somebody can forget. With
    ``REQUIRE_AUTHENTICATION=false`` — the default, and how the container runs for a demo — a
    request with no key is the default organisation's owner, flagged anonymous so the audit trail
    says plainly that nobody proved who they were. With it true there is no anonymous path at all.

    A key that does not resolve is **401 in both modes**: presenting a bad key is different from
    presenting none, and treating them the same would silently downgrade a revoked key to a demo
    session with owner rights.
    """
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""

    if token:
        principal = tenancy.resolve_api_key(token)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="that API key is not valid, or its membership has been removed",
            )
        return principal

    if get_settings().require_authentication:
        raise HTTPException(
            status_code=401,
            detail="an API key is required: Authorization: Bearer <key>",
        )
    return default_principal()


def _entitled(principal: Principal, action: str) -> None:
    """403 for an action the role cannot take.

    403 here, and 404 for a *resource* in another organisation — see the note on
    security.principal.require. The difference is whether the answer confirms something exists.
    """
    try:
        principal.require(action)
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    """Liveness only. Deliberately touches nothing, so it stays true while the DB is down."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readyz() -> dict[str, Any]:
    """Readiness: the database answers and the read-only role is still read-only."""
    try:
        assert_read_only()
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"status": "ready", "read_only_verified": True}


# --- questions --------------------------------------------------------------


def _drive_in_background(run_id: uuid.UUID, question: str, requested_by: str | None) -> None:
    """Run the graph for a run row that already exists.

    The row is created in the request so the caller can be handed an id before any work begins;
    the graph is then driven against that same id rather than creating a second one.
    """
    from analyst_agent.agent.graph import drive
    from analyst_agent.agent.state import initial_state

    thread_id = f"run-{run_id}"
    try:
        state = initial_state(str(run_id), thread_id, question, requested_by)
        with bound(run_id=str(run_id), thread_id=thread_id):
            drive(build_graph(), state, thread_id, run_id)
    except Exception:
        # `drive` already recorded the failure against the run; this only keeps the background
        # task from dying silently.
        log.exception("background run failed", run_id=str(run_id))


@app.post(
    "/v1/questions",
    response_model=schemas.AskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["questions"],
)
def ask(
    payload: schemas.AskRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(current_principal),
) -> schemas.AskResponse:
    """Start an investigation. Returns immediately with a run id.

    202 rather than 200: the work has been accepted, not completed. An investigation takes
    minutes and can pause for a human, so holding the connection open for it would both time out
    and make the approval flow impossible. Poll `/v1/runs/{id}` or follow its stream.
    """
    _entitled(principal, "ask")
    # The organisation comes from the caller, never from the body: a body-supplied tenant id turns
    # "ask in my organisation" into "write into anybody's".
    run_id = repo.create_run(
        payload.question,
        requested_by=payload.requested_by or principal.email or None,
        organization_id=principal.organization_id,
    )
    background.add_task(_drive_in_background, run_id, payload.question, payload.requested_by)

    log.info("question accepted", run_id=str(run_id), question=payload.question)
    return schemas.AskResponse(
        run_id=run_id,
        thread_id=f"run-{run_id}",
        status="received",
        message="accepted; poll /v1/runs/{run_id} or follow /v1/runs/{run_id}/stream",
    )


# --- runs -------------------------------------------------------------------


@app.get("/v1/runs", response_model=list[schemas.RunOut], tags=["runs"])
def list_runs(
    limit: int = 25, principal: Principal = Depends(current_principal)
) -> list[schemas.RunOut]:
    """This organisation's runs, newest first.

    The filter is in the query, not applied to the result: a row fetched and then dropped is a row
    a future refactor can forget to drop.
    """
    _entitled(principal, "read")
    return [
        service.run_view(row["run_id"], organization_id=principal.organization_id)
        for row in repo.recent_runs(
            min(limit, 100), organization_id=principal.organization_id
        )
    ]


@app.get("/v1/runs/{run_id}", response_model=schemas.RunOut, tags=["runs"])
def get_run(
    run_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> schemas.RunOut:
    _entitled(principal, "read")
    _run_or_404(run_id, principal)
    return service.run_view(run_id, organization_id=principal.organization_id)


@app.get("/v1/runs/{run_id}/trace", response_model=schemas.TraceOut, tags=["runs"])
def get_trace(
    run_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> schemas.TraceOut:
    """Everything the run did, including the queries the guard refused."""
    _run_or_404(run_id, principal)
    return service.trace_view(run_id, organization_id=principal.organization_id)


@app.get("/v1/runs/{run_id}/stream", tags=["runs"])
async def stream(
    run_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> EventSourceResponse:
    """Progress as it happens.

    Polls rather than listens: the work happens in a background thread in this same process, and
    a poll against an indexed table is simpler and more robust than wiring a notification channel
    through it. The stream closes on a terminal status, and on a parked one — there is nothing
    further to report until a human acts.
    """
    _run_or_404(run_id, principal)

    async def events() -> AsyncIterator[dict[str, Any]]:
        seen = 0
        while True:
            trace = repo.get_trace(run_id)
            for step in trace["steps"][seen:]:
                yield {
                    "event": "step",
                    "data": schemas.StepOut(
                        seq=step["seq"],
                        node=step["node"],
                        status=step["status"],
                        effort=step.get("effort"),
                        duration_ms=step.get("duration_ms"),
                        summary=step.get("summary"),
                        error=step.get("error"),
                    ).model_dump_json(),
                }
            seen = len(trace["steps"])

            run_status = trace["run"]["status"]
            if run_status in TERMINAL_STATUSES or run_status in PARKED_STATUSES:
                yield {
                    "event": "done",
                    "data": service.run_view(
                        run_id, organization_id=principal.organization_id
                    ).model_dump_json(),
                }
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(events())


# --- approvals and clarifications -------------------------------------------


@app.get(
    "/v1/runs/{run_id}/approvals", response_model=list[schemas.ApprovalOut], tags=["approvals"]
)
def list_approvals(
    run_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> list[schemas.ApprovalOut]:
    _run_or_404(run_id, principal)
    return service.trace_view(run_id, organization_id=principal.organization_id).approvals


def _resume_after_decision(run_id: uuid.UUID) -> None:
    try:
        resume_after_decision(run_id, graph=build_graph())
    except ValueError as exc:
        # Another approval is still outstanding, or there was nothing to act on. Not an error:
        # the run stays parked until every gate it is waiting on has been answered.
        log.info("not resuming yet", run_id=str(run_id), reason=str(exc))
    except Exception:
        log.exception("resume after decision failed", run_id=str(run_id))


def _decide(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    principal: Principal,
    decision: str,
    payload: schemas.DecisionRequest,
    background: BackgroundTasks,
) -> schemas.ApprovalOut:
    _run_or_404(run_id, principal)
    applied = repo.decide_approval(
        approval_id, decision, decided_by=payload.decided_by, decision_reason=payload.reason
    )
    if not applied:
        # Already decided. Refused rather than overwritten: the first decision is the one that
        # was made, and a second one silently replacing it would falsify the audit.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"approval {approval_id} has already been decided"
        )
    found = next(
        (
            a
            for a in service.trace_view(
                run_id, organization_id=principal.organization_id
            ).approvals
            if a.approval_id == approval_id
        ),
        None,
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no approval {approval_id}")

    # Both outcomes carry the run forward. A rejection is not a dead end - the run answers with
    # what it could establish and says what it could not.
    background.add_task(_resume_after_decision, run_id)
    return found


@app.post(
    "/v1/runs/{run_id}/approvals/{approval_id}/approve",
    response_model=schemas.ApprovalOut,
    tags=["approvals"],
)
def approve(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    payload: schemas.DecisionRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(current_principal),
) -> schemas.ApprovalOut:
    _entitled(principal, "decide_approval")
    return _decide(run_id, approval_id, principal, "approved", payload, background)


@app.post(
    "/v1/runs/{run_id}/approvals/{approval_id}/reject",
    response_model=schemas.ApprovalOut,
    tags=["approvals"],
)
def reject(
    run_id: uuid.UUID,
    approval_id: uuid.UUID,
    payload: schemas.DecisionRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(current_principal),
) -> schemas.ApprovalOut:
    """Rejection is a first-class path: the run continues and reports what it could establish."""
    _entitled(principal, "decide_approval")
    return _decide(run_id, approval_id, principal, "rejected", payload, background)


@app.post("/v1/runs/{run_id}/answer", response_model=schemas.RunOut, tags=["runs"])
def answer_clarification(
    run_id: uuid.UUID,
    payload: schemas.AnswerRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(current_principal),
) -> schemas.RunOut:
    """Reply to a question the agent asked, and let it carry on."""
    run = _run_or_404(run_id, principal)
    if run["status"] != "clarifying":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"run {run_id} is {run['status']}, not waiting on a clarification",
        )

    def carry_on() -> None:
        try:
            resume_run(
                run["thread_id"],
                updates={
                    "status": "investigating",
                    "clarifications": [{"question": "", "answer": payload.answer}],
                },
                graph=build_graph(),
            )
        except Exception:
            log.exception("resume failed", run_id=str(run_id))

    background.add_task(carry_on)
    return service.run_view(run_id, organization_id=principal.organization_id)


# --- catalogue --------------------------------------------------------------


@app.get("/v1/metrics", response_model=list[schemas.MetricOut], tags=["catalogue"])
def metrics() -> list[schemas.MetricOut]:
    """The approved definitions. A term not here has none, and the agent will say so."""
    return service.metrics_view()


@app.get("/v1/schema", tags=["catalogue"])
def schema() -> dict[str, Any]:
    """What is queryable, with restricted columns flagged rather than hidden."""
    from analyst_agent.sql_guard.catalog import load_catalog
    from analyst_agent.sql_guard.policy import SENSITIVE_BY_KEY

    catalog = load_catalog()
    objects = []
    for schema_name in sorted(catalog.schemas):
        for obj in sorted(catalog.objects.get(schema_name, frozenset())):
            columns = sorted(catalog.columns_of(schema_name, obj))
            objects.append(
                {
                    "name": f"{schema_name}.{obj}",
                    "columns": [
                        {
                            "name": c,
                            "restricted": (schema_name, obj, c) in SENSITIVE_BY_KEY,
                        }
                        for c in columns
                    ],
                }
            )
    return {"schemas": sorted(catalog.schemas), "objects": objects}


# --- dashboard --------------------------------------------------------------


@app.get("/v1/dashboard/summary", response_model=schemas.DashboardOut, tags=["dashboard"])
def dashboard_summary(
    recent: int = 6, principal: Principal = Depends(current_principal)
) -> schemas.DashboardOut:
    """Everything the dashboard shows, in one response.

    One endpoint rather than six: a dashboard assembled from six requests shows six different
    moments, and the totals then disagree with the list beneath them for no reason the reader can
    see. `recent` bounds the three lists; the counts are always over everything.
    """
    recent = max(1, min(recent, 25))
    _entitled(principal, "read")
    return schemas.DashboardOut(
        **repo.dashboard_summary(recent=recent, organization_id=principal.organization_id)
    )


# --- saved reports ----------------------------------------------------------


@app.post(
    "/v1/reports",
    response_model=schemas.ReportOut,
    status_code=status.HTTP_201_CREATED,
    tags=["reports"],
)
def save_report(
    payload: schemas.SaveReportRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.ReportOut:
    """Freeze a finished run as a report.

    Refused for a run with no answer: a report whose body is empty is a filename, and it would
    sit in the list looking like a result.
    """
    _entitled(principal, "save_report")
    try:
        trace = repo.get_trace(payload.run_id, organization_id=principal.organization_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no such run: {payload.run_id}") from None

    stored = (trace.get("run") or {}).get("answer")
    if not stored:
        raise HTTPException(
            status_code=409,
            detail="that run has no answer yet, so there is nothing to save as a report",
        )

    snapshot = build_snapshot(trace, stored)
    name = (payload.name or "").strip() or default_name(snapshot["question"])
    # The organisation and the saver both come from the caller. Without this the report landed in
    # the default organisation and was invisible to the person who saved it - and visible to a
    # tenant that had nothing to do with it. Caught by the isolation tests, which is what they are
    # for.
    report_id = repo.save_report(
        payload.run_id,
        name,
        snapshot,
        created_by=payload.saved_by or principal.email or None,
        organization_id=principal.organization_id,
        saved_by_user_id=principal.user_id,
    )
    tenancy.audit(principal, "report.save", "report", str(report_id), name=name)
    saved = repo.get_report(report_id, organization_id=principal.organization_id)
    assert saved is not None  # noqa: S101 - just inserted, inside the same transaction boundary
    return schemas.ReportOut(**saved)


@app.get("/v1/reports", response_model=list[schemas.ReportSummaryOut], tags=["reports"])
def list_reports(
    limit: int = 50, principal: Principal = Depends(current_principal)
) -> list[schemas.ReportSummaryOut]:
    """Saved reports, newest first, without their snapshots.

    The snapshot of a long run is large; forty of them would be megabytes to render a page of
    names. The counts a list needs are read out of the snapshot in SQL instead.
    """
    _entitled(principal, "read")
    rows = repo.list_reports(
        limit=max(1, min(limit, 200)),
        organization_id=principal.organization_id,
        viewer_user_id=principal.user_id,
    )
    return [
        schemas.ReportSummaryOut(
            report_id=row["report_id"],
            run_id=row["run_id"],
            name=row["name"],
            question=row.get("question"),
            created_by=row.get("created_by"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confidence_score=int(row["confidence_score"]) if row.get("confidence_score") else None,
            charts=int(row.get("charts") or 0),
            queries=int(row.get("queries") or 0),
        )
        for row in rows
    ]


@app.get("/v1/reports/{report_id}", response_model=schemas.ReportOut, tags=["reports"])
def read_report(
    report_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> schemas.ReportOut:
    return schemas.ReportOut(**_report_or_404(report_id, principal))


@app.patch("/v1/reports/{report_id}", response_model=schemas.ReportOut, tags=["reports"])
def rename_report(
    report_id: uuid.UUID,
    payload: schemas.RenameReportRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.ReportOut:
    """Rename a report. The name is the only mutable field - see db/migrations/004."""
    _entitled(principal, "save_report")
    if not repo.rename_report(
        report_id, payload.name, organization_id=principal.organization_id
    ):
        raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
    return schemas.ReportOut(**_report_or_404(report_id, principal))


@app.delete("/v1/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["reports"])
def delete_report(
    report_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    _entitled(principal, "delete_report")
    if not repo.delete_report(report_id, organization_id=principal.organization_id):
        raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- exports ----------------------------------------------------------------


@app.get("/v1/reports/{report_id}/export.pdf", tags=["reports"])
def export_pdf(
    report_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    """The report as a PDF: conclusion, confidence with its factors, findings, evidence, SQL."""
    report = _report_or_404(report_id, principal)
    return _download(
        to_pdf(report), "application/pdf", safe_filename(report["name"], "pdf")
    )


@app.get("/v1/reports/{report_id}/export.xlsx", tags=["reports"])
def export_excel(
    report_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    """The report as a workbook, one sheet per kind of thing, SQL in a column."""
    report = _report_or_404(report_id, principal)
    return _download(
        to_excel(report),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        safe_filename(report["name"], "xlsx"),
    )


@app.get("/v1/charts/{chart_id}/export.png", tags=["reports"])
def export_chart_png(
    chart_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    """One chart as the PNG that was rendered when it was built.

    Not re-rendered on request: the frame it came from may have been evicted by then, and a
    picture regenerated from a spec without its data would differ from the one the answer was
    written against - under the same id.
    """
    _entitled(principal, "read")
    found = repo.chart_png(chart_id, organization_id=principal.organization_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"no stored image for chart {chart_id}",
        )
    png, title = found
    return _download(png, "image/png", safe_filename(title, "png"))


def _report_or_404(report_id: uuid.UUID, principal: Principal) -> dict[str, Any]:
    """A report, or a 404 - including when it belongs to another organisation.

    404 rather than 403 on purpose. Answering 403 would confirm that the report exists, and a
    sequence of those confirmations is an enumeration of somebody else's work.
    """
    report = repo.get_report(report_id, organization_id=principal.organization_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
    return report


def _download(payload: bytes, media_type: str, filename: str) -> Response:
    """A file the browser saves rather than renders.

    `Content-Length` is set explicitly so a download shows real progress instead of an unbounded
    spinner, which on a slow connection is indistinguishable from a hang.
    """
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "content-length": str(len(payload)),
        },
    )


@app.get("/v1/me", response_model=schemas.WhoAmIOut, tags=["tenancy"])
def whoami(principal: Principal = Depends(current_principal)) -> schemas.WhoAmIOut:
    """What the server thinks the caller is. The page uses this to hide what it must not offer."""
    organization = tenancy.get_organization(principal.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="that organisation no longer exists")
    return schemas.WhoAmIOut(
        organization=schemas.OrganizationOut(**organization),
        user_id=principal.user_id,
        email=principal.email,
        role=principal.role,
        authenticated=not principal.anonymous,
        permissions=sorted(action for action in NEEDS if principal.can(action)),
    )


@app.post(
    "/v1/organizations",
    response_model=schemas.OrganizationOut,
    status_code=status.HTTP_201_CREATED,
    tags=["tenancy"],
)
def create_organization(
    payload: schemas.CreateOrganizationRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.OrganizationOut:
    """Create an organisation with its first owner.

    Open to any authenticated caller on purpose: this is the sign-up path, and gating it behind
    membership of an existing organisation would mean nobody could ever create the first one.
    """
    organization_id, _owner_user_id = tenancy.create_organization(payload.name, str(payload.owner_email))
    tenancy.audit(
        principal,
        "organization.create",
        "organization",
        str(organization_id),
        name=payload.name,
        owner=str(payload.owner_email),
    )
    created = tenancy.get_organization(organization_id)
    assert created is not None  # noqa: S101 - inserted immediately above
    return schemas.OrganizationOut(**created)


# --- team --------------------------------------------------------------------


@app.get("/v1/team", response_model=schemas.TeamOut, tags=["team"])
def read_team(principal: Principal = Depends(current_principal)) -> schemas.TeamOut:
    """The team and each member's activity. Any member may see who they work with."""
    _entitled(principal, "read")
    organization = tenancy.get_organization(principal.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="that organisation no longer exists")
    return schemas.TeamOut(
        organization=schemas.OrganizationOut(**organization),
        members=[
            schemas.MemberOut(**member)
            for member in tenancy.list_members(principal.organization_id)
        ],
    )


@app.post(
    "/v1/team/invite",
    response_model=schemas.InviteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["team"],
)
def invite_member(
    payload: schemas.InviteRequest, principal: Principal = Depends(current_principal)
) -> schemas.InviteOut:
    """Invite somebody into the caller's organisation.

    The organisation comes from the *caller*, never from the request body. A body-supplied
    organisation id is how a multi-tenant system grows a hole: it turns "invite to my team" into
    "invite myself to anybody's team".
    """
    _entitled(principal, "manage_team")
    user_id, created = tenancy.invite_member(
        principal.organization_id, str(payload.email), payload.role, principal.user_id
    )
    tenancy.audit(
        principal,
        "team.invite",
        "user",
        str(user_id),
        email=str(payload.email),
        role=payload.role,
        new_member=created,
    )
    return schemas.InviteOut(
        user_id=user_id,
        email=str(payload.email).lower(),
        role=payload.role,
        created=created,
        message=(
            "added to the organisation"
            if created
            else "already a member; their role has been set to " + payload.role
        ),
    )


@app.patch(
    "/v1/team/member/{user_id}", response_model=schemas.TeamOut, tags=["team"]
)
def update_member(
    user_id: uuid.UUID,
    payload: schemas.UpdateMemberRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.TeamOut:
    """Change a role, or remove a member.

    Three refusals, and each one exists because the alternative is an organisation nobody can
    administer:

    * the **last owner** cannot be removed or demoted — an organisation with no owner cannot
      appoint one;
    * a caller cannot **remove themselves** while they are the last owner, for the same reason;
    * a member of another organisation is a **404**, not a 403, so a probe cannot enumerate who
      belongs where.
    """
    _entitled(principal, "manage_team")

    existing = tenancy.member(principal.organization_id, user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="that person is not in this organisation")

    losing_an_owner = existing["role"] == "owner" and (
        payload.remove or (payload.role and payload.role != "owner")
    )
    if losing_an_owner and tenancy.count_owners(principal.organization_id) <= 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "this is the last owner. Promote somebody else to owner first, or the "
                "organisation would be left with nobody who can administer it."
            ),
        )

    if payload.remove:
        tenancy.remove_member(principal.organization_id, user_id)
        tenancy.audit(
            principal, "team.remove", "user", str(user_id), email=existing.get("email")
        )
    elif payload.role:
        tenancy.set_member_role(principal.organization_id, user_id, payload.role)
        tenancy.audit(
            principal,
            "team.role_change",
            "user",
            str(user_id),
            email=existing.get("email"),
            was=existing["role"],
            now=payload.role,
        )
    else:
        raise HTTPException(
            status_code=422, detail="pass a role to change, or remove: true"
        )

    return read_team(principal)


@app.get("/v1/team/keys", response_model=list[schemas.ApiKeyOut], tags=["team"])
def list_api_keys(
    principal: Principal = Depends(current_principal),
) -> list[schemas.ApiKeyOut]:
    _entitled(principal, "issue_key")
    return [
        schemas.ApiKeyOut(**key) for key in tenancy.list_api_keys(principal.organization_id)
    ]


@app.post(
    "/v1/team/keys",
    response_model=schemas.IssuedApiKeyOut,
    status_code=status.HTTP_201_CREATED,
    tags=["team"],
)
def issue_api_key(
    payload: schemas.IssueApiKeyRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.IssuedApiKeyOut:
    """Mint a key for a member. The token is in this response and nowhere else, ever."""
    _entitled(principal, "issue_key")

    target = principal.user_id
    email = principal.email
    if payload.email:
        found = next(
            (
                member
                for member in tenancy.list_members(principal.organization_id)
                if member["email"] == str(payload.email).lower()
            ),
            None,
        )
        if found is None:
            raise HTTPException(
                status_code=404,
                detail="that person is not in this organisation; invite them first",
            )
        target, email = found["user_id"], found["email"]

    key_id, token = tenancy.issue_api_key(principal.organization_id, target, payload.name)
    tenancy.audit(principal, "key.issue", "api_key", str(key_id), name=payload.name, for_=email)
    issued = next(
        key
        for key in tenancy.list_api_keys(principal.organization_id)
        if str(key["key_id"]) == str(key_id)
    )
    return schemas.IssuedApiKeyOut(**issued, token=token)


@app.delete(
    "/v1/team/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["team"]
)
def revoke_api_key(
    key_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    _entitled(principal, "issue_key")
    if not tenancy.revoke_api_key(principal.organization_id, key_id):
        raise HTTPException(status_code=404, detail="no such key")
    tenancy.audit(principal, "key.revoke", "api_key", str(key_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- data sources ------------------------------------------------------------


@app.get("/v1/data-sources", response_model=list[schemas.DataSourceOut], tags=["data sources"])
def list_data_sources(
    principal: Principal = Depends(current_principal),
) -> list[schemas.DataSourceOut]:
    """Sources for this organisation, credentials excluded at the SQL level."""
    _entitled(principal, "read")
    return [
        schemas.DataSourceOut(**source)
        for source in tenancy.list_data_sources(principal.organization_id)
    ]


@app.post(
    "/v1/data-sources",
    response_model=schemas.DataSourceOut,
    status_code=status.HTTP_201_CREATED,
    tags=["data sources"],
)
def create_data_source(
    payload: schemas.CreateDataSourceRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.DataSourceOut:
    """Register a source. The configuration is encrypted before it is stored.

    A missing ``SECRETS_KEY`` is a **503, not a 500**: the service is working and the deployment is
    incomplete, and the message says which. Storing the configuration in the clear "for now" is the
    one thing this endpoint must not do.
    """
    _entitled(principal, "manage_data_sources")
    try:
        data_source_id = tenancy.create_data_source(
            principal.organization_id,
            payload.name,
            payload.type,
            payload.config,
            principal.user_id,
        )
    except SecretsUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except UniqueViolation:
        raise HTTPException(
            status_code=409, detail="a data source with that name already exists here"
        ) from None

    tenancy.audit(
        principal,
        "data_source.create",
        "data_source",
        str(data_source_id),
        name=payload.name,
        type=payload.type,
        # The redacted summary, never the config: an audit entry holding a password would defeat
        # the encryption on the row it describes.
        summary=redact(payload.config, payload.type),
    )
    created = tenancy.get_data_source(principal.organization_id, data_source_id)
    assert created is not None  # noqa: S101 - inserted immediately above
    return schemas.DataSourceOut(**created)


@app.get(
    "/v1/data-sources/{data_source_id}",
    response_model=schemas.DataSourceOut,
    tags=["data sources"],
)
def read_data_source(
    data_source_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> schemas.DataSourceOut:
    _entitled(principal, "read")
    source = tenancy.get_data_source(principal.organization_id, data_source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="no such data source")
    return schemas.DataSourceOut(**source)


@app.delete(
    "/v1/data-sources/{data_source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["data sources"],
)
def delete_data_source(
    data_source_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    _entitled(principal, "manage_data_sources")
    if not tenancy.delete_data_source(principal.organization_id, data_source_id):
        raise HTTPException(status_code=404, detail="no such data source")
    tenancy.audit(principal, "data_source.delete", "data_source", str(data_source_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- report sharing ----------------------------------------------------------


@app.patch(
    "/v1/reports/{report_id}/visibility",
    response_model=schemas.ReportOut,
    tags=["sharing"],
)
def set_visibility(
    report_id: uuid.UUID,
    payload: schemas.VisibilityRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.ReportOut:
    _entitled(principal, "share_report")
    if not tenancy.set_report_visibility(
        principal.organization_id, report_id, payload.visibility
    ):
        raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
    tenancy.audit(
        principal, "report.visibility", "report", str(report_id), visibility=payload.visibility
    )
    return schemas.ReportOut(**_report_or_404(report_id, principal))


@app.post(
    "/v1/reports/{report_id}/shares",
    response_model=schemas.IssuedShareOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sharing"],
)
def share_report(
    report_id: uuid.UUID,
    payload: schemas.ShareRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> schemas.IssuedShareOut:
    """Create a link. The token is returned once and stored only as a hash."""
    _entitled(principal, "share_report")
    created = tenancy.create_share(
        principal.organization_id,
        report_id,
        payload.audience,
        principal.user_id,
        payload.expires_in_hours,
    )
    if created is None:
        raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
    share_id, token, _expires_at = created
    tenancy.audit(
        principal,
        "report.share",
        "report",
        str(report_id),
        audience=payload.audience,
        expires_in_hours=payload.expires_in_hours,
    )
    row = next(
        share
        for share in tenancy.list_shares(principal.organization_id, report_id)
        if str(share["share_id"]) == str(share_id)
    )
    return schemas.IssuedShareOut(
        **row, url=f"{str(request.base_url).rstrip('/')}/v1/shared/{token}", token=token
    )


@app.get(
    "/v1/reports/{report_id}/shares", response_model=list[schemas.ShareOut], tags=["sharing"]
)
def list_shares(
    report_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)
) -> list[schemas.ShareOut]:
    """Existing links, by prefix. The token is not recoverable, which is the point of hashing it."""
    _entitled(principal, "read")
    base = str(request.base_url).rstrip("/")
    return [
        schemas.ShareOut(**share, url=f"{base}/v1/shared/{share['prefix']}…")
        for share in tenancy.list_shares(principal.organization_id, report_id)
    ]


@app.delete(
    "/v1/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sharing"]
)
def revoke_share(
    share_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    _entitled(principal, "share_report")
    if not tenancy.revoke_share(principal.organization_id, share_id):
        raise HTTPException(status_code=404, detail="no such share, or it is already revoked")
    tenancy.audit(principal, "report.share_revoke", "share", str(share_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/shared/{token}", response_model=schemas.SharedReportOut, tags=["sharing"])
def read_shared(token: str, request: Request) -> schemas.SharedReportOut:
    """Read a report through a link.

    The only unauthenticated read path in the system, and deliberately the narrowest: no
    organisation id, no run id, no saver. A **team** link additionally requires the reader to be in
    the organisation, so "share with my team" does not quietly mean "share with the internet".

    An expired, revoked or unknown token are all the same 404. Distinguishing them would tell a
    holder of a dead link whether it ever existed.
    """
    shared = tenancy.resolve_share(token)
    if shared is None:
        raise HTTPException(
            status_code=404, detail="that link is not valid, has been revoked, or has expired"
        )

    if shared["audience"] == "team":
        principal = current_principal(request)
        if not principal.in_organization(uuid.UUID(str(shared["organization_id"]))):
            raise HTTPException(
                status_code=404,
                detail="that link is not valid, has been revoked, or has expired",
            )

    return schemas.SharedReportOut(
        name=shared["name"],
        audience=shared["audience"],
        created_at=shared["created_at"],
        expires_at=shared.get("expires_at"),
        snapshot=shared["snapshot"],
    )


# --- alerts ------------------------------------------------------------------


@app.get("/v1/alerts", response_model=list[schemas.AlertOut], tags=["alerts"])
def list_alerts(principal: Principal = Depends(current_principal)) -> list[schemas.AlertOut]:
    _entitled(principal, "read")
    return [
        schemas.AlertOut(**alert) for alert in tenancy.list_alerts(principal.organization_id)
    ]


@app.post(
    "/v1/alerts",
    response_model=schemas.AlertOut,
    status_code=status.HTTP_201_CREATED,
    tags=["alerts"],
)
def create_alert(
    payload: schemas.CreateAlertRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.AlertOut:
    """Watch an approved metric.

    The metric must be in the registry: an alert runs unattended, so a definition somebody invented
    once would keep firing about a number nobody agreed on.
    """
    _entitled(principal, "manage_alerts")
    registry = get_registry()
    if payload.metric not in set(registry.names):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{payload.metric!r} is not an approved metric. Approved: "
                + ", ".join(sorted(registry.names))
            ),
        )
    try:
        alert_id = tenancy.create_alert(
            principal.organization_id,
            payload.name,
            payload.metric,
            payload.comparison,
            payload.threshold,
            payload.dimension,
            payload.window_periods,
            principal.user_id,
        )
    except UniqueViolation:
        raise HTTPException(
            status_code=409, detail="an alert with that name already exists here"
        ) from None

    tenancy.audit(
        principal,
        "alert.create",
        "alert",
        str(alert_id),
        metric=payload.metric,
        comparison=payload.comparison,
        threshold=payload.threshold,
    )
    return _alert_or_404(principal, alert_id)


@app.patch("/v1/alerts/{alert_id}", response_model=schemas.AlertOut, tags=["alerts"])
def update_alert(
    alert_id: uuid.UUID,
    payload: schemas.UpdateAlertRequest,
    principal: Principal = Depends(current_principal),
) -> schemas.AlertOut:
    _entitled(principal, "manage_alerts")
    if not tenancy.set_alert_status(principal.organization_id, alert_id, payload.status):
        raise HTTPException(status_code=404, detail="no such alert")
    tenancy.audit(principal, "alert.status", "alert", str(alert_id), status=payload.status)
    return _alert_or_404(principal, alert_id)


@app.delete("/v1/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["alerts"])
def delete_alert(
    alert_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> Response:
    _entitled(principal, "manage_alerts")
    if not tenancy.delete_alert(principal.organization_id, alert_id):
        raise HTTPException(status_code=404, detail="no such alert")
    tenancy.audit(principal, "alert.delete", "alert", str(alert_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/alerts/{alert_id}/check", response_model=schemas.AlertCheckOut, tags=["alerts"]
)
def check_alert(
    alert_id: uuid.UUID, principal: Principal = Depends(current_principal)
) -> schemas.AlertCheckOut:
    """Evaluate one alert now, and record the outcome either way.

    Both outcomes are recorded: keeping only the breaches would leave no way to tell a quiet alert
    from one that stopped running, and "we were never alerted" is the sentence that follows the
    second.
    """
    _entitled(principal, "manage_alerts")
    alert = tenancy.get_alert(principal.organization_id, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="no such alert")

    verdict, query_id = alerts.evaluate(alert, principal.organization_id)
    event_id = tenancy.record_alert_event(
        alert_id,
        triggered=verdict.triggered,
        detail=verdict.detail,
        observed=verdict.observed,
        baseline=verdict.baseline,
        change_pct=verdict.change_pct,
        period=verdict.period,
        query_id=query_id,
    )
    if verdict.triggered:
        tenancy.audit(
            principal,
            "alert.triggered",
            "alert",
            str(alert_id),
            metric=alert["metric"],
            detail=verdict.detail,
        )

    events = tenancy.alert_events(principal.organization_id, alert_id, limit=1)
    return schemas.AlertCheckOut(
        alert=_alert_or_404(principal, alert_id),
        event=schemas.AlertEventOut(
            **next(event for event in events if str(event["event_id"]) == str(event_id))
        ),
    )


@app.get(
    "/v1/alerts/{alert_id}/events",
    response_model=list[schemas.AlertEventOut],
    tags=["alerts"],
)
def alert_history(
    alert_id: uuid.UUID, limit: int = 20, principal: Principal = Depends(current_principal)
) -> list[schemas.AlertEventOut]:
    _entitled(principal, "read")
    if tenancy.get_alert(principal.organization_id, alert_id) is None:
        raise HTTPException(status_code=404, detail="no such alert")
    return [
        schemas.AlertEventOut(**event)
        for event in tenancy.alert_events(
            principal.organization_id, alert_id, limit=max(1, min(limit, 200))
        )
    ]


def _alert_or_404(principal: Principal, alert_id: uuid.UUID) -> schemas.AlertOut:
    found = next(
        (
            alert
            for alert in tenancy.list_alerts(principal.organization_id)
            if str(alert["alert_id"]) == str(alert_id)
        ),
        None,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="no such alert")
    return schemas.AlertOut(**found)


# --- audit -------------------------------------------------------------------


@app.get("/v1/audit", response_model=list[schemas.AuditEntryOut], tags=["security"])
def read_audit(
    limit: int = 100,
    action: str | None = None,
    principal: Principal = Depends(current_principal),
) -> list[schemas.AuditEntryOut]:
    """The organisation's audit trail, newest first.

    Admin and above: the trail names who did what, and that is not a viewer's business. Append-only
    — there is no endpoint that edits or deletes an entry, because a trail somebody can edit is not
    one.
    """
    _entitled(principal, "read_audit")
    return [
        schemas.AuditEntryOut(**entry)
        for entry in tenancy.audit_entries(
            principal.organization_id, limit=max(1, min(limit, 500)), action=action
        )
    ]
