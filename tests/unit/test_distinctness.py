"""Rejecting a hypothesis that is another one rewritten.

Two hypotheses that predict the same thing are one hypothesis, and testing both looks like rigour
while establishing nothing. These tests pin down both checks — and, as importantly, pin down what
the weaker one cannot do, so nobody later mistakes it for a paraphrase detector.
"""

from __future__ import annotations

import pytest

from analyst_agent.agent.distinctness import check, same_test_query, similarity, tokens


def test_stopwords_are_ignored() -> None:
    """Two signals sharing only filler words are not similar in any useful sense."""
    assert tokens("the revenue in the month was more than that of the data") == frozenset()


def test_a_near_verbatim_restatement_is_caught() -> None:
    verdict = check(
        [
            "premium category share fell sharply",
            "premium category share fell very sharply",
            "SP seller deliveries ran late",
        ]
    )
    assert verdict.kept == [0, 2]
    assert len(verdict.rejected) == 1
    index, duplicate_of, reason = verdict.rejected[0]
    assert (index, duplicate_of) == (1, 0)
    assert "no test could tell them apart" in reason


def test_genuinely_different_signals_all_survive() -> None:
    verdict = check(
        [
            "the share of high-price categories collapsed",
            "SP seller shipments missed their estimated dates",
            "a competitor launched a promotion that week",
        ]
    )
    assert verdict.kept == [0, 1, 2]
    assert verdict.enough


def test_one_surviving_hypothesis_is_not_enough() -> None:
    verdict = check(["mix shift happened", "mix shift definitely happened"])
    assert len(verdict.kept) == 1
    assert verdict.enough is False


def test_the_lexical_check_cannot_see_a_paraphrase() -> None:
    """Stated as a test so the limitation is documented rather than assumed away.

    'review scores fell' and 'customer satisfaction ratings declined' are the same claim and
    share no vocabulary. This is exactly why the SQL-level check below exists.
    """
    assert similarity("review scores fell", "customer satisfaction ratings declined") == 0.0
    verdict = check(["review scores fell", "customer satisfaction ratings declined"])
    assert verdict.kept == [0, 1], "the lexical check lets a paraphrase through"


# --- the check that actually holds ------------------------------------------

REVENUE = "SELECT ym, sum(revenue) FROM analytics.v_order_revenue GROUP BY 1"


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (REVENUE, REVENUE, True),
        (REVENUE, "select YM,   SUM(revenue)  from analytics.v_order_revenue group by 1", True),
        (REVENUE, "SELECT seller_state, count(*) FROM analytics.sellers GROUP BY 1", False),
        # Neither parses as a query, and two unknowns must not compare equal.
        ("not sql at all", "not sql at all", False),
        ("DROP TABLE analytics.orders", "DROP TABLE analytics.orders", False),
    ],
)
def test_same_test_query_compares_shape_not_spelling(left: str, right: str, same: bool) -> None:
    assert same_test_query(left, right) is same


def test_two_hypotheses_tested_by_one_query_cannot_be_separated() -> None:
    """The substantive rule: identical SQL means the result cannot tell the two apart."""
    mix = "SELECT category, sum(price) FROM analytics.order_items GROUP BY 1"
    assert same_test_query(mix, mix.replace("SELECT", "select").replace("sum", "SUM"))
