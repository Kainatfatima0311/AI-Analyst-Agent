"""Guard verdicts and the reason codes that justify them.

A verdict is always *explained*. Reason codes are stable strings so that tests, the audit
table and the evaluation graders can all assert on the same vocabulary, rather than matching
prose that will drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["allowed", "rejected", "escalated"]


@dataclass(frozen=True)
class Reason:
    """Why the guard reached its verdict.

    ``code`` is machine-readable and stable; ``message`` is what a reviewer reads in the
    approval dialog or the trace.
    """

    code: str
    message: str
    detail: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class GuardVerdict:
    """The single object every query carries through the system.

    ``allowed`` and ``requires_approval`` are separate because they answer different questions.
    A query can be structurally fine yet still need a human (too expensive, touches a
    restricted column); that is an *escalation*, not a rejection, and conflating the two would
    lose the distinction the approval flow depends on.
    """

    allowed: bool
    requires_approval: bool = False
    reasons: tuple[Reason, ...] = ()
    rewritten_sql: str | None = None
    referenced_objects: tuple[str, ...] = ()
    sensitive_columns: tuple[str, ...] = ()
    estimated_cost: float | None = None
    row_limit: int | None = None

    @property
    def verdict(self) -> Verdict:
        if not self.allowed:
            return "rejected"
        if self.requires_approval:
            return "escalated"
        return "allowed"

    @property
    def executable(self) -> bool:
        """True only when the query may run right now, with no human in the loop."""
        return self.allowed and not self.requires_approval

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(r.message for r in self.reasons)

    def with_reason(self, reason: Reason) -> GuardVerdict:
        """Return a copy with one more reason attached. Verdicts are immutable by design."""
        from dataclasses import replace

        return replace(self, reasons=(*self.reasons, reason))


@dataclass
class _Accumulator:
    """Internal scratch space while walking the AST."""

    rejections: list[Reason] = field(default_factory=list)
    escalations: list[Reason] = field(default_factory=list)
    notes: list[Reason] = field(default_factory=list)
    objects: set[str] = field(default_factory=set)
    sensitive: set[str] = field(default_factory=set)

    def reject(self, code: str, message: str, detail: str | None = None) -> None:
        self.rejections.append(Reason(code, message, detail))

    def escalate(self, code: str, message: str, detail: str | None = None) -> None:
        self.escalations.append(Reason(code, message, detail))

    def note(self, code: str, message: str, detail: str | None = None) -> None:
        self.notes.append(Reason(code, message, detail))

    def build(self, rewritten_sql: str | None, row_limit: int | None) -> GuardVerdict:
        allowed = not self.rejections
        return GuardVerdict(
            allowed=allowed,
            requires_approval=allowed and bool(self.escalations),
            reasons=tuple(self.rejections + self.escalations + self.notes),
            rewritten_sql=rewritten_sql if allowed else None,
            referenced_objects=tuple(sorted(self.objects)),
            sensitive_columns=tuple(sorted(self.sensitive)),
            row_limit=row_limit if allowed else None,
        )


class GuardError(Exception):
    """Raised when code tries to execute something the guard did not clear."""

    def __init__(self, verdict: GuardVerdict) -> None:
        self.verdict = verdict
        detail = "; ".join(str(r) for r in verdict.reasons) or "no reason recorded"
        super().__init__(f"query {verdict.verdict}: {detail}")
