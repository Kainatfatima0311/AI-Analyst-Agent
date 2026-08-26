"""Budget caps and the state predicates the graph routes on.

No database and no model: these are the pieces of policy that are pure functions, and they are
worth testing as such because the graph's edges read them directly.
"""

from __future__ import annotations

import pytest

from analyst_agent.agent.budget import Budget
from analyst_agent.agent.state import (
    AnalystState,
    every_finding_has_evidence,
    executed_query_ids,
    initial_state,
    material_findings,
    open_clarifications,
    unapproved_metrics,
)
from analyst_agent.agent.state import (
    # aliased because pytest collects any module-level name starting with "test"
    tested_hypotheses as hypotheses_tested_for,
)
from analyst_agent.config import Settings

DUMMY_DSN = "postgresql://u:p@localhost:5432/db"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        db_rw_dsn=DUMMY_DSN,
        db_ro_dsn=DUMMY_DSN,
        max_queries_per_run=5,
        max_agent_iterations=10,
        max_tokens_per_run=10_000,
        max_run_wall_clock_seconds=60,
    )


# --- budget -----------------------------------------------------------------


def test_a_fresh_budget_is_not_exhausted(settings: Settings) -> None:
    assert Budget.from_settings(settings).exhausted() is None


def test_the_first_limit_to_bind_is_the_one_reported(settings: Settings) -> None:
    budget = Budget.from_settings(settings)
    for _ in range(5):
        budget.record_query()
    reason = budget.exhausted()
    assert reason is not None
    assert "query budget" in reason


def test_the_token_limit_binds_too(settings: Settings) -> None:
    budget = Budget.from_settings(settings)
    budget.record_tokens(9_000, 1_500)
    reason = budget.exhausted()
    assert reason is not None
    assert "token budget" in reason


def test_a_query_that_would_exceed_the_cap_is_refused_before_it_runs(
    settings: Settings,
) -> None:
    """Checked before authoring, so the run stops cleanly rather than mid-query."""
    budget = Budget.from_settings(settings)
    for _ in range(4):
        budget.record_query()
    assert budget.would_exceed_queries() is False
    budget.record_query()
    assert budget.would_exceed_queries() is True


def test_remaining_reports_what_is_left(settings: Settings) -> None:
    budget = Budget.from_settings(settings)
    budget.record_query()
    budget.record_tokens(1_000, 500)
    remaining = budget.remaining()
    assert remaining["queries"] == 4
    assert remaining["tokens"] == 8_500


def test_an_approved_extension_raises_the_ceilings(settings: Settings) -> None:
    """Approval point 3: a human can grant more, and the grant is counted."""
    budget = Budget.from_settings(settings)
    for _ in range(5):
        budget.record_query()
    assert budget.exhausted() is not None

    budget.grant_extension()
    assert budget.exhausted() is None
    assert budget.extensions_granted == 1
    assert budget.max_queries == 7


def test_a_restored_budget_keeps_its_counters_and_its_extensions(settings: Settings) -> None:
    """State survives a restart; the ceilings a human already raised survive with it."""
    budget = Budget.from_settings(settings)
    for _ in range(3):
        budget.record_query()
    budget.record_tokens(500, 200)
    budget.grant_extension()

    restored = Budget.restore(budget.to_state(), settings)
    assert restored.queries_used == 3
    assert restored.tokens_in == 500
    assert restored.extensions_granted == 1
    assert restored.max_queries == budget.max_queries, "the granted extension is not lost"


def test_the_wall_clock_restarts_on_resume(settings: Settings) -> None:
    """Counting an hour spent waiting for a human against the work budget would make the
    approval flow self-defeating."""
    budget = Budget.from_settings(settings)
    restored = Budget.restore(budget.to_state(), settings)
    assert restored.elapsed_seconds < 1.0


# --- state predicates -------------------------------------------------------


def _state(**kwargs: object) -> AnalystState:
    state = initial_state("r", "t", "why did revenue drop?")
    state.update(kwargs)  # type: ignore[typeddict-item]
    return state


def test_only_executed_queries_count_as_evidence() -> None:
    """A rejected or escalated attempt is in the audit, but it is not evidence."""
    state = _state(
        queries=[
            {"query_id": "a", "verdict": "allowed", "row_count": 12},
            {"query_id": "b", "verdict": "rejected", "reasons": ["not_a_select"]},
            {"query_id": "c", "verdict": "escalated", "reasons": ["sensitive_column"]},
        ]
    )
    assert executed_query_ids(state) == ["a"]


def test_unapproved_metrics_are_surfaced() -> None:
    state = _state(
        resolved_metrics=[
            {"term": "revenue", "approved": True},
            {"term": "ltv", "approved": False, "note": "no approved definition"},
        ]
    )
    assert unapproved_metrics(state) == ["ltv"]


def test_open_clarifications_exclude_answered_ones() -> None:
    state = _state(
        clarifications=[
            {"question": "which month?", "answer": "March"},
            {"question": "which region?", "answer": None},
        ]
    )
    assert len(open_clarifications(state)) == 1


def test_material_findings_are_the_ones_needing_explanation() -> None:
    state = _state(
        findings=[
            {"finding_id": "1", "statement": "revenue fell 32%", "material": True},
            {"finding_id": "2", "statement": "orders were flat", "material": False},
        ]
    )
    assert [f["finding_id"] for f in material_findings(state)] == ["1"]


def test_a_proposed_hypothesis_does_not_count_as_tested() -> None:
    """This is the predicate Step 8's edge reads. A proposal is not a test."""
    state = _state(
        hypotheses=[
            {"hypothesis_id": "h1", "finding_id": "f1", "status": "proposed"},
            {"hypothesis_id": "h2", "finding_id": "f1", "status": "testing"},
            {"hypothesis_id": "h3", "finding_id": "f1", "status": "refuted"},
            {"hypothesis_id": "h4", "finding_id": "f1", "status": "supported"},
            {"hypothesis_id": "h5", "finding_id": "f2", "status": "supported"},
        ]
    )
    assert [h["hypothesis_id"] for h in hypotheses_tested_for(state, "f1")] == ["h3", "h4"]
    assert len(hypotheses_tested_for(state, "f2")) == 1


def test_the_evidence_invariant_as_a_predicate() -> None:
    good = _state(findings=[{"finding_id": "1", "evidence_query_ids": ["a"]}])
    bad = _state(findings=[{"finding_id": "1", "evidence_query_ids": []}])
    assert every_finding_has_evidence(good) is True
    assert every_finding_has_evidence(bad) is False


def test_initial_state_starts_empty_and_received() -> None:
    state = initial_state("r", "t", "a question", requested_by="pytest")
    assert state["status"] == "received"
    assert state["answer"] is None
    assert state["queries"] == []
    assert state["requested_by"] == "pytest"
