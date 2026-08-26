"""Human approval: requesting it, and honouring it once it arrives.

Control C8. Four gates, and the mechanism is the same for all of them: the work stops, a row is
written that a person can read, and the run stays resumable until they decide.

The part that matters most is how an approval is *honoured*. ``sql_runner`` will execute an
escalated statement only when it is handed an ``approval_id`` that the **database** says is
approved and whose recorded SQL matches the statement being run. It is deliberately not a flag
the caller can set: a bug in a node, or a model that learned to pass `approved=true`, must not be
able to manufacture consent. The check is against a row a human wrote to, and against the exact
text they saw.

Rejection is a first-class path, not an error. The run continues and reports what it could
establish without the rejected action, and says plainly what it could not.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from analyst_agent.config import Settings, get_settings
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

QUERY_GATE_KINDS = {"expensive_query", "sensitive_column"}


def statement_fingerprint(sql: str) -> str:
    """A stable digest of the exact statement a reviewer was shown.

    Whitespace-normalised only. Deliberately *not* semantic: approving one statement must not
    silently approve a different one that happens to mean the same thing, because what the
    reviewer agreed to was the text in front of them.
    """
    return hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()


def gate_kind_for(reasons: list[str], sensitive_columns: list[str]) -> str:
    """Which gate an escalation belongs to, so the reviewer sees the right framing."""
    if sensitive_columns or any(r.startswith("sensitive_column") for r in reasons):
        return "sensitive_column"
    return "expensive_query"


def request_query_approval(
    run_id: uuid.UUID,
    sql: str,
    purpose: str,
    reasons: list[str],
    sensitive_columns: list[str],
    estimated_cost: float | None,
    query_id: uuid.UUID | None,
    settings: Settings | None = None,
) -> uuid.UUID:
    """Record that a query needs a decision, with everything the reviewer needs to make it."""
    settings = settings or get_settings()
    kind = gate_kind_for(reasons, sensitive_columns)

    approval_id = repo.create_approval(
        run_id,
        kind=kind,
        reason="; ".join(reasons) or "the guard escalated this statement",
        payload={
            "sql": sql,
            "purpose": purpose,
            "fingerprint": statement_fingerprint(sql),
            "reasons": reasons,
            "sensitive_columns": sensitive_columns,
            "estimated_cost": estimated_cost,
            "query_id": str(query_id) if query_id else None,
        },
        timeout_seconds=settings.approval_timeout_seconds,
    )
    log.info(
        "approval requested",
        run_id=str(run_id),
        approval_id=str(approval_id),
        kind=kind,
        sensitive_columns=sensitive_columns,
    )
    return approval_id


def request_budget_extension(
    run_id: uuid.UUID,
    reason: str,
    established: list[str],
    outstanding: list[str],
    spent: dict[str, Any],
    settings: Settings | None = None,
) -> uuid.UUID:
    """Approval point 3: the investigation has run out of budget and wants more.

    The payload carries what has been established and what remains untested, because "may I have
    more budget" is not a decidable question without them.
    """
    settings = settings or get_settings()
    approval_id = repo.create_approval(
        run_id,
        kind="budget_extension",
        reason=reason,
        payload={
            "established": established,
            "outstanding": outstanding,
            "spent": spent,
        },
        timeout_seconds=settings.approval_timeout_seconds,
    )
    log.info("budget extension requested", run_id=str(run_id), approval_id=str(approval_id))
    return approval_id


def approved_statement(
    run_id: uuid.UUID, approval_id: uuid.UUID, sql: str
) -> tuple[bool, str | None]:
    """Whether this exact statement may now run under this approval.

    Four conditions, all checked against the stored row rather than against anything the caller
    said: the approval exists, it belongs to this run, a human approved it, and the statement
    matches the one they were shown.
    """
    approvals = {a["approval_id"]: a for a in repo.get_trace(run_id)["approvals"]}
    approval = approvals.get(approval_id)

    if approval is None:
        return False, f"no approval {approval_id} on this run"
    if approval["kind"] not in QUERY_GATE_KINDS:
        return False, f"approval {approval_id} is a {approval['kind']}, not a query approval"
    if approval["status"] != "approved":
        return False, f"approval {approval_id} is {approval['status']}"

    expected = approval["payload"].get("fingerprint")
    if expected and expected != statement_fingerprint(sql):
        # The statement changed after a human agreed to it. Refusing is the only safe answer:
        # consent was given to a specific text, not to a slot.
        return False, (
            f"approval {approval_id} was granted for a different statement than the one "
            "being run"
        )
    return True, None


def pending_for_run(run_id: uuid.UUID) -> list[dict[str, Any]]:
    return repo.pending_approvals(run_id)


def resolve_expired(run_id: uuid.UUID | None = None) -> int:
    """Auto-reject anything past its deadline.

    A timeout is *recorded* as a decision with its reason rather than inferred from the clock at
    read time, so the audit says what happened rather than leaving a reader to work it out.
    """
    expired = repo.expire_stale_approvals()
    if expired:
        log.info("approvals timed out", count=expired, run_id=str(run_id) if run_id else None)
    return expired
