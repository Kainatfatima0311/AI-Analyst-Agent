"""The walking skeleton, driven by a scripted model.

No API key is used here, deliberately. What is being tested is the **routing** — where the
policy actually lives — and that has to be deterministic. Whether the model writes good SQL is a
separate question, answered by the evaluation suite in Step 12.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from analyst_agent.agent.graph import build_graph, resume_run, start_run
from analyst_agent.agent.nodes.schemas import (
    AnalysisPlan,
    ClarifyDecision,
    FindingOut,
    Interpretation,
    PlanStepOut,
    SqlDraft,
    Synthesis,
)
from analyst_agent.agent.state import AnalystState
from analyst_agent.db import repository as repo
from tests.fakes import ScriptedLLM

pytestmark = pytest.mark.integration

Q = "'"
REVENUE_SQL = (
    f"SELECT to_char(o.order_purchase_timestamp, {Q}YYYY-MM{Q}) AS ym, sum(oi.price) AS revenue "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    f"WHERE o.order_status <> {Q}canceled{Q} GROUP BY 1 ORDER BY 1"
)


def script(
    answerable: bool = True,
    sql: str = REVENUE_SQL,
    # Not material by default: these tests are about the linear path. A material finding now
    # commits the run to the investigation loop, which is exercised in test_multi_hypothesis.py
    # with a script that actually has explanations in it.
    material: bool = False,
    metric_terms: list[str] | None = None,
) -> dict[type, list[Any]]:
    return {
        ClarifyDecision: [
            ClarifyDecision(
                answerable=answerable,
                reason="the question names an approved metric and a period",
                question_for_user=None if answerable else "Which month did you mean?",
                metric_terms=metric_terms if metric_terms is not None else ["revenue"],
            )
        ],
        AnalysisPlan: [
            AnalysisPlan(
                steps=[PlanStepOut(intent="establish monthly net revenue")],
                expected_shape="one row per month, revenue rising over time",
            )
        ],
        SqlDraft: [SqlDraft(sql=sql, purpose="monthly net revenue")],
        Interpretation: [
            Interpretation(
                findings=[
                    FindingOut(
                        statement="Net revenue fell 32% in 2018-03.",
                        material=material,
                        evidence_query_ids=[],
                    )
                ],
                summary="revenue dropped sharply in one month",
                needs_more_data=False,
            )
        ],
        Synthesis: [
            Synthesis(
                conclusion="Revenue fell 32% in March 2018.",
                confidence="medium",
                caveats=["Excludes cancelled orders, per the approved definition."],
                evidence_query_ids=[],
                refuted=[],
            )
        ],
    }


@pytest.fixture
def graph_for(rw_dsn: str):
    """Build a graph around a scripted model, and clean up the run afterwards."""
    created: list[uuid.UUID] = []

    def build(**kwargs: Any):
        llm = ScriptedLLM(script(**kwargs))
        return llm, build_graph(llm=llm)

    yield build

    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        for run_id in created:
            cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (run_id,))


def _cleanup(rw_dsn: str, state: dict[str, Any]) -> None:
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (uuid.UUID(state["run_id"]),))


def test_a_question_runs_end_to_end(graph_for, rw_dsn: str, seeded: None) -> None:
    _llm, graph = graph_for()
    state: AnalystState = start_run("What was monthly revenue in 2018?", graph=graph)  # type: ignore[assignment]

    try:
        assert state["status"] == "completed"
        assert state["answer"]["conclusion"]
        assert state["answer"]["confidence"] == "medium"

        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        # Every path to an answer goes through the materiality gate, even one with nothing
        # material to explain - that is what makes the gate impossible to route around.
        assert [s["node"] for s in trace["steps"]] == [
            "intake",
            "clarify_gate",
            "resolve_metrics",
            "plan",
            "author_sql",
            "execute",
            "interpret",
            "materiality_check",
            "synthesize",
        ]
        assert trace["summary"]["queries_executed"] == 1
        assert trace["run"]["status"] == "completed"
    finally:
        _cleanup(rw_dsn, dict(state))


def test_each_node_is_asked_at_its_intended_effort(graph_for, rw_dsn: str, seeded: None) -> None:
    """The per-node effort tiering is a cost decision worth asserting, not assuming."""
    llm, graph = graph_for()
    state = start_run("What was monthly revenue in 2018?", graph=graph)
    try:
        assert llm.effort_for("ClarifyDecision") == "low", "classification should be cheap"
        assert llm.effort_for("SqlDraft") == "high", "SQL authoring is correctness-sensitive"
        assert llm.effort_for("Synthesis") == "xhigh", "the reasoning being judged"
    finally:
        _cleanup(rw_dsn, dict(state))


def test_an_ambiguous_question_stops_to_ask(graph_for, rw_dsn: str, seeded: None) -> None:
    """Stopping to ask is a correct outcome. The run parks, resumable, rather than guessing."""
    _llm, graph = graph_for(answerable=False)
    state = start_run("How are we doing?", graph=graph)
    try:
        assert state["status"] == "clarifying"
        assert state["answer"] is None
        assert state["clarifications"][0]["question"] == "Which month did you mean?"

        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        assert [s["node"] for s in trace["steps"]] == ["intake", "clarify_gate"]
        assert trace["summary"]["queries_considered"] == 0, "no query before the ambiguity is resolved"
        assert trace["run"]["status"] == "clarifying"
        assert uuid.UUID(state["run_id"]) in {r["run_id"] for r in repo.resumable_runs()}
    finally:
        _cleanup(rw_dsn, dict(state))


def test_an_unapproved_metric_is_recorded_as_unapproved(
    graph_for, rw_dsn: str, seeded: None
) -> None:
    """The registry decides, not the model - so an invented metric never becomes an answer."""
    _llm, graph = graph_for(metric_terms=["customer lifetime value"])
    state = start_run("What is our customer lifetime value trend?", graph=graph)
    try:
        resolved = state["resolved_metrics"]
        assert len(resolved) == 1
        assert resolved[0]["approved"] is False
        assert "no approved definition" in resolved[0]["note"]
    finally:
        _cleanup(rw_dsn, dict(state))


def test_an_escalated_query_parks_the_run_instead_of_working_around_it(
    graph_for, rw_dsn: str, seeded: None
) -> None:
    """There is no route past an approval. The run stops and stays resumable."""
    _llm, graph = graph_for(sql="SELECT email FROM analytics.customer_contact")
    state = start_run("Who are our customers?", graph=graph)
    try:
        assert state["status"] == "awaiting_approval"
        assert state["answer"] is None

        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        assert trace["summary"]["queries_escalated"] == 1
        assert trace["summary"]["queries_executed"] == 0
        assert "synthesize" not in [s["node"] for s in trace["steps"]]
        assert uuid.UUID(state["run_id"]) in {r["run_id"] for r in repo.resumable_runs()}
    finally:
        _cleanup(rw_dsn, dict(state))


def test_a_rejected_query_still_produces_an_answer_from_what_is_established(
    graph_for, rw_dsn: str, seeded: None
) -> None:
    """A blocked statement is not a dead end: the run answers with what it has, and the
    rejected attempt stays in the audit."""
    _llm, graph = graph_for(sql="DROP TABLE analytics.orders")
    state = start_run("Delete everything", graph=graph)
    try:
        assert state["status"] == "completed"
        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        assert trace["summary"]["queries_rejected"] == 1
        assert trace["summary"]["queries_executed"] == 0
        assert "synthesize" in [s["node"] for s in trace["steps"]]
        # Nothing ran, so the answer cites nothing - rather than citing the blocked attempt.
        assert state["answer"]["evidence"] == []
    finally:
        _cleanup(rw_dsn, dict(state))


def test_a_spent_budget_produces_a_partial_answer_rather_than_an_error(
    graph_for, rw_dsn: str, seeded: None
) -> None:
    """Exhaustion routes to synthesis with the reason stated, not to an exception."""
    from analyst_agent.config import get_settings

    settings = get_settings().model_copy(update={"max_queries_per_run": 0})
    llm = ScriptedLLM(script())
    graph = build_graph(llm=llm, settings=settings)

    state = start_run("What was monthly revenue in 2018?", graph=graph)
    try:
        assert state["status"] == "truncated"
        assert state["truncation_reason"]
        assert "budget" in state["truncation_reason"]

        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        assert trace["summary"]["queries_considered"] == 0
        assert trace["run"]["status"] == "truncated"
        # The answer still exists; it is honest about being partial rather than absent.
        assert state["answer"]["conclusion"]
    finally:
        _cleanup(rw_dsn, dict(state))


def test_a_parked_run_resumes_from_its_checkpoint_after_the_graph_is_rebuilt(
    graph_for, rw_dsn: str, seeded: None
) -> None:
    """The recovery requirement, exercised the way it actually happens.

    The graph object is discarded and rebuilt between the two halves, which is what a process
    restart amounts to: nothing carries the run forward except the checkpoint.
    """
    llm_a = ScriptedLLM(script(answerable=False))
    parked = start_run("How are we doing?", graph=build_graph(llm=llm_a))
    thread_id = parked["thread_id"]
    run_id = uuid.UUID(parked["run_id"])

    try:
        assert parked["status"] == "clarifying"

        # A completely fresh graph, as after a restart.
        llm_b = ScriptedLLM(script(answerable=True))
        resumed = resume_run(
            thread_id,
            updates={
                "status": "investigating",
                "clarifications": [{"question": "Which month did you mean?", "answer": "March 2018"}],
                "_metric_terms": ["revenue"],
            },
            graph=build_graph(llm=llm_b),
        )

        assert resumed["run_id"] == str(run_id), "the same run, not a new one"
        assert resumed["status"] == "completed"
        assert resumed["answer"]["conclusion"]

        trace = repo.get_trace(run_id)
        nodes = [s["node"] for s in trace["steps"]]
        # Intake and clarify_gate ran once, before the pause; the resumed half continues from
        # there rather than starting over.
        assert nodes.count("intake") == 1
        assert nodes[-1] == "synthesize"
        assert trace["summary"]["queries_executed"] == 1
    finally:
        _cleanup(rw_dsn, {"run_id": str(run_id)})


def test_resuming_an_unknown_thread_raises(rw_dsn: str) -> None:
    llm = ScriptedLLM(script())
    with pytest.raises(KeyError, match="no checkpoint"):
        resume_run(f"run-{uuid.uuid4()}", graph=build_graph(llm=llm))
