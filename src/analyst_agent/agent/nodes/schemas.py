"""Structured shapes the nodes ask the model for.

Every node that makes a *decision* asks for one of these rather than for prose. Two reasons, and
the second matters more:

* Parsing prose is where agents quietly go wrong - "probably about 32%" is not a number.
* A decision with named fields can be **routed on**. The graph reads ``needs_clarification`` or
  ``material`` directly; it does not infer intent from wording.

Field descriptions are part of the contract: they are what the model actually reads, so they are
written as instructions rather than as documentation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClarifyDecision(BaseModel):
    """Is the question answerable as asked?"""

    model_config = {"extra": "forbid"}

    answerable: bool = Field(
        description=(
            "True if you can answer this from the available data without guessing what the "
            "asker meant. False if the question is ambiguous, names a metric with no approved "
            "definition, or asks for data that is not there."
        )
    )
    reason: str = Field(description="One sentence on why, for the trace.")
    question_for_user: str | None = Field(
        default=None,
        description=(
            "If not answerable, the single most useful question to ask back. Ask about the one "
            "thing that most changes the answer, not everything at once."
        ),
    )
    metric_terms: list[str] = Field(
        default_factory=list,
        description=(
            "Business terms in the question that need resolving to an approved definition, in "
            "the asker's words. Empty if the question names no metric."
        ),
    )


class PlanStepOut(BaseModel):
    model_config = {"extra": "forbid"}

    intent: str = Field(
        description="What this step establishes, in one sentence. Not the SQL - the purpose."
    )


class AnalysisPlan(BaseModel):
    """The shape of the investigation before any query runs."""

    model_config = {"extra": "forbid"}

    steps: list[PlanStepOut] = Field(
        description=(
            "Two to five steps. Start with the query that establishes whether there is anything "
            "to explain; do not plan the explanation before you have seen the number."
        )
    )
    expected_shape: str = Field(
        description="What you expect the first result to look like, so a surprise is visible."
    )


class SqlDraft(BaseModel):
    """One statement, with the reason it is being run."""

    model_config = {"extra": "forbid"}

    sql: str = Field(
        description=(
            "A single SELECT against the analytics schema. No trailing semicolon, no DDL or DML, "
            "no catalog access."
        )
    )
    purpose: str = Field(
        description=(
            "One sentence for the audit trail: what this establishes and what you expect. Write "
            "it for a reviewer who will read it without the conversation."
        )
    )


class FindingOut(BaseModel):
    model_config = {"extra": "forbid"}

    statement: str = Field(
        description="What the data shows, with the number in it. One sentence, no hedging."
    )
    material: bool = Field(
        description=(
            "True if this is large enough to need explaining rather than merely reporting - a "
            "sharp move, an outlier, something that contradicts the expected shape. A material "
            "finding commits you to testing at least two competing explanations for it."
        )
    )
    evidence_query_ids: list[str] = Field(
        description="The query_id(s) this came from. A finding with no query behind it is not a finding."
    )


class Interpretation(BaseModel):
    """What the result actually says."""

    model_config = {"extra": "forbid"}

    findings: list[FindingOut] = Field(description="What the data shows. May be empty.")
    summary: str = Field(description="One sentence a reader could take away.")
    needs_more_data: bool = Field(
        description="True if the result raises a question the next query should answer."
    )


class Synthesis(BaseModel):
    """The answer."""

    model_config = {"extra": "forbid"}

    conclusion: str = Field(
        description=(
            "The answer to the question as asked. Lead with the answer, then the support. Every "
            "number must appear in a query you ran."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "high: the evidence is direct and alternatives were tested and refuted. "
            "medium: well supported but an alternative remains possible. "
            "low: the data is suggestive only, or the investigation was cut short. "
            "Downgrade rather than overstate."
        )
    )
    caveats: list[str] = Field(
        description=(
            "What would change this answer, what the data cannot tell you, and any definition "
            "caveat carried from a metric. Do not leave this empty to look confident."
        )
    )
    evidence_query_ids: list[str] = Field(
        description="Every query the conclusion rests on."
    )
    refuted: list[str] = Field(
        default_factory=list,
        description=(
            "Explanations you tested and ruled out, and why. Naming what you disproved is part "
            "of the answer, not an appendix to it."
        ),
    )
