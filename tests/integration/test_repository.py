"""Run state, trace and audit persistence.

Two things are asserted here. First, that a whole run round-trips: a reviewer who was not
present can reconstruct it from `get_trace` alone. Second, that the design document's
invariants are enforced by the **database**, not merely by application code — because a CHECK
constraint cannot be forgotten by a node written six steps from now.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from analyst_agent.db import repository as repo

pytestmark = pytest.mark.integration


@pytest.fixture
def run_id(rw_dsn: str):
    """A real run, cleaned up afterwards. ON DELETE CASCADE removes everything beneath it."""
    rid = repo.create_run("Why did revenue drop in 2018-03?", requested_by="pytest")
    yield rid
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (rid,))


def test_a_whole_run_round_trips(run_id: uuid.UUID) -> None:
    repo.set_run_status(run_id, "investigating")

    with repo.step(run_id, "author_sql", effort="high") as handle:
        query_id = repo.record_sql_audit(
            run_id,
            purpose="monthly net revenue for 2018",
            sql_text="SELECT year_month, sum(item_revenue) FROM analytics.v_order_revenue "
            "GROUP BY 1 ORDER BY 1",
            verdict="allowed",
            referenced_objects=["analytics.v_order_revenue"],
            estimated_cost=1234.5,
            step_id=handle.step_id,
        )
        repo.mark_sql_executed(query_id, row_count=24, truncated=False, duration_ms=41)
        repo.record_tool_call(
            run_id,
            tool="sql_runner",
            arguments={"sql": "SELECT ...", "purpose": "monthly net revenue"},
            ok=True,
            step_id=handle.step_id,
            result_summary={"rows": 24},
            duration_ms=41,
        )

    finding_id = repo.record_finding(
        run_id, "Net revenue fell 32% in 2018-03.", [query_id], material=True
    )
    repo.record_hypothesis(run_id, finding_id, "Category mix shifted.", "Compare category share.")
    repo.finish_run(run_id, "completed", answer={"conclusion": "mix shift plus cancellations"})

    trace = repo.get_trace(run_id)

    assert trace["run"]["status"] == "completed"
    assert trace["run"]["duration_ms"] is not None
    assert len(trace["steps"]) == 1
    assert trace["steps"][0]["node"] == "author_sql"
    assert trace["steps"][0]["status"] == "ok"
    assert trace["steps"][0]["effort"] == "high"
    assert len(trace["tool_calls"]) == 1
    assert trace["summary"]["queries_executed"] == 1
    assert trace["summary"]["queries_rejected"] == 0
    # Every reported finding resolves back to a stored query.
    evidence = set(trace["findings"][0]["evidence_query_ids"])
    stored = {q["query_id"] for q in trace["queries"]}
    assert evidence <= stored


def test_rejected_queries_are_kept_in_the_audit(run_id: uuid.UUID) -> None:
    """A run where the guard blocked attempts beats one where those attempts vanished."""
    repo.record_sql_audit(
        run_id,
        purpose="attempted deletion",
        sql_text="DELETE FROM analytics.orders",
        verdict="rejected",
        reasons=["statement root is not a SELECT"],
    )
    repo.record_sql_audit(
        run_id,
        purpose="expensive scan",
        sql_text="SELECT * FROM analytics.order_items oi, analytics.orders o",
        verdict="escalated",
        reasons=["estimated cost above ceiling", "cross join without a join condition"],
        estimated_cost=9_000_000,
    )

    trace = repo.get_trace(run_id)
    assert trace["summary"]["queries_considered"] == 2
    assert trace["summary"]["queries_executed"] == 0
    assert trace["summary"]["queries_rejected"] == 1
    assert trace["summary"]["queries_escalated"] == 1
    reasons = [r for q in trace["queries"] for r in q["reasons"]]
    assert "estimated cost above ceiling" in reasons


def test_a_rejected_query_cannot_be_marked_executed(run_id: uuid.UUID) -> None:
    """Constraint sql_audit_executed_implies_allowed: a tool-layer bug cannot fake execution."""
    query_id = repo.record_sql_audit(
        run_id, purpose="blocked", sql_text="DROP TABLE analytics.orders", verdict="rejected"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.mark_sql_executed(query_id, row_count=0, truncated=False, duration_ms=1)


def test_a_finding_without_evidence_is_rejected(run_id: uuid.UUID) -> None:
    """Constraint findings_require_evidence: the traceability invariant, held in the schema."""
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.record_finding(run_id, "Revenue fell because of the weather.", [])


def test_a_hypothesis_cannot_become_a_verdict_untested(run_id: uuid.UUID) -> None:
    """Constraint hypotheses_require_a_test: no conclusion from an untested hypothesis."""
    query_id = repo.record_sql_audit(
        run_id, purpose="evidence", sql_text="SELECT 1", verdict="allowed"
    )
    finding_id = repo.record_finding(run_id, "Revenue fell 32%.", [query_id], material=True)
    hypothesis_id = repo.record_hypothesis(
        run_id, finding_id, "Seasonality.", "Compare with the same month last year."
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        repo.update_hypothesis(hypothesis_id, "supported", test_query_ids=[])

    # With a test query attached it moves normally.
    test_query_id = repo.record_sql_audit(
        run_id, purpose="seasonality test", sql_text="SELECT 2", verdict="allowed"
    )
    repo.update_hypothesis(
        hypothesis_id, "refuted", test_query_ids=[test_query_id], reasoning="2017-03 was flat."
    )
    rows = repo.hypotheses_for_finding(finding_id)
    assert rows[0]["status"] == "refuted"
    assert rows[0]["reasoning"] == "2017-03 was flat."


def test_terminal_hypothesis_count_ignores_untested_ones(run_id: uuid.UUID) -> None:
    """This is what the graph reads to decide whether synthesis is allowed yet."""
    query_id = repo.record_sql_audit(run_id, purpose="e", sql_text="SELECT 1", verdict="allowed")
    finding_id = repo.record_finding(run_id, "Revenue fell.", [query_id], material=True)

    a = repo.record_hypothesis(run_id, finding_id, "Mix shift.", "Category share by month.")
    b = repo.record_hypothesis(run_id, finding_id, "Delays.", "Late rate by seller state.")
    repo.record_hypothesis(run_id, finding_id, "Seasonality.", "Same month last year.")

    assert repo.terminal_hypothesis_count(finding_id) == 0

    repo.update_hypothesis(a, "supported", test_query_ids=[query_id])
    assert repo.terminal_hypothesis_count(finding_id) == 1

    repo.update_hypothesis(b, "refuted", test_query_ids=[query_id])
    assert repo.terminal_hypothesis_count(finding_id) == 2


def test_an_approval_cannot_be_decided_twice(run_id: uuid.UUID) -> None:
    approval_id = repo.create_approval(
        run_id,
        kind="sensitive_column",
        reason="query projects analytics.customer_contact.email",
        payload={"sql": "SELECT email FROM analytics.customer_contact"},
        timeout_seconds=1800,
    )
    assert repo.pending_approvals(run_id)[0]["approval_id"] == approval_id

    assert repo.decide_approval(approval_id, "rejected", decided_by="pytest") is True
    # The second decision is refused rather than overwriting the first.
    assert repo.decide_approval(approval_id, "approved", decided_by="someone-else") is False

    trace = repo.get_trace(run_id)
    approval = trace["approvals"][0]
    assert approval["status"] == "rejected"
    assert approval["decided_by"] == "pytest"
    assert trace["summary"]["approvals_pending"] == 0


def test_a_failing_node_is_recorded_and_re_raised(run_id: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="boom"), repo.step(run_id, "author_sql"):
        raise ValueError("boom")

    trace = repo.get_trace(run_id)
    assert trace["steps"][0]["status"] == "error"
    assert trace["steps"][0]["error"]["type"] == "ValueError"
    assert trace["steps"][0]["error"]["message"] == "boom"


def test_a_summary_written_by_a_node_survives_the_step_closing(run_id: uuid.UUID) -> None:
    """Regression: the context manager used to close an already-closed step on the way out,
    overwriting the node's summary with NULL. That silently emptied the one human-readable
    column in the whole trace, which is most of what makes a run reviewable."""
    with repo.step(run_id, "interpret") as handle:
        repo.finish_step(handle, summary="revenue fell 32% in 2018-03", usage=repo.Usage(10, 5))

    step = repo.get_trace(run_id)["steps"][0]
    assert step["summary"] == "revenue fell 32% in 2018-03"
    assert step["status"] == "ok"
    assert step["tokens_in"] == 10


def test_a_failing_node_records_the_error_even_after_writing_a_summary(
    run_id: uuid.UUID,
) -> None:
    """How a step ended matters more than the summary it managed to write before failing."""
    with pytest.raises(RuntimeError), repo.step(run_id, "author_sql") as handle:
        repo.finish_step(handle, summary="looked fine at this point")
        raise RuntimeError("then it did not")

    step = repo.get_trace(run_id)["steps"][0]
    assert step["status"] == "error"
    assert step["error"]["message"] == "then it did not"


def test_usage_accumulates_on_the_run(run_id: uuid.UUID) -> None:
    repo.add_run_usage(run_id, repo.Usage(1000, 200, 800, 0.01), queries=1, iterations=1)
    repo.add_run_usage(run_id, repo.Usage(500, 100, 400, 0.005), queries=2, iterations=1)

    run = repo.get_run(run_id)
    assert run is not None
    assert run["tokens_in"] == 1500
    assert run["tokens_out"] == 300
    assert run["cache_read_tokens"] == 1200
    assert run["queries_used"] == 3
    assert run["iterations"] == 2


def test_a_resumable_run_is_listed_and_a_finished_one_is_not(run_id: uuid.UUID) -> None:
    repo.set_run_status(run_id, "awaiting_approval")
    assert run_id in {r["run_id"] for r in repo.resumable_runs()}

    repo.finish_run(run_id, "completed")
    assert run_id not in {r["run_id"] for r in repo.resumable_runs()}


def test_step_sequence_numbers_are_dense_and_ordered(run_id: uuid.UUID) -> None:
    for node in ("intake", "plan", "author_sql"):
        with repo.step(run_id, node):
            pass
    steps = repo.get_trace(run_id)["steps"]
    assert [s["seq"] for s in steps] == [1, 2, 3]
    assert [s["node"] for s in steps] == ["intake", "plan", "author_sql"]


def test_a_refusal_is_recorded_as_a_result_not_an_error(run_id: uuid.UUID) -> None:
    repo.record_tool_call(
        run_id,
        tool="metric_lookup",
        arguments={"term": "customer lifetime value"},
        ok=True,
        refusal="no approved definition for 'customer lifetime value'",
    )
    call = repo.get_trace(run_id)["tool_calls"][0]
    assert call["ok"] is True
    assert call["error"] is None
    assert "no approved definition" in call["refusal"]


def test_expired_approvals_are_timed_out_with_a_recorded_reason(run_id: uuid.UUID) -> None:
    approval_id = repo.create_approval(
        run_id,
        kind="expensive_query",
        reason="estimated cost above ceiling",
        payload={"estimated_cost": 9_000_000},
        timeout_seconds=-1,  # already past its deadline
    )
    assert repo.expire_stale_approvals() >= 1

    approval = next(
        a for a in repo.get_trace(run_id)["approvals"] if a["approval_id"] == approval_id
    )
    assert approval["status"] == "timed_out"
    assert approval["decided_at"] is not None
    assert approval["decision_reason"] == "no decision before the deadline"


def test_unknown_run_raises(rw_dsn: str) -> None:
    with pytest.raises(KeyError):
        repo.get_trace(uuid.uuid4())
