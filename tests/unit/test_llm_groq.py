"""The Groq backend's translation layer.

Everything here runs without a key and without a network call. That is the point: the risk in a
provider adapter is not the HTTP request, it is the *shape* conversion — an assistant turn whose
tool calls are dropped, or a tool result attached to nothing, fails at the second turn of a run
and looks like a model problem. These tests pin the conversion in both directions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar

import groq
import pytest
from pydantic import BaseModel

from analyst_agent.agent.llm_groq import (
    GroqLLM,
    _loads_arguments,
    _looks_like_schema_rejection,
    _system_text,
    _to_chat_messages,
    _to_chat_tools,
    _usage_of,
    _with_schema_in_system,
)
from analyst_agent.config import Settings


class Decision(BaseModel):
    answerable: bool
    reason: str


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_provider": "groq",
        "groq_api_key": "test-key",
        "db_rw_dsn": "postgresql://u:p@localhost:5432/x",
        "db_ro_dsn": "postgresql://u:p@localhost:5432/x",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- fake transport ------------------------------------------------------------


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str | None = None
    tool_calls: list[_FakeToolCall] | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _FakeCompletion:
    choices: list[_FakeChoice]
    usage: _FakeUsage


class _FakeCompletions:
    """Records requests and replays scripted responses."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(responses)})()

    @property
    def completions(self) -> _FakeCompletions:
        return self.chat.completions  # type: ignore[no-any-return]


def _llm(responses: list[Any], **overrides: Any) -> tuple[GroqLLM, _FakeClient]:
    llm = GroqLLM(settings=_settings(**overrides))
    client = _FakeClient(responses)
    llm._groq = client  # type: ignore[assignment]
    return llm, client


def _text(body: str, finish: str = "stop", tokens: tuple[int, int] = (10, 5)) -> _FakeCompletion:
    return _FakeCompletion(
        choices=[_FakeChoice(_FakeMessage(content=body), finish)],
        usage=_FakeUsage(*tokens),
    )


# --- system prompt -------------------------------------------------------------


def test_cached_system_blocks_are_flattened_in_order() -> None:
    """The cache breakpoint has no meaning here, but the ordering still does."""
    blocks = GroqLLM.cached_system("STABLE RULES", "volatile question")
    assert _system_text(blocks) == "STABLE RULES\n\nvolatile question"


def test_a_plain_string_system_prompt_passes_through() -> None:
    assert _system_text("just this") == "just this"


def test_empty_blocks_do_not_leave_blank_separators() -> None:
    assert _system_text([{"type": "text", "text": ""}, {"type": "text", "text": "a"}]) == "a"


# --- message translation -------------------------------------------------------


def test_the_system_prompt_becomes_the_first_message() -> None:
    out = _to_chat_messages("rules", [{"role": "user", "content": "question"}])
    assert out[0] == {"role": "system", "content": "rules"}
    assert out[1] == {"role": "user", "content": "question"}


def test_an_assistant_tool_use_becomes_tool_calls_with_string_arguments() -> None:
    """Arguments are an object on the Anthropic path and a JSON string here."""
    out = _to_chat_messages(
        "rules",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking the schema"},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "schema_inspector",
                        "input": {"tables": ["orders"]},
                    },
                ],
            },
        ],
    )
    assistant = out[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking the schema"
    call = assistant["tool_calls"][0]
    assert call == {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "schema_inspector",
            "arguments": json.dumps({"tables": ["orders"]}),
        },
    }


def test_a_tool_calling_assistant_turn_with_no_text_still_carries_content() -> None:
    """The key must be present, not absent, or the API rejects the message."""
    out = _to_chat_messages(
        "rules",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "c", "name": "metric_lookup", "input": {}}
                ],
            }
        ],
    )
    assert "content" in out[-1]
    assert out[-1]["content"] is None


def test_each_tool_result_becomes_its_own_tool_message() -> None:
    """One user message holding several results has no equivalent in this dialect.

    The tool loop deliberately returns every result in a single user turn, because the Messages
    API requires exactly that. Splitting them here is the whole reason this adapter exists.
    """
    out = _to_chat_messages(
        "rules",
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "12 rows"},
                    {"type": "tool_result", "tool_use_id": "c2", "content": "revenue@v1"},
                    {"type": "text", "text": "now plan the next step"},
                ],
            }
        ],
    )
    assert [m["role"] for m in out] == ["system", "tool", "tool", "user"]
    assert out[1] == {"role": "tool", "tool_call_id": "c1", "content": "12 rows"}
    assert out[2] == {"role": "tool", "tool_call_id": "c2", "content": "revenue@v1"}
    # The instruction follows the results it comments on, not the other way round.
    assert out[3] == {"role": "user", "content": "now plan the next step"}


def test_a_full_tool_loop_round_trip_keeps_calls_and_results_paired() -> None:
    """The end-to-end shape a second loop turn depends on."""
    out = _to_chat_messages(
        "rules",
        [
            {"role": "user", "content": "why did revenue drop"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "x1", "name": "metric_query", "input": {"m": 1}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x1", "content": "3 rows"}
                ],
            },
        ],
    )
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["id"] == out[3]["tool_call_id"]


# --- tool definitions ----------------------------------------------------------


def test_input_schema_becomes_parameters_and_strict_is_dropped() -> None:
    schema = {"type": "object", "properties": {"term": {"type": "string"}}}
    out = _to_chat_tools(
        [{"name": "metric_lookup", "description": "resolve", "input_schema": schema, "strict": True}]
    )
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "metric_lookup",
                "description": "resolve",
                "parameters": schema,
            },
        }
    ]


def test_a_tool_with_no_schema_still_produces_a_valid_object() -> None:
    out = _to_chat_tools([{"name": "t", "description": "d"}])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# --- argument parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("", {}),
        (None, {}),
        ("not json", {}),
        ("[1, 2]", {}),  # a list is not a tool payload
    ],
)
def test_tool_arguments_degrade_to_an_empty_payload_rather_than_raising(
    raw: str | None, expected: dict[str, Any]
) -> None:
    """A model recovers from a tool refusal; it cannot recover from a traceback."""
    assert _loads_arguments(raw) == expected


# --- usage ---------------------------------------------------------------------


def test_usage_is_mapped_and_cache_reads_are_reported_as_zero() -> None:
    """There is no prompt caching here, and saying zero is more honest than omitting it."""
    usage = _usage_of(_FakeCompletion(choices=[], usage=_FakeUsage(120, 34)))
    assert (usage.tokens_in, usage.tokens_out, usage.cache_read_tokens) == (120, 34, 0)


def test_a_response_with_no_usage_block_yields_zeroes() -> None:
    usage = _usage_of(object())
    assert usage.tokens_in == 0


# --- one turn ------------------------------------------------------------------


def test_a_text_turn_returns_text_and_accounts_tokens() -> None:
    llm, _ = _llm([_text("the answer", tokens=(200, 40))])
    response = llm.complete(system="rules", messages=[{"role": "user", "content": "q"}])
    assert response.text == "the answer"
    assert response.usage.tokens_in == 200
    assert response.stop_reason == "stop"
    assert not response.wants_tools


def test_a_tool_call_turn_reports_tool_use_so_the_loop_continues() -> None:
    """The loop routes on ``stop_reason == "tool_use"``; this API says ``tool_calls``."""
    completion = _FakeCompletion(
        choices=[
            _FakeChoice(
                _FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall("c1", _FakeFunction("metric_query", '{"metric": "revenue"}'))
                    ],
                ),
                "tool_calls",
            )
        ],
        usage=_FakeUsage(10, 2),
    )
    llm, _ = _llm([completion])
    response = llm.complete(
        system="rules",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "metric_query", "description": "d", "input_schema": {}}],
    )
    assert response.wants_tools
    assert response.tool_calls == [
        {"id": "c1", "name": "metric_query", "input": {"metric": "revenue"}}
    ]


def test_tools_and_a_response_model_together_are_refused() -> None:
    llm, _ = _llm([])
    with pytest.raises(ValueError, match="either calls tools or produces"):
        llm.complete(
            system="s",
            messages=[],
            tools=[{"name": "t", "description": "d", "input_schema": {}}],
            response_model=Decision,
        )


def test_structured_output_requests_a_json_schema_and_returns_a_validated_model() -> None:
    llm, client = _llm([_text('{"answerable": true, "reason": "clear"}')])
    decision, usage = llm.structured(
        system="rules", messages=[{"role": "user", "content": "q"}], response_model=Decision
    )
    assert isinstance(decision, Decision)
    assert decision.answerable is True
    assert usage.tokens_out == 5
    fmt = client.completions.requests[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Decision"


def test_reasoning_effort_is_withheld_unless_it_is_switched_on() -> None:
    """A model that does not accept the parameter rejects the whole request."""
    llm, client = _llm([_text("hi")])
    llm.complete(system="s", messages=[{"role": "user", "content": "q"}], effort="xhigh")
    assert "reasoning_effort" not in client.completions.requests[0]


def test_when_switched_on_the_top_two_effort_tiers_map_onto_high() -> None:
    llm, client = _llm([_text("hi")], groq_reasoning_effort=True)
    llm.complete(system="s", messages=[{"role": "user", "content": "q"}], effort="xhigh")
    assert client.completions.requests[0]["reasoning_effort"] == "high"


# --- the schema downgrade ------------------------------------------------------


def _bad_request(message: str) -> groq.BadRequestError:
    class _Response:
        status_code = 400
        headers: ClassVar[dict[str, str]] = {}
        request = None

    return groq.BadRequestError(message, response=_Response(), body=None)  # type: ignore[arg-type]


def test_a_model_that_rejects_json_schema_is_retried_in_json_object_mode() -> None:
    """Weaker — the shape is requested rather than enforced — but the run continues.

    ``_parse`` still validates the result against the pydantic model, so a model that ignores
    the instruction fails loudly rather than returning something malformed.
    """
    llm, client = _llm(
        [
            _bad_request("response_format json_schema is not supported for this model"),
            _text('{"answerable": false, "reason": "no approved metric"}'),
        ]
    )
    decision, _ = llm.structured(
        system="rules", messages=[{"role": "user", "content": "q"}], response_model=Decision
    )
    assert decision.reason == "no approved metric"
    first, second = client.completions.requests
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    # The schema has to reach the model somehow, so it moves into the system prompt.
    assert "JSON Schema" in second["messages"][0]["content"]


def test_a_genuine_bad_request_is_not_retried_in_a_weaker_mode() -> None:
    """Narrow on purpose: a malformed request must fail loudly."""
    llm, client = _llm([_bad_request("messages: field required")])
    with pytest.raises(groq.BadRequestError):
        llm.structured(system="s", messages=[], response_model=Decision)
    assert len(client.completions.requests) == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("json_schema is unsupported", True),
        ("invalid response_format", True),
        ("schema validation failed", True),
        ("model not found", False),
        ("rate limit reached", False),
    ],
)
def test_schema_rejection_detection(message: str, expected: bool) -> None:
    assert _looks_like_schema_rejection(_bad_request(message)) is expected


def test_the_schema_is_appended_to_an_existing_system_message() -> None:
    patched = _with_schema_in_system(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}], Decision
    )
    assert patched[0]["content"].startswith("rules")
    assert "answerable" in patched[0]["content"]
    assert patched[1] == {"role": "user", "content": "q"}


def test_the_schema_is_prepended_when_there_is_no_system_message() -> None:
    patched = _with_schema_in_system([{"role": "user", "content": "q"}], Decision)
    assert patched[0]["role"] == "system"
    assert len(patched) == 2


# --- configuration -------------------------------------------------------------


def test_the_model_id_comes_from_the_groq_setting() -> None:
    llm = GroqLLM(settings=_settings(groq_model="llama-3.3-70b-versatile"))
    assert llm.model == "llama-3.3-70b-versatile"


def test_a_missing_groq_key_fails_with_an_actionable_message() -> None:
    llm = GroqLLM(settings=_settings(groq_api_key=None))
    with pytest.raises(RuntimeError, match="GROQ_API_KEY is not set"):
        _ = llm.client


def test_a_blank_groq_key_reads_as_absent_not_as_configured() -> None:
    """The placeholder line committed in .env.example must not look like configuration."""
    assert _settings(groq_api_key="   ").groq_api_key is None


def test_the_groq_key_is_registered_for_log_redaction() -> None:
    assert "test-key" in _settings().secret_values


def test_the_provider_switch_selects_the_backend() -> None:
    from analyst_agent.agent import llm as llm_module

    settings = _settings()
    assert settings.llm_provider == "groq"
    assert settings.analyst_model_id == settings.groq_model
    assert settings.require_provider_key() == "test-key"
    # get_llm caches, so the reset hook is what makes a provider switch testable at all.
    assert callable(llm_module.reset_llm)

# --- provider-specific tool handling -------------------------------------------


def test_tool_validation_is_delegated_back_to_the_allowlist() -> None:
    """The loop's own refusal is recoverable; a 400 from the provider is not.

    Groq validates tool calls against ``tools`` server-side and fails the whole request when the
    model names something else. The tool loop is built to answer exactly that with a refusal in
    the tool result, which the model reads and corrects — so validation is switched off here and
    the allowlist stays the single authority.
    """
    llm, client = _llm([_text("hi")])
    llm.complete(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "metric_lookup", "description": "d", "input_schema": {}}],
    )
    request = client.completions.requests[0]
    assert request["disable_tool_validation"] is True
    assert request["tool_choice"] == "auto"


def test_no_tool_settings_leak_into_a_toolless_turn() -> None:
    llm, client = _llm([_text("hi")])
    llm.complete(system="s", messages=[{"role": "user", "content": "q"}])
    request = client.completions.requests[0]
    assert "tools" not in request
    assert "disable_tool_validation" not in request


def test_a_rejected_tool_call_becomes_a_turn_the_loop_can_continue_from() -> None:
    """A malformed call must not end the investigation on a 400."""
    llm, _ = _llm([_bad_request("Tool call validation failed: code tool_use_failed")])
    response = llm.complete(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "metric_query", "description": "d", "input_schema": {}}],
    )
    assert response.tool_calls == []
    assert response.stop_reason == "tool_use_failed"
    assert "argument names" in response.text


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("code tool_use_failed", True),
        ("Tool call validation failed: no such tool", True),
        ("messages: field required", False),
    ],
)
def test_tool_use_failure_detection(message: str, expected: bool) -> None:
    from analyst_agent.agent.llm_groq import _is_tool_use_failure

    assert _is_tool_use_failure(_bad_request(message)) is expected


# --- rate limits ---------------------------------------------------------------


def _status_error(status: int, message: str, retry_after: str | None = None) -> groq.APIStatusError:
    class _Response:
        status_code = status

        def __init__(self) -> None:
            self.headers = {"retry-after": retry_after} if retry_after else {}

        request = None

    return groq.APIStatusError(message, response=_Response(), body=None)  # type: ignore[arg-type]


def test_a_413_about_the_token_allowance_is_treated_as_a_wait_not_a_bad_request() -> None:
    """Groq refuses an oversized request against the remaining per-minute allowance with a 413.

    It is a wait, not a malformed request: retrying it a minute later succeeds, so failing the
    run immediately would throw away work a free-tier key could have completed.
    """
    from analyst_agent.agent.llm_groq import _is_rate_limit

    assert _is_rate_limit(_status_error(413, "rate_limit_exceeded: tokens per minute")) is True
    assert _is_rate_limit(_status_error(429, "too many requests")) is True
    assert _is_rate_limit(_status_error(413, "payload too large")) is False
    assert _is_rate_limit(_status_error(500, "server error")) is False


def test_the_servers_own_retry_after_is_honoured_over_a_guessed_backoff() -> None:
    """Guessing against a per-minute window either waits far too long or retries too early."""
    from analyst_agent.agent.llm_groq import _retry_after

    assert _retry_after(_status_error(429, "wait", retry_after="7.5"), attempt=1) == 7.5
    # Capped, so a hostile or absurd header cannot park a run indefinitely.
    assert _retry_after(_status_error(429, "wait", retry_after="9999"), attempt=1) == 65.0
    # No header, and a non-numeric one, both fall back to exponential backoff.
    assert _retry_after(_status_error(429, "wait"), attempt=3) == 4.0
    assert _retry_after(_status_error(429, "wait", retry_after="soon"), attempt=1) == 1.0


def test_a_rate_limited_call_is_retried_and_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("analyst_agent.agent.llm_groq.time.sleep", slept.append)
    llm, client = _llm(
        [_status_error(429, "rate limit reached", retry_after="2"), _text("finally")]
    )
    response = llm.complete(system="s", messages=[{"role": "user", "content": "q"}])
    assert response.text == "finally"
    assert slept == [2.0]
    assert len(client.completions.requests) == 2


def test_rate_limiting_still_gives_up_eventually(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry loop with no ceiling is how a run spends its wall clock on nothing."""
    monkeypatch.setattr("analyst_agent.agent.llm_groq.time.sleep", lambda _: None)
    llm, _ = _llm([_status_error(429, "rate limit reached") for _ in range(5)])
    with pytest.raises(groq.APIStatusError):
        llm.complete(system="s", messages=[{"role": "user", "content": "q"}], max_retries=2)


def test_the_output_cap_is_the_groq_one_not_the_anthropic_one() -> None:
    """max_completion_tokens counts against the allowance before a single token is generated."""
    llm, client = _llm([_text("hi")], groq_max_tokens=1_024)
    llm.complete(system="s", messages=[{"role": "user", "content": "q"}])
    assert client.completions.requests[0]["max_completion_tokens"] == 1_024


def test_the_base_url_is_the_host_only() -> None:
    """The SDK appends /openai/v1 itself; including it here produces a 404."""
    assert _settings().groq_base_url == "https://api.groq.com"


def test_a_structured_schema_forbids_unknown_properties() -> None:
    """This provider rejects a schema whose objects do not, so the tightening is not optional."""
    llm, client = _llm([_text('{"answerable": true, "reason": "r"}')])
    llm.structured(system="s", messages=[{"role": "user", "content": "q"}], response_model=Decision)
    schema = client.completions.requests[0]["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["answerable", "reason"]
