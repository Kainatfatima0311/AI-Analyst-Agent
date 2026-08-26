"""A bounded tool-calling loop, for the nodes where the model should choose.

Not every node should call tools. SQL authoring deliberately does not: it goes through structured
output so that exactly one statement per turn reaches the guard and the audit, which is what makes
the safety story deterministic. Handing the model a free-running SQL tool would trade that away
for flexibility it does not need.

But three tools are only useful if the model can decide when to reach for them — you cannot
schedule "look at the schema first" or "chart this" from outside, because whether they help
depends on what the data turned out to look like. So those nodes get a real loop, with the bounds
that keep it a loop rather than an open licence:

* a hard cap on turns, so a model that keeps calling the same tool stops rather than spending the
  run's budget;
* only the tools the node names — ``visualize`` cannot reach ``sql_runner``;
* every call goes through ``ToolRegistry.invoke``, so it is validated, timed and **audited** on
  the way through, exactly like a hardcoded call.

A refusal comes back to the model as a result, not an exception, because a refusal is information
it can act on: pick a different chart type, ask about a different table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from analyst_agent.agent.llm import LLM, Effort
from analyst_agent.db.repository import Usage
from analyst_agent.observability.logging import get_logger
from analyst_agent.tools.registry import ToolRegistry

log = get_logger(__name__)

MAX_TURNS = 6


@dataclass
class ToolLoopResult:
    """What the loop did, for the node to fold into state."""

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    hit_cap: bool = False

    @property
    def summary(self) -> str:
        names = [call["tool"] for call in self.calls]
        if not names:
            return "no tools were called"
        counted = ", ".join(sorted(set(names)))
        note = " (turn cap reached)" if self.hit_cap else ""
        return f"{len(names)} call(s) across {counted}{note}"

    def results_for(self, tool: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["tool"] == tool]


def run_tool_loop(
    llm: LLM,
    tools: ToolRegistry,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    allowed: list[str],
    run_id: uuid.UUID,
    step_id: uuid.UUID | None = None,
    effort: Effort = "high",
    max_turns: int = MAX_TURNS,
) -> ToolLoopResult:
    """Let the model use ``allowed`` tools until it stops asking, or the cap is reached."""
    result = ToolLoopResult()
    schemas = tools.schemas(only=allowed)
    conversation = list(messages)

    for turn in range(1, max_turns + 1):
        result.turns = turn
        response = llm.complete(
            system=system, messages=conversation, effort=effort, tools=schemas
        )
        result.usage = result.usage + response.usage
        if response.text:
            result.text = response.text

        if not response.tool_calls:
            return result

        # The assistant turn has to be echoed back verbatim, tool_use blocks included, or the
        # tool_result blocks below have nothing to attach to.
        conversation.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["input"],
                    }
                    for call in response.tool_calls
                ],
            }
        )

        # Every result goes back in a *single* user message. Splitting them across messages
        # silently teaches the model to stop making parallel calls.
        blocks: list[dict[str, Any]] = []
        for call in response.tool_calls:
            if call["name"] not in allowed:
                # The model asked for something this node does not offer. Refused as a result,
                # so it can choose again rather than the step failing.
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": (
                            f"{call['name']} is not available here. Available: "
                            f"{', '.join(allowed)}."
                        ),
                        "is_error": True,
                    }
                )
                continue

            outcome = tools.invoke(call["name"], call["input"], run_id, step_id)
            result.calls.append(
                {
                    "tool": call["name"],
                    "arguments": call["input"],
                    "ok": outcome.ok,
                    "refused": outcome.refused,
                    "summary": outcome.summary,
                    "data": outcome.data,
                }
            )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": str(outcome.for_model()),
                    "is_error": not outcome.ok,
                }
            )

        conversation.append({"role": "user", "content": blocks})

    result.hit_cap = True
    log.info(
        "tool loop hit its turn cap",
        turns=max_turns,
        calls=len(result.calls),
        allowed=allowed,
    )
    return result
