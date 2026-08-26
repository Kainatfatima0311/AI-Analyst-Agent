"""Budget caps (control C7): what bounds a run.

A runaway loop should be *bounded*, not merely expensive. Five limits apply at once - queries,
graph iterations, tokens, wall clock, and hypotheses per finding - and the first to bind stops
the run.

Exhaustion is not an error. It routes to a partial answer marked ``truncated`` with the reason
stated, because an investigation that ran out of budget has still established something, and
saying so is more useful than either an exception or an unsupported conclusion.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from analyst_agent.config import Settings, get_settings


@dataclass
class Budget:
    """Counters plus the limits they are checked against."""

    max_queries: int
    max_iterations: int
    max_tokens: int
    max_wall_clock_seconds: int
    max_hypotheses_per_finding: int

    queries_used: int = 0
    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)
    extensions_granted: int = 0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Budget:
        settings = settings or get_settings()
        return cls(
            max_queries=settings.max_queries_per_run,
            max_iterations=settings.max_agent_iterations,
            max_tokens=settings.max_tokens_per_run,
            max_wall_clock_seconds=settings.max_run_wall_clock_seconds,
            max_hypotheses_per_finding=settings.max_hypotheses_per_finding,
        )

    @classmethod
    def restore(cls, data: dict[str, Any], settings: Settings | None = None) -> Budget:
        """Rebuild from checkpointed state after a restart.

        The wall clock deliberately restarts. The limit bounds how long the agent *works*, and
        counting an hour spent waiting for a human approval against it would make the approval
        flow self-defeating.
        """
        budget = cls.from_settings(settings)
        for key in ("queries_used", "iterations", "tokens_in", "tokens_out", "extensions_granted"):
            if key in data:
                setattr(budget, key, int(data[key]))
        for _ in range(budget.extensions_granted):
            budget._raise_ceilings()
        return budget

    # --- accounting ------------------------------------------------------

    @property
    def tokens_used(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def record_query(self) -> None:
        self.queries_used += 1

    def record_iteration(self) -> None:
        self.iterations += 1

    def record_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    def _raise_ceilings(self, factor: float = 1.5) -> None:
        self.max_queries = int(self.max_queries * factor)
        self.max_iterations = int(self.max_iterations * factor)
        self.max_tokens = int(self.max_tokens * factor)
        self.max_wall_clock_seconds = int(self.max_wall_clock_seconds * factor)

    def grant_extension(self, factor: float = 1.5) -> None:
        """Raise the ceilings after a human approves an extension (approval point 3)."""
        self.extensions_granted += 1
        self._raise_ceilings(factor)

    # --- limits ----------------------------------------------------------

    def exhausted(self) -> str | None:
        """The reason the run must stop, or None while it may continue."""
        if self.queries_used >= self.max_queries:
            return f"query budget exhausted ({self.queries_used}/{self.max_queries} queries)"
        if self.iterations >= self.max_iterations:
            return f"iteration budget exhausted ({self.iterations}/{self.max_iterations} steps)"
        if self.tokens_used >= self.max_tokens:
            return f"token budget exhausted ({self.tokens_used:,}/{self.max_tokens:,} tokens)"
        if self.elapsed_seconds >= self.max_wall_clock_seconds:
            return (
                f"time budget exhausted ({self.elapsed_seconds:.0f}s of "
                f"{self.max_wall_clock_seconds}s)"
            )
        return None

    def would_exceed_queries(self, additional: int = 1) -> bool:
        return self.queries_used + additional > self.max_queries

    def remaining(self) -> dict[str, Any]:
        """What is left, so the model can pace itself and an approval request can show it."""
        return {
            "queries": max(0, self.max_queries - self.queries_used),
            "iterations": max(0, self.max_iterations - self.iterations),
            "tokens": max(0, self.max_tokens - self.tokens_used),
            "seconds": max(0, int(self.max_wall_clock_seconds - self.elapsed_seconds)),
        }

    def to_state(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("started_monotonic", None)
        data["elapsed_seconds"] = round(self.elapsed_seconds, 1)
        data["remaining"] = self.remaining()
        return data
