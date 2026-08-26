"""The Groq backend: the same wrapper surface, a different provider underneath.

``LLM`` in :mod:`analyst_agent.agent.llm` is written against the Anthropic Messages API, and the
nodes are written against ``LLM``. This subclass keeps that contract — ``complete``,
``structured``, ``cached_system``, ``LLMResponse`` — and translates it to Groq's
OpenAI-compatible chat completions API. Nothing in ``nodes/``, ``graph.py`` or ``tool_loop.py``
changes, and ``ScriptedLLM`` still fakes both.

Subclassing rather than introducing a protocol is deliberate: every annotation in the project
already says ``LLM``, and a provider swap should not require touching sixteen nodes to prove it
is safe.

**What Groq does not have, and what is done about it.**

* *Prompt caching.* There is no ``cache_control``. ``cached_system`` still splits the prompt —
  the nodes call it — and this backend simply concatenates the blocks. ``cache_read_tokens``
  is therefore always 0, which is honest rather than hidden: the run cost is the full prefix
  every turn.
* *Adaptive thinking.* Not available. Some Groq models accept ``reasoning_effort``, so the
  project's effort tiers map onto it — but only when ``GROQ_REASONING_EFFORT=true``, because a
  model that does not support the parameter rejects the whole request.
* *Server-side fallbacks and refusal stop reasons.* There is no ``stop_reason == "refusal"``.
  A refusal arrives as ordinary text, so it cannot be detected structurally; a node reads it as
  content. This is a real loss of signal and is recorded as such.

**Message translation.** The nodes and the tool loop build Anthropic-shaped messages — content
block lists, ``tool_use`` on the assistant turn, ``tool_result`` blocks in a following user
turn. Chat completions wants a flat ``content`` string, ``tool_calls`` on the assistant message,
and each result as its own ``role: "tool"`` message. :func:`_to_chat_messages` does that
conversion in one place so the rest of the project never sees it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import groq
from pydantic import BaseModel

from analyst_agent.agent.llm import LLM, Effort, LLMResponse
from analyst_agent.config import Settings
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

RETRYABLE = (
    groq.RateLimitError,
    groq.APIConnectionError,
    groq.APITimeoutError,
    groq.InternalServerError,
)

# The project's five effort tiers onto the three this API accepts. Only sent when the configured
# model is known to support the parameter - see the module docstring.
EFFORT_TO_REASONING: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


@dataclass(frozen=True)
class _Turn:
    """What one chat completion produced.

    A small carrier rather than attributes set on the SDK's own response model: those are
    pydantic models, and attaching fields to them is both fragile and a lie about their shape.
    """

    text: str
    tool_calls: list[Any]
    finish_reason: str | None
    usage: repo.Usage


def _system_text(system: str | list[dict[str, Any]]) -> str:
    """Flatten a cached-system block list into one string.

    The split exists for a cache breakpoint this provider does not have, so the blocks are
    simply joined in order. Order is preserved because the volatile half is written to follow
    the stable half.
    """
    if isinstance(system, str):
        return system
    return "\n\n".join(str(block.get("text", "")) for block in system if block.get("text"))


def _to_chat_messages(
    system: str | list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anthropic-shaped history to chat-completions-shaped history.

    Three conversions matter:

    * an assistant turn carrying ``tool_use`` blocks becomes one assistant message with
      ``tool_calls``, whose arguments are a JSON *string* rather than an object;
    * each ``tool_result`` block becomes its own ``role: "tool"`` message keyed by
      ``tool_call_id`` - a single user message holding several results has no equivalent here;
    * text blocks collapse into a plain string, because a content list is not accepted.
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": _system_text(system)}]

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content or []
        text_parts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        tool_results = [b for b in blocks if b.get("type") == "tool_result"]

        if role == "assistant":
            assistant: dict[str, Any] = {"role": "assistant"}
            # An assistant message with tool_calls may carry no text at all, and the API wants
            # the key present rather than absent.
            assistant["content"] = "\n".join(text_parts) if text_parts else None
            if tool_uses:
                assistant["tool_calls"] = [
                    {
                        "id": str(block.get("id", "")),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name", "")),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                    for block in tool_uses
                ]
            out.append(assistant)
            continue

        # A user turn. Results first, because they answer the assistant turn just echoed above;
        # any text the node added alongside them is a fresh instruction and follows.
        for block in tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": str(block.get("content", "")),
                }
            )
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})

    return out


def _to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic tool definitions to function definitions.

    ``input_schema`` becomes ``parameters``; ``strict`` is dropped because it is not a field of
    the function object here. The schemas themselves are unchanged - they are ordinary JSON
    Schema in both dialects.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


class GroqLLM(LLM):
    """``LLM`` against Groq's chat completions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._groq: groq.Groq | None = None

    @property
    def client(self) -> Any:
        if self._groq is None:
            self._groq = groq.Groq(
                api_key=self._settings.require_groq_key(),
                base_url=self._settings.groq_base_url,
            )
        return self._groq

    @property
    def model(self) -> str:
        return self._settings.groq_model

    def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        effort: Effort = "high",
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_model: type[BaseModel] | None = None,
        max_retries: int = 3,
    ) -> LLMResponse:
        """One turn, with the same contract as the Anthropic path.

        ``structured()`` is inherited and calls straight into this, so a node asking for a
        validated shape needs no knowledge of which provider answered.
        """
        if tools and response_model:
            raise ValueError(
                "a turn either calls tools or produces a structured result, not both"
            )

        request: dict[str, Any] = {
            "model": self.model,
            "messages": _to_chat_messages(system, messages),
            "max_completion_tokens": max_tokens or self._settings.max_tokens_nonstreaming,
            "temperature": self._settings.groq_temperature,
        }
        if tools:
            request["tools"] = _to_chat_tools(tools)
            request["tool_choice"] = "auto"
        if response_model is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": response_model.model_json_schema(),
                    "strict": True,
                },
            }
        if self._settings.groq_reasoning_effort:
            request["reasoning_effort"] = EFFORT_TO_REASONING[effort]

        started = time.monotonic()
        turn = self._send_chat(request, max_retries=max_retries, response_model=response_model)
        duration_ms = int((time.monotonic() - started) * 1000)

        tool_calls = [
            {
                "id": call.id,
                "name": call.function.name,
                # Arguments arrive as a JSON string here and as an object on the Anthropic path.
                # Parsing at the boundary means every caller sees the object.
                "input": _loads_arguments(call.function.arguments),
            }
            for call in turn.tool_calls
        ]
        # The tool loop routes on ``stop_reason == "tool_use"``; this API says "tool_calls".
        stop_reason = "tool_use" if tool_calls else turn.finish_reason
        text, usage = turn.text, turn.usage
        parsed = self._parse(text, response_model) if response_model is not None else None

        log.info(
            "llm turn",
            provider="groq",
            model=self.model,
            effort=effort,
            stop_reason=stop_reason,
            tool_calls=len(tool_calls),
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            duration_ms=duration_ms,
        )
        return LLMResponse(
            text=text,
            parsed=parsed,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            duration_ms=duration_ms,
        )

    def _send_chat(
        self,
        request: dict[str, Any],
        max_retries: int,
        response_model: type[BaseModel] | None,
    ) -> _Turn:
        """Send, retrying only what is worth retrying.

        One provider-specific accommodation: not every Groq model accepts a full JSON Schema in
        ``response_format``. When one rejects the schema, the request is retried once in
        ``json_object`` mode with the schema moved into the system prompt. That is weaker - the
        shape is requested rather than enforced - so it is logged at warning level rather than
        passed over silently, and ``_parse`` still validates the result against the model.
        """
        attempt = 0
        downgraded = False
        while True:
            attempt += 1
            try:
                completion = self.client.chat.completions.create(**request)
                choice = completion.choices[0]
                return _Turn(
                    text=choice.message.content or "",
                    tool_calls=list(choice.message.tool_calls or []),
                    finish_reason=choice.finish_reason,
                    usage=_usage_of(completion),
                )
            except groq.BadRequestError as exc:
                if (
                    response_model is not None
                    and not downgraded
                    and _looks_like_schema_rejection(exc)
                ):
                    downgraded = True
                    attempt = 0
                    log.warning(
                        "json_schema rejected; retrying in json_object mode",
                        model=self.model,
                        error=str(exc)[:200],
                    )
                    request["response_format"] = {"type": "json_object"}
                    request["messages"] = _with_schema_in_system(
                        request["messages"], response_model
                    )
                    continue
                raise
            except (groq.NotFoundError, groq.AuthenticationError, groq.PermissionDeniedError):
                raise
            except RETRYABLE as exc:
                if attempt > max_retries:
                    log.error("groq call failed after retries", attempts=attempt, error=str(exc))
                    raise
                delay = min(2 ** (attempt - 1), 8)
                log.warning(
                    "groq call retrying",
                    attempt=attempt,
                    delay_seconds=delay,
                    error_type=type(exc).__name__,
                )
                time.sleep(delay)
            except groq.APIStatusError as exc:
                log.error("groq call failed", status=exc.status_code, error=str(exc))
                raise


def _loads_arguments(arguments: str | None) -> dict[str, Any]:
    """Tool arguments, or an empty object rather than an exception.

    A malformed argument string is the model's error to recover from, and it recovers from a
    tool result far better than from a traceback - the tool's own pydantic validation will
    reject an empty payload with a message it can read.
    """
    if not arguments:
        return {}
    try:
        loaded = json.loads(arguments)
    except json.JSONDecodeError:
        log.warning("tool arguments were not valid JSON", arguments=arguments[:200])
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _usage_of(completion: Any) -> repo.Usage:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return repo.Usage()
    return repo.Usage(
        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
        # No prompt caching on this provider. Reported as zero rather than omitted, so a run's
        # cost accounting is not quietly flattering.
        cache_read_tokens=0,
    )


def _looks_like_schema_rejection(exc: groq.BadRequestError) -> bool:
    """Whether a 400 is about the response schema rather than about the request.

    Matched on the message because the API does not distinguish these by code. Deliberately
    narrow: a genuine malformed request must still fail loudly rather than be retried in a
    weaker mode.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("json_schema", "response_format", "schema", "structured output")
    )


def _with_schema_in_system(
    messages: list[dict[str, Any]], response_model: type[BaseModel]
) -> list[dict[str, Any]]:
    """Move the schema into the system prompt for json_object mode."""
    schema = json.dumps(response_model.model_json_schema(), indent=2)
    instruction = (
        "\n\nRespond with a single JSON object and nothing else - no prose, no code fence. "
        f"It must validate against this JSON Schema:\n{schema}"
    )
    patched = list(messages)
    if patched and patched[0].get("role") == "system":
        patched[0] = {
            "role": "system",
            "content": str(patched[0].get("content", "")) + instruction,
        }
    else:
        patched.insert(0, {"role": "system", "content": instruction.strip()})
    return patched
