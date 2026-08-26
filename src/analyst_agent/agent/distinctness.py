"""Rejecting a hypothesis that is another one rewritten.

The failure this exists to prevent is subtle and common: an agent asked for "two competing
explanations" produces two, they sound different, and they predict exactly the same thing. Two
hypotheses that cannot be separated by any test are one hypothesis, and testing both looks like
rigour while establishing nothing.

So distinctness is decided in code, on the ``distinguishing_signal`` each hypothesis has to
declare - what would be true if *this* is the cause and the others are not. Asking the prompt to
"make them different" is not enforcement; comparing the signals is.

**What this catches and what it does not.** The lexical measure catches near-verbatim
restatements and nothing more. It scores "review scores fell" against "customer satisfaction
ratings declined" at zero, because Jaccard overlap cannot see a paraphrase. Claiming it enforces
conceptual distinctness would be false.

So there is a second, stronger check that does not depend on wording: ``same_test_query``. Two
hypotheses whose tests normalise to the *same SQL* are the same hypothesis whatever they are
called, because no result could ever separate them. That one is exact, and it runs after the
tests are authored rather than before.

Between them: the lexical check is a cheap floor applied at generation, the SQL check is the
real gate applied at test time, and how *interesting* the surviving hypotheses are is a question
for the evaluation rubric rather than for code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# Words that carry no discriminating information, so two signals sharing only these are not
# similar in any useful sense.
STOPWORDS = frozenset(
    # Kept as a literal rather than a split string so the linter can see it, and so a word can
    # be added with a one-line diff.
    [
        "a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these",
        "those", "is", "are", "was", "were", "be", "been", "being", "of", "in", "on", "at",
        "to", "for", "from", "with", "without", "by", "as", "it", "its", "into", "over",
        "under", "about", "during", "would", "will", "could", "should", "may", "might", "can",
        "cause", "caused", "causes", "because", "due", "more", "less", "higher", "lower",
        "increase", "decrease", "increased", "decreased", "change", "changed",
        "revenue", "orders", "month", "months", "data", "query", "result", "results",
    ]
)

SIMILARITY_THRESHOLD = 0.6
"""Jaccard overlap above which two signals are treated as the same signal."""


@dataclass(frozen=True)
class DistinctnessVerdict:
    kept: list[int]
    rejected: list[tuple[int, int, str]]
    """(index rejected, index it duplicates, reason)"""

    @property
    def enough(self) -> bool:
        """Two survivors is the minimum an investigation can proceed on."""
        return len(self.kept) >= 2


def tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z_]{3,}", text.lower())
    return frozenset(w for w in words if w not in STOPWORDS)


def similarity(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalised_sql(sql: str) -> str | None:
    """A statement reduced to its shape, so two spellings of one query compare equal.

    Returns None unless the statement parses *as a query*. Anything else - a parse failure, or
    text that happens to parse as some other expression - normalises to None, and two Nones are
    never treated as equal. Without that, the string "not sql" parsed as a boolean expression and
    two unparseable tests compared identical.
    """
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return None
    if not isinstance(parsed, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        return None
    return parsed.sql(dialect="postgres", normalize=True, pretty=False).lower()


def same_test_query(left_sql: str, right_sql: str) -> bool:
    """Whether two hypotheses are being tested by the same statement.

    This is the check that actually holds. Two explanations tested by identical SQL cannot be
    separated by its result, so running it twice produces the appearance of a second test and
    none of the substance.
    """
    left, right = normalised_sql(left_sql), normalised_sql(right_sql)
    return left is not None and right is not None and left == right


def check(signals: list[str], threshold: float = SIMILARITY_THRESHOLD) -> DistinctnessVerdict:
    """Keep the first of any near-duplicate pair; reject the rest, with a reason.

    First-wins rather than best-wins on purpose: the model is asked to lead with the explanation
    it thinks most likely, so the earlier one is the one worth keeping when two collapse.

    Only near-verbatim duplicates are caught here - see the module docstring on the limits.
    """
    kept: list[int] = []
    rejected: list[tuple[int, int, str]] = []

    for index, signal in enumerate(signals):
        duplicate_of = next(
            (
                other
                for other in kept
                if similarity(signal, signals[other]) >= threshold
            ),
            None,
        )
        if duplicate_of is None:
            kept.append(index)
        else:
            rejected.append(
                (
                    index,
                    duplicate_of,
                    f"predicts the same thing as hypothesis {duplicate_of + 1}, so no test "
                    f"could tell them apart (signal overlap "
                    f"{similarity(signal, signals[duplicate_of]):.0%})",
                )
            )

    return DistinctnessVerdict(kept=kept, rejected=rejected)
