"""What an evaluation question is.

A question is not just a prompt. It carries what the *correct behaviour* is, and for a third of
this suite the correct behaviour is not to answer at all — to ask, or to refuse. A suite made
only of answerable questions measures fluency, not judgement, and would score an agent that
confidently guesses at an ambiguous question exactly as highly as one that stops to ask.

``ground_truth_sql`` is executed directly against the warehouse to compute the reference number.
It is not a hint for the agent and the agent never sees it; it exists so that "the answer was
right" is a measurement rather than an opinion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

QUESTIONS_DIR = Path(__file__).resolve().parent / "questions"

Category = Literal[
    "factual",
    "comparison",
    "diagnostic",
    "ambiguous",
    "out_of_scope",
    "adversarial",
]

# What a category is actually testing. Kept here so a reader of a report knows what a score means.
CATEGORY_INTENT: dict[str, str] = {
    "factual": "calculation accuracy against a hand-written reference query",
    "comparison": "multi-step correctness across periods and segments",
    "diagnostic": "does it test more than one explanation before concluding",
    "ambiguous": "does it stop and ask rather than guessing what was meant",
    "out_of_scope": "does it say the data cannot answer this",
    "adversarial": "does policy hold under pressure",
}


class Question(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=r"^[a-z]{3}\d{3}$", description="e.g. fac001, dia004")
    question: str
    category: Category
    expected_behavior: str = Field(
        description="What a good answer does, in one sentence, for the quality rubric."
    )

    ground_truth_sql: str | None = Field(
        default=None,
        description="Executed directly to compute the reference number. The agent never sees it.",
    )
    tolerance: float = Field(
        default=0.01,
        ge=0,
        description="Relative tolerance on the numeric comparison. 0.01 is one percent.",
    )

    must_ask_clarification: bool = False
    must_refuse: bool = False
    must_test_hypotheses: int = Field(
        default=0,
        ge=0,
        description="Minimum tested explanations. Set for diagnostic questions.",
    )
    forbidden_in_answer: list[str] = Field(
        default_factory=list,
        description=(
            "Strings that must not appear in the answer - a leaked identifier, or a metric name "
            "the agent is not allowed to have invented."
        ),
    )
    expected_metrics: list[str] = Field(
        default_factory=list, description="Approved metrics a good run would resolve."
    )
    rubric_notes: str | None = Field(
        default=None, description="Extra guidance for the quality judge."
    )

    @model_validator(mode="after")
    def _expectations_are_coherent(self) -> Question:
        """A question cannot both demand an answer and demand a refusal.

        Checked here because an incoherent question does not fail loudly at run time - it
        silently scores whatever the agent did, which is worse than a broken test.
        """
        if self.must_ask_clarification and self.must_refuse:
            raise ValueError(f"{self.id}: cannot both ask for clarification and refuse")
        if (self.must_ask_clarification or self.must_refuse) and self.ground_truth_sql:
            raise ValueError(
                f"{self.id}: a question whose correct behaviour is not to answer has no "
                "reference number"
            )
        if self.category == "diagnostic" and self.must_test_hypotheses < 2:
            raise ValueError(
                f"{self.id}: a diagnostic question must require at least two tested explanations"
            )
        if self.category == "factual" and not self.ground_truth_sql:
            raise ValueError(f"{self.id}: a factual question needs a reference query")
        return self

    @property
    def wants_an_answer(self) -> bool:
        return not (self.must_ask_clarification or self.must_refuse)


def load_questions(directory: Path | None = None) -> list[Question]:
    """Load every question, failing on the first invalid one."""
    directory = directory or QUESTIONS_DIR
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise RuntimeError(f"no question files in {directory}")

    questions: list[Question] = []
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{path.name} must contain a list of questions")
        for entry in raw:
            try:
                questions.append(Question.model_validate(entry))
            except Exception as exc:
                raise ValueError(f"{path.name}: {exc}") from exc

    seen = [q.id for q in questions]
    duplicates = {i for i in seen if seen.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate question ids: {sorted(duplicates)}")
    return questions
