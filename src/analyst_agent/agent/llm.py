"""The single Anthropic entry point.

This is the only module in the project that imports ``anthropic``. Nodes describe *what* they
want - a decision, a plan, a piece of SQL - and this wrapper owns model id, thinking, effort,
prompt caching, structured outputs, refusal handling, retries and token accounting. Keeping that
in one place is why the project uses the SDK directly rather than a LangChain LLM abstraction:
each of those is a lever worth controlling, and the abstraction hides them.

Conventions, all current-API rather than remembered:

* ``claude-opus-5``, never date-suffixed.
* ``thinking={"type": "adaptive"}`` - ``budget_tokens`` is rejected outright on this model.
* The cost lever is ``output_config={"effort": ...}``, tiered per node: cheap classification at
  ``low``, SQL authoring at ``high``, the reasoning the project is judged on at ``xhigh``.
* The schema card, metric registry and safety rules form a large **stable prefix**, so the
  system block carries ``cache_control`` and the volatile question goes after it. Cache
  effectiveness is measured, not assumed - ``Usage.cache_read_tokens`` is recorded on every call.
* ``stop_reason == "refusal"`` is checked before the content is read, and server-side fallbacks
  are enabled by default so a refusal reroutes rather than failing the run.
* Streaming for large ``max_tokens``, because a long non-streaming request hits the HTTP timeout.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import anthropic
from pydantic import BaseModel

from analyst_agent.config import Settings, get_settings
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
T = TypeVar("T", bound=BaseModel)

# Server-side fallbacks: on a safety refusal the request is rerouted by category rather than
# failing. "default" means we never have to maintain a model list.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Streaming is required above this, or a long generation trips the HTTP timeout.
STREAM_ABOVE_TOKENS = 16_000

RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
)


class LLMRefusalError(RuntimeError):
    """The model declined, and the fallback declined too."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        self.category = category
        self.explanation = explanation
        super().__init__(
            f"the model refused this request (category: {category or 'unspecified'})"
            + (f": {explanation}" if explanation else "")
        )


@dataclass(frozen=True)
class LLMResponse:
    """What a node gets back."""

    text: str
    parsed: Any | None
    tool_calls: list[dict[str, Any]]
    stop_reason: str | None
    usage: repo.Usage
    duration_ms: int

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == "tool_use"


class LLM:
    """A thin, opinionated wrapper over the Messages API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # require_api_key fails loudly here rather than at the first call deep in a run.
            self._client = anthropic.Anthropic(api_key=self._settings.require_api_key())
        return self._client

    @property
    def model(self) -> str:
        return self._settings.analyst_model

    # --- prompt construction ---------------------------------------------

    @staticmethod
    def cached_system(stable: str, volatile: str | None = None) -> list[dict[str, Any]]:
        """A system prompt split so the expensive part can be cached.

        The stable block - schema card, metric catalogue, safety rules - is identical on every
        call in a run and carries the cache breakpoint. Anything that varies goes *after* it,
        because a single changed byte anywhere in the prefix invalidates everything following it.
        """
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
        ]
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return blocks

    # --- the call --------------------------------------------------------

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
        """One turn.

        ``response_model`` switches on structured outputs, so a node that needs a decision gets a
        validated object instead of prose it has to parse. ``tools`` and ``response_model`` are
        mutually exclusive - a turn either calls a tool or produces a final shape.
        """
        if tools and response_model:
            raise ValueError(
                "a turn either calls tools or produces a structured result, not both"
            )

        max_tokens = max_tokens or (
            self._settings.max_tokens_streaming
            if response_model is None and tools is None
            else self._settings.max_tokens_nonstreaming
        )

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if tools:
            request["tools"] = tools
        if response_model is not None:
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": response_model.model_json_schema(),
            }

        started = time.monotonic()
        message = self._send(request, max_retries=max_retries)
        duration_ms = int((time.monotonic() - started) * 1000)

        # Checked before the content is read: on a refusal there is no answer to parse.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        tool_calls = [
            {"id": block.id, "name": block.name, "input": block.input}
            for block in message.content
            if getattr(block, "type", None) == "tool_use"
        ]

        usage = self._usage(message)
        parsed = self._parse(text, response_model) if response_model is not None else None

        log.info(
            "llm turn",
            effort=effort,
            stop_reason=message.stop_reason,
            tool_calls=len(tool_calls),
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cache_read_tokens=usage.cache_read_tokens,
            duration_ms=duration_ms,
        )
        return LLMResponse(
            text=text,
            parsed=parsed,
            tool_calls=tool_calls,
            stop_reason=message.stop_reason,
            usage=usage,
            duration_ms=duration_ms,
        )

    def structured(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        response_model: type[T],
        effort: Effort = "high",
        max_tokens: int | None = None,
    ) -> tuple[T, repo.Usage]:
        """A turn that must produce a specific shape.

        Returns the validated model rather than the whole response, so a node reads a typed
        object instead of unwrapping ``parsed`` and asserting it is not None.
        """
        response = self.complete(
            system=system,
            messages=messages,
            effort=effort,
            max_tokens=max_tokens,
            response_model=response_model,
        )
        if not isinstance(response.parsed, response_model):
            raise ValueError(
                f"expected {response_model.__name__} from the model, got "
                f"{type(response.parsed).__name__}"
            )
        return response.parsed, response.usage

    def _send(self, request: dict[str, Any], max_retries: int) -> Any:
        """Send, retrying only what is worth retrying.

        A most-specific-first chain rather than one broad except: a 404 or a malformed request
        will fail identically on the next attempt, and retrying it wastes budget and hides the
        real error behind a timeout.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                if request["max_tokens"] > STREAM_ABOVE_TOKENS:
                    with self.client.beta.messages.stream(**request) as stream:
                        return stream.get_final_message()
                return self.client.beta.messages.create(**request)
            except anthropic.NotFoundError:
                raise
            except anthropic.BadRequestError:
                raise
            except anthropic.AuthenticationError:
                raise
            except RETRYABLE as exc:
                if attempt > max_retries:
                    log.error("llm call failed after retries", attempts=attempt, error=str(exc))
                    raise
                delay = min(2 ** (attempt - 1), 8)
                log.warning(
                    "llm call retrying",
                    attempt=attempt,
                    delay_seconds=delay,
                    error_type=type(exc).__name__,
                )
                time.sleep(delay)
            except anthropic.APIStatusError as exc:
                log.error("llm call failed", status=exc.status_code, error=str(exc))
                raise

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _usage(message: Any) -> repo.Usage:
        usage = getattr(message, "usage", None)
        if usage is None:
            return repo.Usage()
        return repo.Usage(
            tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
            tokens_out=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    @staticmethod
    def _parse(text: str, response_model: type[BaseModel]) -> BaseModel:
        """Validate a structured response.

        Parsed with ``json.loads`` rather than by matching on the serialised string: the current
        models vary their JSON escaping, and string matching on tool or structured output is a
        known way to break intermittently.
        """
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"structured output was not valid JSON: {text[:200]}"
            ) from exc
        return response_model.model_validate(payload)


_llm: LLM | None = None


def get_llm() -> LLM:
    """The configured backend.

    ``LLM_PROVIDER=groq`` returns the Groq subclass, which honours the same contract. The
    import is local so that a deployment using only one provider does not need the other's SDK
    resolvable at module load.
    """
    global _llm
    if _llm is None:
        if get_settings().llm_provider == "groq":
            from analyst_agent.agent.llm_groq import GroqLLM

            _llm = GroqLLM()
        else:
            _llm = LLM()
    return _llm


def reset_llm() -> None:
    """Drop the cached backend. For tests, and after a settings change."""
    global _llm
    _llm = None
