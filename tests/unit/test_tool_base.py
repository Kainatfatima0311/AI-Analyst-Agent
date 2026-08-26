"""Tool plumbing: schemas, the result contract, and the refusal-versus-error distinction.

No database and no model. These are the conventions every tool relies on, so they are worth
pinning down separately from any one tool's behaviour.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, Field

from analyst_agent.tools.base import Tool, ToolResult, anthropic_tool_schema
from analyst_agent.tools.registry import TOOL_CLASSES, ToolRegistry


class ExampleInput(BaseModel):
    model_config = {"extra": "forbid"}

    term: str = Field(description="a required string")
    count: int | None = Field(default=None, description="an optional number")


class ExampleTool(Tool[ExampleInput]):
    name = "example"
    description = "An example tool."
    input_model = ExampleInput

    def __init__(self, behaviour: str = "succeed") -> None:
        self.behaviour = behaviour

    def run(self, payload, run_id, step_id):  # type: ignore[no-untyped-def]
        if self.behaviour == "refuse":
            return ToolResult.refuse("nothing to do here")
        if self.behaviour == "raise":
            raise RuntimeError("kaboom")
        return ToolResult.succeed(f"got {payload.term}", term=payload.term)


RUN_ID = uuid.uuid4()


# --- schemas ----------------------------------------------------------------


def test_schema_is_strict_compatible() -> None:
    """Strict tool use needs additionalProperties false and every property required."""
    schema = anthropic_tool_schema(ExampleInput, "example", "An example tool.")
    assert schema["strict"] is True
    inner = schema["input_schema"]
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {"term", "count"}


def test_optional_arguments_are_nullable_rather_than_omitted() -> None:
    """Strict mode requires every key, so optionality is expressed as a nullable type."""
    schema = anthropic_tool_schema(ExampleInput, "example", "x")
    count = schema["input_schema"]["properties"]["count"]
    types = {entry.get("type") for entry in count["anyOf"]}
    assert "null" in types


@pytest.mark.parametrize("tool_class", TOOL_CLASSES, ids=[c.name for c in TOOL_CLASSES])
def test_every_real_tool_produces_a_valid_schema(tool_class: type[Tool]) -> None:
    schema = tool_class.schema()
    assert schema["name"] == tool_class.name
    assert schema["strict"] is True
    assert schema["input_schema"]["additionalProperties"] is False
    assert schema["description"].strip()
    # The description is what the model actually reads, so it has to say something.
    assert len(schema["description"]) > 80, f"{tool_class.name} has a thin description"


@pytest.mark.parametrize("tool_class", TOOL_CLASSES, ids=[c.name for c in TOOL_CLASSES])
def test_every_argument_is_documented(tool_class: type[Tool]) -> None:
    """An undocumented argument is one the model will guess at."""
    properties = tool_class.schema()["input_schema"]["properties"]
    undocumented = [name for name, spec in properties.items() if not spec.get("description")]
    assert not undocumented, f"{tool_class.name}: {undocumented}"


# --- the result contract ----------------------------------------------------


def test_a_refusal_is_a_result_not_an_error() -> None:
    result = ExampleTool("refuse").invoke({"term": "x", "count": None}, RUN_ID, audit=False)
    assert result.ok is True
    assert result.refused is True
    assert result.error is None
    assert result.for_model()["refused"] is True


def test_success_carries_its_data() -> None:
    result = ExampleTool().invoke({"term": "hello", "count": 1}, RUN_ID, audit=False)
    assert result.ok and not result.refused
    assert result.data["term"] == "hello"
    assert "hello" in result.summary


def test_an_unexpected_exception_becomes_a_reported_failure() -> None:
    """A tool crash is information the graph can act on, not a reason to abort the step."""
    result = ExampleTool("raise").invoke({"term": "x", "count": None}, RUN_ID, audit=False)
    assert result.ok is False
    assert result.error is not None
    assert result.error["type"] == "RuntimeError"
    assert "kaboom" in result.error["message"]


def test_invalid_arguments_come_back_readable_rather_than_raising() -> None:
    result = ExampleTool().invoke({"count": "not a number"}, RUN_ID, audit=False)
    assert result.ok is False
    assert result.error is not None
    assert result.error["type"] == "ValidationError"
    assert "term" in result.error["message"]


def test_an_unknown_argument_is_rejected() -> None:
    result = ExampleTool().invoke(
        {"term": "x", "count": None, "surprise": 1}, RUN_ID, audit=False
    )
    assert result.ok is False
    assert "surprise" in (result.error or {}).get("message", "")


# --- the registry -----------------------------------------------------------


def test_the_registry_exposes_every_tool() -> None:
    registry = ToolRegistry()
    assert len(registry) == 6
    assert set(registry.names) == {
        "metric_lookup",
        # Computes an approved metric by name, so no free text reaches SQL for those figures.
        "metric_query",
        "schema_inspector",
        "sql_runner",
        "python_analysis",
        "chart_builder",
    }


def test_schemas_come_back_in_a_stable_order() -> None:
    """The tool block is part of the cached prompt prefix; reordering it would waste the cache."""
    registry = ToolRegistry()
    first = [s["name"] for s in registry.schemas()]
    second = [s["name"] for s in registry.schemas()]
    assert first == second
    assert first == [cls.name for cls in TOOL_CLASSES]


def test_an_unknown_tool_name_is_a_failure_not_an_exception() -> None:
    """The model asking for a tool that does not exist is a mistake it can correct."""
    result = ToolRegistry().invoke("nonesuch", {}, RUN_ID)
    assert result.ok is False
    assert result.error is not None
    assert "nonesuch" in result.error["message"]


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolRegistry([ExampleTool(), ExampleTool()])
