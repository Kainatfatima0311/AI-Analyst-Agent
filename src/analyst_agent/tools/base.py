"""Common machinery for the agent's tools.

Every tool is the same four things: a pydantic input model, an Anthropic tool schema generated
from it, an executor, and a mandatory audit write. Keeping that shape in one place means a new
tool cannot quietly skip the audit.

Two conventions matter more than they look:

* **A refusal is a result, not an error.** When a tool declines — no approved metric for that
  term, a query the guard rejected, a chart that would be unreadable — it returns
  ``ToolResult.refuse(...)`` with ``ok=True``. Conflating that with a crash would lose the
  distinction in the trace, and would teach the model to retry rather than to change course.
* **Nothing returns silently empty.** An empty result is a finding the model must be told
  about, not an absence it should paper over.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)



@dataclass
class ToolResult:
    """What a tool hands back to the graph."""

    ok: bool
    summary: str
    """One line for the model: what happened, in plain language."""
    data: dict[str, Any] = field(default_factory=dict)
    refusal: str | None = None
    error: dict[str, Any] | None = None
    audit: dict[str, Any] = field(default_factory=dict)
    """Extra fields worth recording in tool_calls but not worth sending to the model."""

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    @classmethod
    def succeed(cls, summary: str, **data: Any) -> ToolResult:
        return cls(ok=True, summary=summary, data=data)

    @classmethod
    def refuse(cls, reason: str, **data: Any) -> ToolResult:
        """The tool did its job by declining. ok stays True."""
        return cls(ok=True, summary=reason, refusal=reason, data=data)

    @classmethod
    def fail(cls, summary: str, kind: str, message: str, **data: Any) -> ToolResult:
        return cls(
            ok=False, summary=summary, data=data, error={"type": kind, "message": message}
        )

    def for_model(self) -> dict[str, Any]:
        """The payload the model sees. Never includes raw audit detail."""
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary, **self.data}
        if self.refusal:
            payload["refused"] = True
        if self.error:
            payload["error"] = self.error
        return payload


def anthropic_tool_schema(
    model: type[BaseModel], name: str, description: str
) -> dict[str, Any]:
    """Build a ``strict``-compatible Anthropic tool definition from a pydantic model.

    Strict tool use requires ``additionalProperties: false`` and every property listed in
    ``required``. Optional arguments are therefore declared as nullable rather than omitted —
    the model must pass the key and may pass null. That is why the input models below use
    ``X | None = None`` rather than leaving fields out.
    """
    return {
        "name": name,
        "description": description.strip(),
        "input_schema": strict_schema(model),
        "strict": True,
    }


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A pydantic model's JSON Schema, tightened for strict mode.

    Shared with the Groq backend, which enforces the same two rules on a structured-output
    schema as strict tool use does here - and rejects the whole request rather than relaxing
    them. One implementation means a schema accepted in one place is accepted in both.
    """
    schema = model.model_json_schema()
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    """Recursively require every property and forbid unknown ones."""
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node.setdefault("additionalProperties", False)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = sorted(properties)
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)


class Tool[TInput: BaseModel](ABC):
    """Base class for the agent's tools.

    Subclasses implement ``run``; ``invoke`` handles validation, timing, the audit write and
    logging, so those cannot be forgotten.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return anthropic_tool_schema(cls.input_model, cls.name, cls.description)

    @abstractmethod
    def run(self, payload: TInput, run_id: uuid.UUID, step_id: uuid.UUID | None) -> ToolResult:
        """Do the work. Raise only for genuinely unexpected failures."""

    def invoke(
        self,
        arguments: dict[str, Any],
        run_id: uuid.UUID,
        step_id: uuid.UUID | None = None,
        audit: bool = True,
    ) -> ToolResult:
        """Validate, run, time, and record. The only entry point the graph uses."""
        started = time.monotonic()

        try:
            payload = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            # Invalid arguments are the model's mistake to correct, so they come back as a
            # readable failure rather than an exception that aborts the step.
            result = ToolResult.fail(
                f"{self.name} was called with invalid arguments",
                kind="ValidationError",
                message="; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                ),
            )
        else:
            try:
                result = self.run(payload, run_id, step_id)  # type: ignore[arg-type]
            except Exception as exc:
                # Recorded in the audit, then reported to the model rather than aborting the
                # step: a tool crash is information the graph can act on.
                log.exception("tool raised", tool=self.name)
                result = ToolResult.fail(
                    f"{self.name} failed unexpectedly",
                    kind=type(exc).__name__,
                    message=str(exc),
                )

        duration_ms = int((time.monotonic() - started) * 1000)

        if audit:
            repo.record_tool_call(
                run_id=run_id,
                tool=self.name,
                arguments=arguments,
                ok=result.ok,
                step_id=step_id,
                result_summary={"summary": result.summary, **result.audit},
                refusal=result.refusal,
                error=result.error,
                duration_ms=duration_ms,
            )

        return result
