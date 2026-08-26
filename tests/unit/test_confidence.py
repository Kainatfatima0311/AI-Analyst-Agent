"""The confidence score.

The point of a number here is that it can be argued with. So these tests pin the properties a
reader would rely on rather than the exact arithmetic: that applicable components are the only
ones counted, that the agent's own stated band is a ceiling and never a floor, and that each of
the four inputs the score claims to use actually moves it.

A score nobody can decompose is decoration, and a score whose behaviour nobody has pinned is
worse — it drifts silently the next time the weights are touched.
"""

from __future__ import annotations

import pytest

from analyst_agent.agent.confidence import (
    CEILING,
    Confidence,
    RunFacts,
    from_trace,
    score,
)


def _facts(**overrides: object) -> RunFacts:
    """A clean, well-evidenced factual run: three queries, nothing to explain."""
    base: dict[str, object] = {
        "executed_queries": 3,
        "cited_queries": 3,
        "stated_band": "high",
    }
    base.update(overrides)
    return RunFacts(**base)  # type: ignore[arg-type]


# --- the shape of the result ---------------------------------------------------


def test_a_clean_factual_run_scores_at_the_top() -> None:
    """Three queries, complete results, nothing left open — there is nothing to deduct for."""
    result = score(_facts())
    assert result.score == 100
    assert result.band == "high"
    assert result.capped_by is None


def test_the_score_never_leaves_the_range() -> None:
    """A percentage outside 0-100 is a bug the UI would render as a broken bar."""
    for facts in (
        RunFacts(),
        _facts(),
        _facts(truncated_results=9, empty_results=9, blocked_queries=9, escalated_queries=9),
        _facts(material_findings=1, unexplained_findings=1, run_truncated=True),
    ):
        assert 0 <= score(facts).score <= 100


def test_an_answer_resting_on_nothing_scores_nothing() -> None:
    """Better than a default of 50, which would look like a measurement."""
    result = score(RunFacts())
    assert result.score == 0
    assert result.band == "low"


def test_every_factor_is_returned_even_when_it_does_not_apply() -> None:
    """The reader has to be able to see *why* a component was not counted."""
    result = score(_facts())
    keys = {factor.key for factor in result.factors}
    assert keys == {"evidence", "hypotheses", "alternatives", "data_quality", "completeness"}
    hypotheses = next(f for f in result.factors if f.key == "hypotheses")
    assert hypotheses.weight == 0, "no material finding, so nothing to test"
    assert not hypotheses.applicable


def test_a_factor_that_does_not_apply_is_not_scored_as_a_failure() -> None:
    """A single-metric lookup has no hypotheses. Counting that as a miss would be wrong.

    This is the property that lets a factual question reach 100 while a diagnostic question with
    one untested explanation cannot.
    """
    factual = score(_facts())
    diagnostic = score(_facts(material_findings=1, tested_hypotheses=0))
    assert factual.score == 100
    assert diagnostic.score < 60


# --- the four inputs the score claims to use -----------------------------------


def test_more_supporting_queries_never_lowers_the_score() -> None:
    """Monotonic in evidence, and flat once there is enough of it."""
    ladder = [score(_facts(executed_queries=n, cited_queries=n)).score for n in (0, 1, 2, 3, 7)]
    assert ladder == sorted(ladder)
    assert ladder[0] == 0, "no queries is no evidence"
    assert ladder[-1] == ladder[-2], "past three, more queries add nothing"


def test_evidence_is_counted_on_what_the_answer_cites() -> None:
    """A run that executed five queries and cited none has not shown its work."""
    cited = score(_facts(executed_queries=5, cited_queries=3))
    uncited = score(_facts(executed_queries=5, cited_queries=1))
    assert uncited.score < cited.score


def test_testing_a_second_explanation_raises_the_score() -> None:
    one = score(_facts(material_findings=1, tested_hypotheses=1, refuted_hypotheses=0))
    two = score(_facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1))
    assert two.score > one.score


def test_refuting_an_alternative_is_worth_more_than_two_supported_ones() -> None:
    """Refutation is what separates an investigation from a list of guesses."""
    both_supported = score(_facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=0))
    one_refuted = score(_facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1))
    assert one_refuted.score > both_supported.score
    label = next(f.label for f in one_refuted.factors if f.key == "alternatives")
    assert "rejected" in label


@pytest.mark.parametrize(
    "problem",
    ["truncated_results", "empty_results", "blocked_queries", "escalated_queries"],
)
def test_each_data_quality_problem_costs_something_and_is_named(problem: str) -> None:
    clean = score(_facts())
    dirty = score(_facts(**{problem: 1}))
    assert dirty.score < clean.score, problem
    factor = next(f for f in dirty.factors if f.key == "data_quality")
    assert not factor.passed
    assert factor.label, "the reason has to be stated, not just deducted"


def test_a_truncated_result_costs_more_than_an_awaited_decision() -> None:
    """Reasoning over a partial sample is a worse problem than a query still pending."""
    truncated = score(_facts(truncated_results=1))
    escalated = score(_facts(escalated_queries=1))
    assert truncated.score < escalated.score


def test_an_unexplained_material_finding_and_a_truncated_run_both_cost() -> None:
    baseline = score(_facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1))
    unexplained = score(
        _facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1,
               unexplained_findings=1)
    )
    out_of_budget = score(
        _facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1, run_truncated=True)
    )
    assert unexplained.score < baseline.score
    assert out_of_budget.score < baseline.score
    assert "not fully explained" in next(
        f.label for f in unexplained.factors if f.key == "completeness"
    )


# --- the stated band is a ceiling ----------------------------------------------


def test_the_agents_own_band_caps_the_score() -> None:
    """`reconcile` already capped confidence when the tests could not separate explanations.

    That is a judgement about the investigation, and arithmetic here must not overturn it: a run
    the agent called `low` cannot come out at 90 because it happened to run four queries.
    """
    result = score(_facts(stated_band="low"))
    assert result.score == CEILING["low"]
    assert result.band == "low"
    assert result.capped_by == "low"


def test_a_generous_band_does_not_raise_a_weak_score() -> None:
    """A ceiling, never a floor."""
    result = score(_facts(executed_queries=1, cited_queries=1, stated_band="high"))
    assert result.score < 75


def test_a_single_query_cannot_reach_the_high_band() -> None:
    """However clean its result was, one query is one query.

    Without this a one-query lookup with nothing wrong with it reaches high confidence on the
    strength of having no problems, which is not the same as having evidence.
    """
    result = score(_facts(executed_queries=1, cited_queries=1, stated_band="high"))
    assert result.band == "medium"
    assert result.capped_by == "thin evidence"


def test_an_unstated_band_does_not_cap_anything() -> None:
    assert score(_facts(stated_band=None)).score == 100


@pytest.mark.parametrize(
    ("value", "band"), [(100, "high"), (75, "high"), (74, "medium"), (50, "medium"), (49, "low")]
)
def test_the_band_follows_the_score(value: int, band: str) -> None:
    facts = _facts(stated_band=None)
    # Reach the value through the ceiling rather than by guessing weights, so this test does not
    # break every time a weight is tuned.
    result = Confidence(score=value, band=score(facts).band, factors=())
    from analyst_agent.agent.confidence import _band

    assert _band(result.score) == band


# --- rendering -----------------------------------------------------------------


def test_the_result_serialises_for_the_api_and_a_report() -> None:
    payload = score(_facts(material_findings=1, tested_hypotheses=2, refuted_hypotheses=1)).as_dict()
    assert set(payload) == {"score", "band", "capped_by", "factors"}
    assert payload["factors"][0]["label"]
    assert isinstance(payload["score"], int)


def test_the_summary_reads_as_a_sentence_fragment() -> None:
    assert score(_facts()).summary == "100% (high)"


# --- from a trace --------------------------------------------------------------


def test_a_trace_produces_the_same_score_as_its_facts() -> None:
    """The adapter is where a mistake would be invisible, so it gets its own case."""
    finding_id = "f1"
    trace = {
        "run": {"status": "completed"},
        "queries": [
            {"query_id": "q1", "executed": True, "verdict": "allowed", "row_count": 12,
             "truncated": False},
            {"query_id": "q2", "executed": True, "verdict": "allowed", "row_count": 4,
             "truncated": False},
            {"query_id": "q3", "executed": False, "verdict": "rejected"},
        ],
        "findings": [{"finding_id": finding_id, "material": True}],
        "hypotheses": [
            {"finding_id": finding_id, "status": "supported"},
            {"finding_id": finding_id, "status": "refuted"},
        ],
    }
    answer = {"confidence": "medium", "evidence": [{"query_id": "q1"}, {"query_id": "q2"}]}
    result = from_trace(trace, answer)

    assert result.capped_by == "medium", "the stated band still caps it"
    assert result.score == CEILING["medium"]
    blocked = next(f for f in result.factors if f.key == "data_quality")
    assert "refused by the guard" in blocked.label


def test_a_trace_with_a_half_tested_material_finding_is_marked_unexplained() -> None:
    trace = {
        "run": {"status": "completed"},
        "queries": [{"query_id": "q1", "executed": True, "verdict": "allowed", "row_count": 3}],
        "findings": [{"finding_id": "f1", "material": True}],
        "hypotheses": [{"finding_id": "f1", "status": "supported"}],
    }
    result = from_trace(trace, {"confidence": "high", "evidence": [{"query_id": "q1"}]})
    completeness = next(f for f in result.factors if f.key == "completeness")
    assert "not fully explained" in completeness.label
    assert result.score < 50


def test_an_empty_trace_does_not_raise() -> None:
    """A run that failed before doing anything still has to render."""
    assert from_trace({}, None).score == 0
