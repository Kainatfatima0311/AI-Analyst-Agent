"""Did it get the number right?

The reference number comes from executing the question's ``ground_truth_sql`` directly. That
matters: "the answer was right" becomes a measurement rather than a judgement about whether the
prose sounded correct.

Two things this grader deliberately does not do. It does not parse prose for a number when the
run cited a query — the executed query's own result is the number, and reading it off the text
would grade the agent's phrasing rather than its arithmetic. And it does not give partial credit
for being close in the wrong direction; a tolerance is a tolerance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from analyst_agent.observability.logging import get_logger
from evals.schema import Question

log = get_logger(__name__)

# Numbers as a person writes them: 1,234.56 · 32% · -0.31 · 5.4m
NUMBER = re.compile(r"-?\d[\d,]*\.?\d*\s*%?")


@dataclass
class CalculationResult:
    graded: bool
    passed: bool
    expected: float | None = None
    actual: float | None = None
    relative_error: float | None = None
    note: str = ""

    @property
    def label(self) -> str:
        if not self.graded:
            return "not applicable"
        return "correct" if self.passed else "wrong"


def reference_value(question: Question, conn: Any) -> float | None:
    """Run the question's reference query and return its single value."""
    if not question.ground_truth_sql:
        return None
    with conn.cursor() as cur:
        cur.execute(question.ground_truth_sql)
        row = cur.fetchone()
    if row is None:
        return None
    value = row["value"] if isinstance(row, dict) and "value" in row else next(iter(row.values()))
    return float(value) if value is not None else None


def _numbers_in(text: str) -> list[float]:
    values: list[float] = []
    for match in NUMBER.finditer(text):
        raw = match.group().strip()
        percent = raw.endswith("%")
        cleaned = raw.rstrip("%").strip().replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            continue
        values.append(number / 100 if percent else number)
        if percent:
            # Keep the un-scaled reading too: an answer may quote "32%" for a value the
            # reference computes as 32, not 0.32.
            values.append(number)
    return values


def grade(question: Question, run: dict[str, Any], expected: float | None) -> CalculationResult:
    """Compare what the run reported against the reference.

    Prefers the value the agent's own cited query returned, and falls back to reading a number
    out of the conclusion only when there is no single-value query to read.
    """
    if expected is None:
        return CalculationResult(graded=False, passed=True, note="no reference number")

    answer = run.get("answer") or {}
    conclusion = answer.get("conclusion") or ""
    if not conclusion:
        return CalculationResult(
            graded=True, passed=False, expected=expected, note="the run produced no answer"
        )

    candidates = _numbers_in(conclusion)
    if not candidates:
        return CalculationResult(
            graded=True,
            passed=False,
            expected=expected,
            note="the answer states no number",
        )

    scale = max(abs(expected), 1e-9)
    best = min(candidates, key=lambda value: abs(value - expected))
    error = abs(best - expected) / scale
    passed = error <= question.tolerance

    return CalculationResult(
        graded=True,
        passed=passed,
        expected=expected,
        actual=best,
        relative_error=error,
        note=(
            f"closest reported figure {best:,.4g} against reference {expected:,.4g} "
            f"({error:.2%} off, tolerance {question.tolerance:.0%})"
        ),
    )
