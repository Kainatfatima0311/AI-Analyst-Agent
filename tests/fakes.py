"""A scripted model, so the graph can be tested without an API key.

The point is not only that CI has no key. It is that the *routing* is where the policy lives —
"stop and ask", "park on an escalation", "a spent budget produces a partial answer" — and routing
should be asserted deterministically rather than through whatever the model happens to say on the
day. The nodes take their LLM by injection precisely so this is possible.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from analyst_agent.agent.llm import LLM
from analyst_agent.db.repository import Usage


class ScriptedLLM(LLM):
    """Returns pre-built responses in order, one per structured call.

    Keyed by response model, so a test writes what each *kind* of decision should be without
    having to know how many times the graph asks for it. A queue for a model that runs out
    repeats its last entry, which keeps a loop test from needing an exact call count.
    """

    def __init__(self, script: dict[type[BaseModel], list[BaseModel]]) -> None:
        self.script = {model: list(items) for model, items in script.items()}
        self.calls: list[tuple[str, str]] = []
        self.usage_per_call = Usage(tokens_in=1200, tokens_out=300, cache_read_tokens=900)

    def structured(  # type: ignore[override]
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        response_model: type[BaseModel],
        effort: str = "high",
        max_tokens: int | None = None,
    ) -> tuple[Any, Usage]:
        queue = self.script.get(response_model)
        if not queue:
            raise AssertionError(
                f"the script has no {response_model.__name__} response left; "
                f"calls so far: {[c[0] for c in self.calls]}"
            )
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        self.calls.append((response_model.__name__, effort))
        return item, self.usage_per_call

    def effort_for(self, model_name: str) -> str | None:
        """What effort tier a given decision was asked at — the per-node tiering is a claim
        worth asserting rather than assuming."""
        for name, effort in self.calls:
            if name == model_name:
                return effort
        return None
