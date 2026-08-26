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
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from analyst_agent.agent.graph import build_graph, resume_run
from analyst_agent.api import schemas, service
from analyst_agent.config import get_settings
from analyst_agent.db import repository as repo
from analyst_agent.db.engine import assert_read_only, close_pools
from analyst_agent.observability.logging import (
    bound,
    configure_logging,
    get_logger,
    set_redaction_secrets,
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


def _run_or_404(run_id: uuid.UUID) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
    return run


# --- health -----------------------------------------------------------------


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
def ask(payload: schemas.AskRequest, background: BackgroundTasks) -> schemas.AskResponse:
    """Start an investigation. Returns immediately with a run id.

    202 rather than 200: the work has been accepted, not completed. An investigation takes
    minutes and can pause for a human, so holding the connection open for it would both time out
    and make the approval flow impossible. Poll `/v1/runs/{id}` or follow its stream.
    """
    run_id = repo.create_run(payload.question, requested_by=payload.requested_by)
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
def list_runs(limit: int = 25) -> list[schemas.RunOut]:
    return [service.run_view(r["run_id"]) for r in repo.recent_runs(min(limit, 100))]


@app.get("/v1/runs/{run_id}", response_model=schemas.RunOut, tags=["runs"])
def get_run(run_id: uuid.UUID) -> schemas.RunOut:
    _run_or_404(run_id)
    return service.run_view(run_id)


@app.get("/v1/runs/{run_id}/trace", response_model=schemas.TraceOut, tags=["runs"])
def get_trace(run_id: uuid.UUID) -> schemas.TraceOut:
    """Everything the run did, including the queries the guard refused."""
    _run_or_404(run_id)
    return service.trace_view(run_id)


@app.get("/v1/runs/{run_id}/stream", tags=["runs"])
async def stream(run_id: uuid.UUID) -> EventSourceResponse:
    """Progress as it happens.

    Polls rather than listens: the work happens in a background thread in this same process, and
    a poll against an indexed table is simpler and more robust than wiring a notification channel
    through it. The stream closes on a terminal status, and on a parked one — there is nothing
    further to report until a human acts.
    """
    _run_or_404(run_id)

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
                    "data": service.run_view(run_id).model_dump_json(),
                }
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(events())


# --- approvals and clarifications -------------------------------------------


@app.get(
    "/v1/runs/{run_id}/approvals", response_model=list[schemas.ApprovalOut], tags=["approvals"]
)
def list_approvals(run_id: uuid.UUID) -> list[schemas.ApprovalOut]:
    _run_or_404(run_id)
    return service.trace_view(run_id).approvals


def _decide(
    run_id: uuid.UUID, approval_id: uuid.UUID, decision: str, payload: schemas.DecisionRequest
) -> schemas.ApprovalOut:
    _run_or_404(run_id)
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
        (a for a in service.trace_view(run_id).approvals if a.approval_id == approval_id), None
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no approval {approval_id}")
    return found


@app.post(
    "/v1/runs/{run_id}/approvals/{approval_id}/approve",
    response_model=schemas.ApprovalOut,
    tags=["approvals"],
)
def approve(
    run_id: uuid.UUID, approval_id: uuid.UUID, payload: schemas.DecisionRequest
) -> schemas.ApprovalOut:
    return _decide(run_id, approval_id, "approved", payload)


@app.post(
    "/v1/runs/{run_id}/approvals/{approval_id}/reject",
    response_model=schemas.ApprovalOut,
    tags=["approvals"],
)
def reject(
    run_id: uuid.UUID, approval_id: uuid.UUID, payload: schemas.DecisionRequest
) -> schemas.ApprovalOut:
    """Rejection is a first-class path: the run continues and reports what it could establish."""
    return _decide(run_id, approval_id, "rejected", payload)


@app.post("/v1/runs/{run_id}/answer", response_model=schemas.RunOut, tags=["runs"])
def answer_clarification(
    run_id: uuid.UUID, payload: schemas.AnswerRequest, background: BackgroundTasks
) -> schemas.RunOut:
    """Reply to a question the agent asked, and let it carry on."""
    run = _run_or_404(run_id)
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
    return service.run_view(run_id)


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
