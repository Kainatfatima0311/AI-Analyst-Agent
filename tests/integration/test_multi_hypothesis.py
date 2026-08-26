"""The investigation loop, and the gate that is the point of the project.

The claim being tested: **a material finding cannot reach an answer with fewer than two tested
explanations.** That is a property of the graph, so it is tested as one — by driving the graph
with a scripted model, and separately by asserting the gate predicate directly, so the rule holds
whichever path the graph happens to take.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from analyst_agent.agent.graph import build_graph, start_run, synthesis_is_blocked
from analyst_agent.agent.nodes.schemas import (
    AnalysisPlan,
    ClarifyDecision,
    FindingOut,
    HypothesisEvaluation,
    HypothesisOut,
    HypothesisSet,
    Interpretation,
    PlanStepOut,
    Reconciliation,
    SqlDraft,
    Synthesis,
)
from analyst_agent.agent.state import initial_state
from analyst_agent.db import repository as repo
from tests.fakes import ScriptedLLM

pytestmark = pytest.mark.integration

Q = "'"
REVENUE_SQL = (
    f"SELECT to_char(o.order_purchase_timestamp, {Q}YYYY-MM{Q}) AS ym, sum(oi.price) AS revenue "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    f"WHERE o.order_status <> {Q}canceled{Q} GROUP BY 1 ORDER BY 1"
)
MIX_SQL = (
    "SELECT p.product_category_name AS category, sum(oi.price) AS revenue "
    "FROM analytics.order_items oi JOIN analytics.products p ON p.product_id = oi.product_id "
    "GROUP BY 1 ORDER BY 2 DESC"
)
DELAY_SQL = (
    "SELECT s.seller_state, count(*) AS late "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    "JOIN analytics.sellers s ON s.seller_id = oi.seller_id "
    "WHERE o.order_delivered_customer_date > o.order_estimated_delivery_date GROUP BY 1"
)


def build_script(
    hypotheses: list[HypothesisOut],
    test_sqls: list[str],
    evaluations: list[HypothesisEvaluation],
    reconciliation_confidence: str = "high",
    refuted: list[str] | None = None,
) -> dict[type, list[Any]]:
    return {
        ClarifyDecision: [
            ClarifyDecision(
                answerable=True, reason="clear enough", question_for_user=None,
                metric_terms=["revenue"],
            )
        ],
        AnalysisPlan: [
            AnalysisPlan(
                steps=[PlanStepOut(intent="establish monthly revenue")],
                expected_shape="one row per month",
            )
        ],
        SqlDraft: [SqlDraft(sql=REVENUE_SQL, purpose="monthly net revenue")]
        + [SqlDraft(sql=sql, purpose="hypothesis test") for sql in test_sqls],
        Interpretation: [
            Interpretation(
                findings=[
                    FindingOut(
                        statement="Net revenue fell 32% in 2018-03.",
                        material=True,
                        evidence_query_ids=[],
                    )
                ],
                summary="a sharp one-month drop",
                needs_more_data=False,
            )
        ],
        HypothesisSet: [HypothesisSet(hypotheses=hypotheses, reasoning="the plausible causes")],
        HypothesisEvaluation: evaluations,
        Reconciliation: [
            Reconciliation(
                conclusion="Category mix shift explains most of the drop.",
                refuted=refuted if refuted is not None else ["review scores fell"],
                confidence=reconciliation_confidence,  # type: ignore[arg-type]
                needs_follow_up=False,
            )
        ],
        Synthesis: [
            Synthesis(
                conclusion="Revenue fell 32% in March 2018, driven by category mix.",
                confidence="high",
                caveats=["Excludes cancelled orders."],
                evidence_query_ids=[],
                refuted=[],
            )
        ],
    }


TWO_DISTINCT = [
    HypothesisOut(
        statement="The premium category share collapsed.",
        test_design="compare category revenue share in the shock month against the prior month",
        distinguishing_signal="high-price categories lose share while volume holds",
    ),
    HypothesisOut(
        statement="SP seller deliveries ran late, pushing cancellations up.",
        test_design="compare late-delivery counts by seller state",
        distinguishing_signal="lateness concentrates in one seller state",
    ),
]


def _cleanup(rw_dsn: str, run_id: str) -> None:
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (uuid.UUID(run_id),))


# --- the gate, asserted directly --------------------------------------------


def test_the_gate_blocks_an_untested_material_finding() -> None:
    """Asserted on the predicate, not only through a graph path, because this is *the* rule."""
    state = initial_state("r", "t", "why did revenue drop?")
    state["findings"] = [
        {"finding_id": "f1", "statement": "Revenue fell 32%.", "material": True,
         "evidence_query_ids": ["q1"]}
    ]

    blocked, reason = synthesis_is_blocked(state)
    assert blocked is True
    assert reason is not None
    assert "0 of 2 tested explanations" in reason


def test_a_proposed_hypothesis_does_not_open_the_gate() -> None:
    """Proposing two explanations is not testing them."""
    state = initial_state("r", "t", "why?")
    state["findings"] = [
        {"finding_id": "f1", "statement": "Revenue fell.", "material": True,
         "evidence_query_ids": ["q1"]}
    ]
    state["hypotheses"] = [
        {"hypothesis_id": "h1", "finding_id": "f1", "status": "proposed"},
        {"hypothesis_id": "h2", "finding_id": "f1", "status": "proposed"},
    ]
    blocked, _ = synthesis_is_blocked(state)
    assert blocked is True


def test_one_tested_explanation_is_still_not_enough() -> None:
    state = initial_state("r", "t", "why?")
    state["findings"] = [
        {"finding_id": "f1", "statement": "Revenue fell.", "material": True,
         "evidence_query_ids": ["q1"]}
    ]
    state["hypotheses"] = [
        {"hypothesis_id": "h1", "finding_id": "f1", "status": "supported"},
        {"hypothesis_id": "h2", "finding_id": "f1", "status": "proposed"},
    ]
    blocked, reason = synthesis_is_blocked(state)
    assert blocked is True
    assert "1 of 2" in reason  # type: ignore[operator]


def test_two_tested_explanations_open_the_gate() -> None:
    """A refutation counts. The point is that the alternative was tested, not that it won."""
    state = initial_state("r", "t", "why?")
    state["findings"] = [
        {"finding_id": "f1", "statement": "Revenue fell.", "material": True,
         "evidence_query_ids": ["q1"]}
    ]
    state["hypotheses"] = [
        {"hypothesis_id": "h1", "finding_id": "f1", "status": "supported"},
        {"hypothesis_id": "h2", "finding_id": "f1", "status": "refuted"},
    ]
    assert synthesis_is_blocked(state) == (False, None)


def test_a_finding_that_is_not_material_does_not_need_explaining() -> None:
    state = initial_state("r", "t", "what was revenue?")
    state["findings"] = [
        {"finding_id": "f1", "statement": "Revenue was 5.4m.", "material": False,
         "evidence_query_ids": ["q1"]}
    ]
    assert synthesis_is_blocked(state) == (False, None)


# --- the loop, driven end to end --------------------------------------------


def test_a_material_finding_gets_two_tested_explanations(rw_dsn: str, seeded: None) -> None:
    """The whole point, exercised: the run cannot conclude until both were tested."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=TWO_DISTINCT,
            test_sqls=[MIX_SQL, DELAY_SQL],
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="premium share fell to 0.11"),
                HypothesisEvaluation(status="refuted", reasoning="lateness was spread evenly"),
            ],
        )
    )
    state = start_run("Why did revenue drop in March 2018?", graph=build_graph(llm=llm))
    try:
        assert state["status"] == "completed"

        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        nodes = [s["node"] for s in trace["steps"]]
        assert nodes.count("test_hypothesis") == 2, "both explanations were tested"
        assert "reconcile" in nodes
        assert nodes.index("materiality_check") < nodes.index("generate_hypotheses")
        assert nodes.index("reconcile") < nodes.index("synthesize")

        statuses = sorted(h["status"] for h in trace["hypotheses"])
        assert statuses == ["refuted", "supported"]
        assert trace["summary"]["hypotheses_refuted"] == 1
        # Three queries: the finding, then one test per explanation.
        assert trace["summary"]["queries_executed"] == 3
    finally:
        _cleanup(rw_dsn, state["run_id"])


def test_the_refuted_explanation_reaches_the_answer(rw_dsn: str, seeded: None) -> None:
    """Naming what was disproved is part of the answer, not an appendix to it."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=TWO_DISTINCT,
            test_sqls=[MIX_SQL, DELAY_SQL],
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="premium share fell"),
                HypothesisEvaluation(status="refuted", reasoning="lateness was flat"),
            ],
            refuted=["delivery delays: lateness was flat across states"],
        )
    )
    state = start_run("Why did revenue drop?", graph=build_graph(llm=llm))
    try:
        assert any("delivery delays" in r for r in state["answer"]["refuted"])
    finally:
        _cleanup(rw_dsn, state["run_id"])


def test_an_inconclusive_alternative_downgrades_confidence(rw_dsn: str, seeded: None) -> None:
    """The model asked for high. It does not get to keep it while an alternative stands."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=TWO_DISTINCT,
            test_sqls=[MIX_SQL, DELAY_SQL],
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="premium share fell"),
                HypothesisEvaluation(
                    status="inconclusive", reasoning="the test could not separate the two"
                ),
            ],
            reconciliation_confidence="high",
        )
    )
    state = start_run("Why did revenue drop?", graph=build_graph(llm=llm))
    try:
        assert state["answer"]["confidence"] == "medium", (
            "an unresolved competing explanation caps confidence"
        )
    finally:
        _cleanup(rw_dsn, state["run_id"])


def test_two_surviving_explanations_cap_confidence_at_low(rw_dsn: str, seeded: None) -> None:
    """If both survive and nothing separated them, saying 'high' would be a false claim."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=TWO_DISTINCT,
            test_sqls=[MIX_SQL, DELAY_SQL],
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="consistent with mix shift"),
                HypothesisEvaluation(status="supported", reasoning="also consistent with delays"),
            ],
            reconciliation_confidence="high",
        )
    )
    state = start_run("Why did revenue drop?", graph=build_graph(llm=llm))
    try:
        assert state["answer"]["confidence"] == "low"
    finally:
        _cleanup(rw_dsn, state["run_id"])


def test_a_duplicate_explanation_is_rejected_before_it_is_tested(
    rw_dsn: str, seeded: None
) -> None:
    """Three proposals, two of which predict the same thing, yield two hypotheses."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=[
                TWO_DISTINCT[0],
                HypothesisOut(
                    statement="High-price categories lost share.",
                    test_design="compare category share",
                    # Near-verbatim restatement of the first signal.
                    distinguishing_signal="high-price categories lose share while volume holds",
                ),
                TWO_DISTINCT[1],
            ],
            test_sqls=[MIX_SQL, DELAY_SQL],
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="premium share fell"),
                HypothesisEvaluation(status="refuted", reasoning="lateness was flat"),
            ],
        )
    )
    state = start_run("Why did revenue drop?", graph=build_graph(llm=llm))
    try:
        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        assert len(trace["hypotheses"]) == 2, "the restatement was never recorded"
        summary = next(s for s in trace["steps"] if s["node"] == "generate_hypotheses")["summary"]
        assert "1 rejected as duplicates" in summary
    finally:
        _cleanup(rw_dsn, state["run_id"])


def test_two_explanations_tested_by_the_same_query_cannot_both_count(
    rw_dsn: str, seeded: None
) -> None:
    """The exact gate. Identical SQL means the second test establishes nothing, so it is marked
    inconclusive rather than being allowed to look like corroboration."""
    llm = ScriptedLLM(
        build_script(
            hypotheses=TWO_DISTINCT,
            test_sqls=[MIX_SQL, MIX_SQL],  # the same statement twice
            evaluations=[
                HypothesisEvaluation(status="supported", reasoning="premium share fell"),
                HypothesisEvaluation(status="supported", reasoning="should never be reached"),
            ],
        )
    )
    state = start_run("Why did revenue drop?", graph=build_graph(llm=llm))
    try:
        trace = repo.get_trace(uuid.UUID(state["run_id"]))
        statuses = sorted(h["status"] for h in trace["hypotheses"])
        assert statuses == ["inconclusive", "supported"]
        duplicate = next(h for h in trace["hypotheses"] if h["status"] == "inconclusive")
        assert "same query" in duplicate["reasoning"]
        # The duplicate was stopped before execution, so only the first test ran.
        assert trace["summary"]["queries_executed"] == 2
        # And confidence reflects that only one explanation was really separated.
        assert state["answer"]["confidence"] in ("low", "medium")
    finally:
        _cleanup(rw_dsn, state["run_id"])
