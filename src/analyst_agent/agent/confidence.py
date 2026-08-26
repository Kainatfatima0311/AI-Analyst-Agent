"""How confident the answer is, as a number a reader can audit.

The agent already states a confidence *band* — high, medium, low — but a band is a claim the
model makes about itself. This module computes a score from what the run actually did, so the
number can be checked against the trace rather than taken on trust. Every point is attributable
to a named factor, and the factors are returned alongside the score for exactly that reason: a
confidence figure nobody can decompose is decoration.

**Applicable components only.** A factual question with one query has no hypotheses to test, and
scoring it as though it had failed to test any would be wrong. Each component declares whether it
applies to this run, and the score is the weighted average over the ones that do. That is why a
simple lookup can reach 100 and a diagnostic question with one untested explanation cannot.

**The model's own band is a ceiling, never a floor.** `reconcile` already caps confidence when
competing explanations could not be separated, and `synthesize` caps it again when a material
finding went unexplained. Those caps are decisions about the *investigation*, and this arithmetic
must not overturn them — so a run the agent called `low` cannot come out at 90 because it happened
to run four queries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

Band = Literal["high", "medium", "low"]

# A band a reader can act on. The thresholds are deliberately generous at the bottom: the
# difference between 20 and 40 is not worth a distinct label, but the difference between "act on
# this" and "check this first" is.
HIGH_FROM = 75
MEDIUM_FROM = 50

# What the model's own band permits. Stated confidence is an input, not the last word — but it is
# a ceiling, because the caps behind it are judgements about the investigation.
CEILING: dict[str, int] = {"high": 100, "medium": 74, "low": 49}

# The top of the medium band. Reused as the cap for a run whose evidence is a single query.
MEDIUM_CEILING = HIGH_FROM - 1


@dataclass(frozen=True)
class Factor:
    """One reason the score is what it is."""

    key: str
    label: str
    passed: bool
    earned: float
    weight: float
    detail: str = ""

    @property
    def applicable(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True)
class Confidence:
    score: int
    band: Band
    factors: tuple[Factor, ...] = field(default_factory=tuple)
    capped_by: str | None = None
    """The stated band, when it held the score below what the factors earned."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "band": self.band,
            "capped_by": self.capped_by,
            "factors": [asdict(f) for f in self.factors],
        }

    @property
    def summary(self) -> str:
        """A one-line rendering, for a log or an export header."""
        return f"{self.score}% ({self.band})"


@dataclass(frozen=True)
class RunFacts:
    """What the score is computed from.

    Primitives rather than a state or trace object, so the arithmetic can be tested without a
    database and so the same numbers can be produced from a live run or from a stored report.
    """

    executed_queries: int = 0
    """Queries that ran and returned a result. The evidence an answer rests on."""

    cited_queries: int = 0
    """Executed queries the answer actually cites. Evidence nobody points at is not evidence."""

    material_findings: int = 0
    tested_hypotheses: int = 0
    """Hypotheses in a terminal state — supported, refuted or inconclusive."""

    refuted_hypotheses: int = 0
    """A refuted alternative is the strongest single signal that the agent looked."""

    inconclusive_hypotheses: int = 0
    unexplained_findings: int = 0
    """Material findings that reached the answer without two tested explanations."""

    truncated_results: int = 0
    """Queries whose rows hit the cap, so the agent reasoned over a partial sample."""

    empty_results: int = 0
    blocked_queries: int = 0
    """Statements the guard refused. Not a safety concern here - a gap in the evidence."""

    escalated_queries: int = 0
    run_truncated: bool = False
    """The budget ran out mid-investigation."""

    stated_band: str | None = None


def score(facts: RunFacts) -> Confidence:
    """Compute the score and the factors behind it."""
    facts = _normalise(facts)
    factors = (
        _evidence(facts),
        _hypotheses(facts),
        _alternatives(facts),
        _data_quality(facts),
        _completeness(facts),
    )

    if not facts.executed_queries:
        # An answer resting on nothing scores nothing. Averaging the components here would let
        # "nothing left unresolved" — vacuously true when nothing was attempted — carry a run
        # that established no facts at all to a number that looks like a measurement.
        return Confidence(score=0, band="low", factors=factors)

    applicable = [f for f in factors if f.applicable]
    earned = sum(f.earned for f in applicable)
    total = sum(f.weight for f in applicable)
    raw = round(100 * earned / total)

    final, capped = _apply_ceilings(raw, facts)
    return Confidence(score=final, band=_band(final), factors=factors, capped_by=capped)


def _normalise(facts: RunFacts) -> RunFacts:
    """Close the one gap a caller could leave open.

    A material finding with fewer than two tested explanations *is* an unexplained finding — that
    is the bar the whole investigation loop is built around. Deriving it here rather than trusting
    the caller to pass it means the score cannot disagree with the graph's own rule.
    """
    if facts.material_findings and facts.tested_hypotheses < 2 and not facts.unexplained_findings:
        return replace(facts, unexplained_findings=1)
    return facts


def _apply_ceilings(raw: int, facts: RunFacts) -> tuple[int, str | None]:
    """Two caps, both of them statements about the investigation rather than the arithmetic.

    * **Thin evidence.** However clean a single query's result was, it is still a single query.
      Without a second, the answer cannot claim high confidence — otherwise a one-query lookup
      with no problems reaches the high band on the strength of having nothing wrong with it.
    * **The agent's own band.** `reconcile` already lowered confidence where the tests could not
      separate two explanations, and `synthesize` lowered it again for an unexplained material
      finding. Those are judgements, and this arithmetic must not overturn them.
    """
    supporting = facts.cited_queries or facts.executed_queries
    score_now, capped = raw, None

    if supporting < 2 and score_now > MEDIUM_CEILING:
        score_now, capped = MEDIUM_CEILING, "thin evidence"

    stated = CEILING.get(facts.stated_band or "", 100)
    if score_now > stated:
        score_now, capped = stated, facts.stated_band

    return score_now, capped


def _band(value: int) -> Band:
    if value >= HIGH_FROM:
        return "high"
    if value >= MEDIUM_FROM:
        return "medium"
    return "low"


# --- the components -----------------------------------------------------------


def _evidence(facts: RunFacts) -> Factor:
    """Supporting queries. Weight 30.

    Counted on *cited* queries where the answer cites any, and on executed ones otherwise: a run
    that executed five queries and cited none has not shown its work, and should not be paid for
    the four the answer ignored.
    """
    weight = 30.0
    count = facts.cited_queries or facts.executed_queries
    earned = {0: 0.0, 1: 14.0, 2: 22.0}.get(count, 30.0) if count >= 0 else 0.0
    plural = "y" if count == 1 else "ies"
    return Factor(
        key="evidence",
        label=f"{count} quer{plural} executed",
        passed=count >= 2,
        earned=earned,
        weight=weight,
        detail=(
            "Three or more supporting queries is full marks; one is thin but not nothing."
            if count
            else "Nothing ran that this answer rests on."
        ),
    )


def _hypotheses(facts: RunFacts) -> Factor:
    """Competing explanations tested. Weight 30, and only for a run with a material finding.

    A single-metric lookup has nothing to explain, so this does not apply and its weight leaves
    the denominator rather than scoring zero.
    """
    if not facts.material_findings:
        return Factor(
            key="hypotheses",
            label="No finding required explaining",
            passed=True,
            earned=0.0,
            weight=0.0,
            detail="A factual question has no competing explanations to test.",
        )

    tested = facts.tested_hypotheses
    earned = {0: 0.0, 1: 11.0}.get(tested, 30.0)
    plural = "" if tested == 1 else "es"
    return Factor(
        key="hypotheses",
        label=f"{tested} hypothes{'is' if tested == 1 else 'es'} tested",
        passed=tested >= 2,
        earned=earned,
        weight=30.0,
        detail=(
            "Two tested explanations is the bar this project sets for a material finding."
            if tested < 2
            else f"{tested} explanation{plural} were each given their own query."
        ),
    )


def _alternatives(facts: RunFacts) -> Factor:
    """Whether an alternative was actually ruled out. Weight 15, material findings only.

    Two supported explanations are weaker evidence than one supported and one refuted: refutation
    is what separates an investigation from a list of guesses.
    """
    if not facts.material_findings:
        return Factor(
            key="alternatives",
            label="No alternatives to rule out",
            passed=True,
            earned=0.0,
            weight=0.0,
        )
    refuted = facts.refuted_hypotheses
    return Factor(
        key="alternatives",
        label=(
            f"{refuted} alternative explanation{'s' if refuted != 1 else ''} rejected"
            if refuted
            else "No alternative was ruled out"
        ),
        passed=refuted >= 1,
        earned=15.0 if refuted >= 1 else 0.0,
        weight=15.0,
        detail=(
            "A refuted alternative is the strongest single sign the agent looked rather than "
            "guessed."
        ),
    )


def _data_quality(facts: RunFacts) -> Factor:
    """What the data itself allowed. Weight 25.

    Full marks for results that were complete and non-empty. Each problem costs, and the reason
    is named: a truncated result means the agent reasoned over a partial sample, an empty one
    means a filter may have been wrong, and a blocked statement is a gap in the evidence the
    reader cannot see from the conclusion alone.
    """
    weight = 25.0
    if not facts.executed_queries:
        return Factor(
            key="data_quality",
            label="No results to assess",
            passed=False,
            earned=0.0,
            weight=weight,
            detail="Nothing executed, so nothing can be said about the data behind the answer.",
        )

    penalties: list[tuple[float, str]] = []
    if facts.truncated_results:
        penalties.append((10.0, f"{facts.truncated_results} result(s) hit the row cap"))
    if facts.empty_results:
        penalties.append((7.0, f"{facts.empty_results} query returned no rows"))
    if facts.blocked_queries:
        penalties.append((6.0, f"{facts.blocked_queries} statement(s) refused by the guard"))
    if facts.escalated_queries:
        penalties.append((4.0, f"{facts.escalated_queries} statement(s) await a decision"))

    lost = min(weight, sum(p for p, _ in penalties))
    return Factor(
        key="data_quality",
        label="Complete results" if not penalties else "; ".join(note for _, note in penalties),
        passed=not penalties,
        earned=weight - lost,
        weight=weight,
        detail=(
            "Every supporting query returned a complete, non-empty result."
            if not penalties
            else "Each of these narrows what the answer can safely claim."
        ),
    )


def _completeness(facts: RunFacts) -> Factor:
    """What is still open. Weight 20.

    An unexplained material finding and a budget that ran out are the two ways a run ends with
    the question only partly answered, and both are things the reader has to know.
    """
    weight = 20.0
    problems: list[str] = []
    if facts.unexplained_findings:
        problems.append(f"{facts.unexplained_findings} finding(s) not fully explained")
    if facts.inconclusive_hypotheses:
        problems.append(f"{facts.inconclusive_hypotheses} explanation(s) inconclusive")
    if facts.run_truncated:
        problems.append("the investigation was cut short by its budget")

    lost = min(weight, 12.0 * bool(facts.unexplained_findings)
               + 5.0 * bool(facts.inconclusive_hypotheses)
               + 10.0 * bool(facts.run_truncated))
    return Factor(
        key="completeness",
        label="Nothing left unresolved" if not problems else "; ".join(problems),
        passed=not problems,
        earned=weight - lost,
        weight=weight,
        detail=(
            "The investigation reached a conclusion on everything it raised."
            if not problems
            else "Stated because an answer that hides its loose ends is worse than one that "
            "names them."
        ),
    )


# --- adapters -----------------------------------------------------------------


def from_trace(trace: dict[str, Any], answer: dict[str, Any] | None) -> Confidence:
    """Compute from a stored trace, so any run — live or long finished — scores identically.

    Deriving this on read rather than freezing it at write time means a scoring change applies to
    every run in the history instead of only to new ones, and there is no stored number that can
    disagree with the trace it came from.
    """
    run = trace.get("run") or {}
    queries = trace.get("queries") or []
    findings = trace.get("findings") or []
    # The trace keeps hypotheses in their own flat list, keyed by finding, rather than nested.
    # Grouping here keeps this the only place that has to know that.
    hypotheses = trace.get("hypotheses") or []

    executed = [q for q in queries if q.get("executed")]
    terminal = [
        h for h in hypotheses if h.get("status") in ("supported", "refuted", "inconclusive")
    ]
    material = [f for f in findings if f.get("material")]

    by_finding: dict[Any, list[dict[str, Any]]] = {}
    for hypothesis in hypotheses:
        by_finding.setdefault(hypothesis.get("finding_id"), []).append(hypothesis)

    unexplained = 0
    for finding in material:
        tested = [
            h
            for h in by_finding.get(finding.get("finding_id"), [])
            if h.get("status") in ("supported", "refuted")
        ]
        if len(tested) < 2:
            unexplained += 1

    cited = 0
    if answer:
        cited = len(answer.get("evidence") or answer.get("evidence_query_ids") or [])

    return score(
        RunFacts(
            executed_queries=len(executed),
            cited_queries=cited,
            material_findings=len(material),
            tested_hypotheses=len(terminal),
            refuted_hypotheses=len([h for h in hypotheses if h.get("status") == "refuted"]),
            inconclusive_hypotheses=len(
                [h for h in hypotheses if h.get("status") == "inconclusive"]
            ),
            unexplained_findings=unexplained,
            truncated_results=len([q for q in executed if q.get("truncated")]),
            empty_results=len([q for q in executed if q.get("row_count") == 0]),
            blocked_queries=len([q for q in queries if q.get("verdict") == "rejected"]),
            escalated_queries=len([q for q in queries if q.get("verdict") == "escalated"]),
            run_truncated=run.get("status") == "truncated",
            stated_band=(answer or {}).get("confidence"),
        )
    )
