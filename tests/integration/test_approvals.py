"""Control C8: human approval, and surviving the wait.

The claims under test:

* An escalated query writes a row a person can actually read, and the run parks.
* Approval is honoured only when the **database** says a human approved *this exact statement*.
  A caller cannot approve its own query by passing an id.
* Both outcomes carry the run forward. A rejection is a first-class path, not a dead end.
* The wait survives a process restart, which is the whole reason the checkpoint exists.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from analyst_agent.agent import approvals
from analyst_agent.agent.graph import build_graph, resume_after_decision, start_run
from analyst_agent.db import repository as repo
from analyst_agent.tools.registry import get_tool_registry
from tests.fakes import ScriptedLLM
from tests.integration.test_graph_linear import script

pytestmark = pytest.mark.integration

SENSITIVE_SQL = "SELECT email FROM analytics.customer_contact"


def _cleanup(rw_dsn: str, run_id: str) -> None:
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (uuid.UUID(run_id),))


@pytest.fixture
def parked(rw_dsn: str, seeded: None):
    """A run stopped at a sensitive-column approval, with the approval row written."""
    llm = ScriptedLLM(script(sql=SENSITIVE_SQL))
    state = start_run("Who are our customers?", graph=build_graph(llm=llm))
    yield state
    _cleanup(rw_dsn, state["run_id"])


# --- the gate writes something a person can read ----------------------------


def test_an_escalated_query_creates_an_approval_a_person_can_read(parked: dict[str, Any]) -> None:
    run_id = uuid.UUID(parked["run_id"])
    assert parked["status"] == "awaiting_approval"

    pending = repo.pending_approvals(run_id)
    assert len(pending) == 1
    approval = pending[0]
    assert approval["kind"] == "sensitive_column"
    # Everything needed to decide, without going and reading the code.
    assert approval["payload"]["sql"] == SENSITIVE_SQL
    assert approval["payload"]["purpose"]
    assert "analytics.customer_contact.email" in approval["payload"]["sensitive_columns"]
    assert approval["reason"]
    assert approval["expires_at"] is not None


def test_the_run_stays_resumable_while_it_waits(parked: dict[str, Any]) -> None:
    run_id = uuid.UUID(parked["run_id"])
    assert run_id in {r["run_id"] for r in repo.resumable_runs()}
    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_executed"] == 0
    assert "synthesize" not in [s["node"] for s in trace["steps"]]


# --- approval cannot be manufactured ----------------------------------------


def test_a_caller_cannot_approve_its_own_query(parked: dict[str, Any]) -> None:
    """The id is checked against a stored decision, not taken as consent."""
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]

    # Still pending: passing the id must not clear it.
    result = get_tool_registry().invoke(
        "sql_runner",
        {
            "sql": SENSITIVE_SQL,
            "purpose": "trying to run it anyway",
            "row_limit": None,
            "approval_id": str(approval_id),
        },
        run_id,
    )
    assert result.refused
    assert result.data["verdict"] == "escalated"
    assert "pending" in result.data["approval_error"]


def test_an_invented_approval_id_is_refused(parked: dict[str, Any]) -> None:
    run_id = uuid.UUID(parked["run_id"])
    result = get_tool_registry().invoke(
        "sql_runner",
        {
            "sql": SENSITIVE_SQL,
            "purpose": "invented id",
            "row_limit": None,
            "approval_id": str(uuid.uuid4()),
        },
        run_id,
    )
    assert result.refused
    assert "no approval" in result.data["approval_error"]


def test_an_approval_does_not_cover_a_different_statement(parked: dict[str, Any]) -> None:
    """Consent was given to a specific text, not to a slot in the flow."""
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]
    repo.decide_approval(approval_id, "approved", decided_by="reviewer@example.com")

    swapped = "SELECT phone, street_address FROM analytics.customer_contact"
    result = get_tool_registry().invoke(
        "sql_runner",
        {
            "sql": swapped,
            "purpose": "swapped after approval",
            "row_limit": None,
            "approval_id": str(approval_id),
        },
        run_id,
    )
    assert result.refused
    assert "different statement" in result.data["approval_error"]


def test_whitespace_does_not_defeat_the_fingerprint() -> None:
    a = "SELECT   email\n  FROM analytics.customer_contact"
    b = "SELECT email FROM analytics.customer_contact"
    assert approvals.statement_fingerprint(a) == approvals.statement_fingerprint(b)


def test_a_different_statement_has_a_different_fingerprint() -> None:
    assert approvals.statement_fingerprint("SELECT email FROM t") != approvals.statement_fingerprint(
        "SELECT phone FROM t"
    )


# --- both outcomes carry the run forward ------------------------------------


def test_approving_lets_the_same_statement_run(parked: dict[str, Any], rw_dsn: str) -> None:
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]
    repo.decide_approval(
        approval_id, "approved", decided_by="reviewer@example.com", decision_reason="one-off"
    )

    llm = ScriptedLLM(script(sql=SENSITIVE_SQL))
    resumed = resume_after_decision(run_id, graph=build_graph(llm=llm))

    assert resumed["status"] == "completed"
    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_executed"] == 1, "the approved statement actually ran"
    assert trace["summary"]["approvals_pending"] == 0


def test_rejecting_still_produces_an_answer(parked: dict[str, Any]) -> None:
    """Refusal is a first-class path: the run says what it could establish, and what it could not."""
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]
    repo.decide_approval(
        approval_id,
        "rejected",
        decided_by="reviewer@example.com",
        decision_reason="personal data is out of scope for this question",
    )

    llm = ScriptedLLM(script(sql=SENSITIVE_SQL))
    resumed = resume_after_decision(run_id, graph=build_graph(llm=llm))

    assert resumed["status"] == "completed"
    assert resumed["answer"] is not None
    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_executed"] == 0, "the rejected statement never ran"
    # The refusal is in the run's own record of what went wrong, not silently dropped.
    assert any(e["kind"] == "ApprovalRefused" for e in resumed.get("errors", []))


def test_resuming_while_a_decision_is_outstanding_is_refused(parked: dict[str, Any]) -> None:
    run_id = uuid.UUID(parked["run_id"])
    with pytest.raises(ValueError, match="undecided"):
        resume_after_decision(run_id, graph=build_graph(llm=ScriptedLLM(script())))


# --- surviving the wait -----------------------------------------------------


def test_the_wait_survives_a_restart(parked: dict[str, Any]) -> None:
    """The point of the checkpoint. The graph object is discarded between the two halves, which
    is what a process restart amounts to - nothing carries the run forward but the checkpoint."""
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]

    # An hour passes, the service reboots, and only then does a human decide.
    repo.decide_approval(approval_id, "approved", decided_by="reviewer@example.com")

    fresh_graph = build_graph(llm=ScriptedLLM(script(sql=SENSITIVE_SQL)))
    resumed = resume_after_decision(run_id, graph=fresh_graph)

    assert resumed["run_id"] == str(run_id), "the same run, not a new one"
    assert resumed["status"] == "completed"
    nodes = [s["node"] for s in repo.get_trace(run_id)["steps"]]
    assert nodes.count("intake") == 1, "it resumed rather than starting over"
    assert nodes[-1] == "synthesize"


def test_a_timed_out_approval_is_recorded_as_a_decision(
    parked: dict[str, Any], rw_dsn: str
) -> None:
    """A timeout is written down with its reason rather than inferred from the clock later."""
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]

    # Move the deadline into the past rather than waiting half an hour for it.
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.approvals SET expires_at = now() - interval '1 minute' "
            "WHERE approval_id = %s",
            (approval_id,),
        )

    assert approvals.resolve_expired(run_id) >= 1
    decided = next(
        a for a in repo.get_trace(run_id)["approvals"] if a["approval_id"] == approval_id
    )
    assert decided["status"] == "timed_out"
    assert decided["decided_at"] is not None
    assert decided["decision_reason"] == "no decision before the deadline"


def test_a_timed_out_run_still_answers(parked: dict[str, Any]) -> None:
    run_id = uuid.UUID(parked["run_id"])
    approval_id = repo.pending_approvals(run_id)[0]["approval_id"]
    repo.decide_approval(approval_id, "timed_out", decided_by=None, decision_reason="deadline")

    llm = ScriptedLLM(script(sql=SENSITIVE_SQL))
    resumed = resume_after_decision(run_id, graph=build_graph(llm=llm))
    assert resumed["status"] == "completed"
    assert repo.get_trace(run_id)["summary"]["queries_executed"] == 0
