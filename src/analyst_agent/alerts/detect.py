"""Anomaly detection over approved metrics.

An alert watches **an approved metric**, never a free-text query. That is the same rule the agent
itself follows, and it matters more here: an alert runs unattended on a schedule, so a definition
somebody invented once would keep firing about a number nobody agreed on. The metric registry
renders the statement, `sql_guard` validates it, and the read runs as `analyst_ro` like every other
query in this system.

Two families of comparison, because "is this wrong" has two different meanings:

* **`drop` and `spike`** are relative to a *baseline*: the mean of the periods before the latest
  one. This is what "revenue dropped" or "sales spiked" actually mean — a change against how things
  have been, not against a number typed a year ago.
* **`below` and `above`** are absolute thresholds. Useful when a floor is contractual rather than
  statistical: an on-time delivery rate below 90% is a problem whatever last month looked like.

**The baseline excludes the observed period.** Including it would dilute the very change being
detected: a large enough drop pulls the mean down with it, and a single-period window compares a
value to itself and can never fire — which reads as "nothing is wrong". The schema enforces at
least two periods for that reason.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

Comparison = Literal["drop", "spike", "below", "above"]

# A period whose baseline is near zero cannot produce a meaningful percentage change: a move from
# 0.01 to 1 is a 9,900% spike and tells nobody anything.
NEGLIGIBLE = 1e-9


@dataclass(frozen=True)
class Observation:
    """A metric's recent series, newest last."""

    periods: tuple[str, ...]
    values: tuple[float, ...]

    @property
    def latest(self) -> float | None:
        return self.values[-1] if self.values else None

    @property
    def latest_period(self) -> str | None:
        return self.periods[-1] if self.periods else None

    def baseline(self) -> float | None:
        """The mean of everything before the latest period.

        The mean rather than the previous period alone: one quiet month next to a noisy series is
        not an anomaly, and comparing only to the immediately preceding period turns ordinary
        variance into a page.
        """
        prior = self.values[:-1]
        return statistics.fmean(prior) if prior else None

    def volatility(self) -> float | None:
        """Standard deviation of the prior periods, or None when there are too few."""
        prior = self.values[:-1]
        return statistics.stdev(prior) if len(prior) >= 2 else None


@dataclass(frozen=True)
class Verdict:
    triggered: bool
    detail: str
    observed: float | None = None
    baseline: float | None = None
    change_pct: float | None = None
    period: str | None = None
    """The period the observation came from, so an event says *when*, not only *what*."""


def judge(
    observation: Observation, comparison: Comparison, threshold: float
) -> Verdict:
    """Decide whether an observation breaches an alert.

    Pure arithmetic over the series, with no database and no metric registry, so the rule can be
    tested against hand-written numbers. Every path returns a Verdict with a sentence in it: an
    alert that fires without saying what it saw is one somebody will disable.
    """
    latest = observation.latest
    period = observation.latest_period

    if latest is None:
        return Verdict(
            triggered=False,
            detail="the metric returned no periods, so there is nothing to compare",
            period=period,
        )

    if comparison == "below":
        breached = latest < threshold
        return Verdict(
            triggered=breached,
            detail=(
                f"{_n(latest)} is below the floor of {_n(threshold)}"
                if breached
                else f"{_n(latest)} is at or above the floor of {_n(threshold)}"
            ),
            observed=latest,
            period=period,
        )

    if comparison == "above":
        breached = latest > threshold
        return Verdict(
            triggered=breached,
            detail=(
                f"{_n(latest)} is above the ceiling of {_n(threshold)}"
                if breached
                else f"{_n(latest)} is at or below the ceiling of {_n(threshold)}"
            ),
            observed=latest,
            period=period,
        )

    baseline = observation.baseline()
    if baseline is None:
        return Verdict(
            triggered=False,
            detail=(
                "only one period is available, so there is no baseline to compare against - a "
                "value compared to itself can never move"
            ),
            observed=latest,
            period=period,
        )
    if abs(baseline) < NEGLIGIBLE:
        return Verdict(
            triggered=False,
            detail=(
                f"the baseline is effectively zero ({_n(baseline)}), so a percentage change would "
                "be arithmetic rather than information"
            ),
            observed=latest,
            baseline=baseline,
            period=period,
        )

    change = (latest - baseline) / abs(baseline) * 100.0

    if comparison == "drop":
        breached = change <= -abs(threshold)
        direction = "fell" if change < 0 else "rose"
        return Verdict(
            triggered=breached,
            detail=(
                f"{_n(latest)} {direction} {abs(change):.1f}% against a baseline of "
                f"{_n(baseline)}"
                + (
                    f", past the {abs(threshold):.1f}% drop this alert watches for"
                    if breached
                    else f"; the alert fires at a {abs(threshold):.1f}% drop"
                )
            ),
            observed=latest,
            baseline=baseline,
            change_pct=change,
            period=period,
        )

    breached = change >= abs(threshold)
    direction = "rose" if change > 0 else "fell"
    return Verdict(
        triggered=breached,
        detail=(
            f"{_n(latest)} {direction} {abs(change):.1f}% against a baseline of {_n(baseline)}"
            + (
                f", past the {abs(threshold):.1f}% spike this alert watches for"
                if breached
                else f"; the alert fires at a {abs(threshold):.1f}% spike"
            )
        ),
        observed=latest,
        baseline=baseline,
        change_pct=change,
        period=period,
    )


def _n(value: float) -> str:
    """A number a person can read in an alert line."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if abs(value) >= 1_000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


# --- running one against the warehouse ---------------------------------------


def evaluate(
    alert: dict[str, Any], organization_id: uuid.UUID | None = None
) -> tuple[Verdict, uuid.UUID | None]:
    """Compute the metric this alert watches and judge the result.

    Goes through ``metric_query`` rather than issuing SQL, so an alert's read is validated by the
    guard, audited in ``sql_audit`` and attributable afterwards — an unattended query with no
    audit row is the one nobody can explain later.

    **An evaluation creates a real run**, owned by the alert's organisation. It could have used a
    throwaway id, and that is what the first version did — until the foreign key on ``tool_calls``
    refused it, correctly: a tool call with no run is a query nobody can trace back to a reason.
    So an alert check appears in the trace like any other piece of work, attributed to the alert
    that asked for it.

    Returns the verdict and the ``query_id``, so an alert event can point at the statement that
    produced it. That id is the difference between "revenue dropped 18%" and "revenue dropped 18%,
    here is the SQL".
    """
    from analyst_agent.db import repository as repo
    from analyst_agent.tools.registry import get_tool_registry

    dimension = alert.get("dimension") or "month"
    window = int(alert.get("window_periods") or 6)
    owner_run = repo.create_run(
        f"alert check: {alert.get('name', alert['metric'])}",
        requested_by="alert",
        organization_id=organization_id,
    )

    result = get_tool_registry().invoke(
        "metric_query",
        {
            "metric": alert["metric"],
            "dimensions": [dimension],
            "date_from": None,
            "date_to": None,
            "filters": None,
            "rank_by_value": False,
            "purpose": f"alert '{alert.get('name', '')}': {alert['metric']} by {dimension}",
        },
        owner_run,
        None,
    )

    query_id = result.data.get("query_id")
    query_uuid = uuid.UUID(query_id) if query_id else None

    if result.refused or not result.ok:
        repo.finish_run(
            owner_run,
            "failed",
            error={"type": "AlertMetricFailed", "message": result.summary},
        )
        return (
            Verdict(
                triggered=False,
                detail=f"the metric could not be computed: {result.summary}",
            ),
            query_uuid,
        )

    observation = _series(result.data, dimension, window)
    verdict = judge(observation, alert["comparison"], float(alert["threshold"]))
    # Closed either way, so an alert check does not sit on the dashboard as work in flight.
    repo.finish_run(
        owner_run,
        "completed",
        answer={
            "conclusion": verdict.detail,
            "confidence": "high",
            "evidence": [{"query_id": str(query_uuid)}] if query_uuid else [],
        },
    )
    log.info(
        "alert evaluated",
        alert=alert.get("name"),
        metric=alert["metric"],
        triggered=verdict.triggered,
        observed=verdict.observed,
    )
    return verdict, query_uuid


def _series(data: dict[str, Any], dimension: str, window: int) -> Observation:
    """Pull an ordered series out of a metric result.

    The rows come back ordered by dimension, so the last `window` of them are the most recent
    periods. A row whose measure will not parse as a number is dropped rather than coerced to
    zero: a zero is a value, and inventing one would move the baseline.
    """
    rows = data.get("rows") or []
    columns = data.get("columns") or []
    measure = next((c for c in columns if c not in (dimension, "period")), None)
    if measure is None:
        return Observation(periods=(), values=())

    periods: list[str] = []
    values: list[float] = []
    for row in rows:
        raw = row.get(measure)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        periods.append(str(row.get(dimension, "")))
        values.append(value)

    if window and len(values) > window:
        periods, values = periods[-window:], values[-window:]
    return Observation(periods=tuple(periods), values=tuple(values))
