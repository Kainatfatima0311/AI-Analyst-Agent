"""The tool registry: one place that knows every tool and its schema.

The graph asks for tools by name and gets back both the Anthropic schemas to send with a request
and the executor to run whatever comes back. Keeping that pairing in one place means a tool
cannot be advertised to the model without also being runnable, or run without being audited.
"""

from __future__ import annotations

import uuid
from typing import Any

from analyst_agent.observability.logging import get_logger
from analyst_agent.tools.base import Tool, ToolResult
from analyst_agent.tools.chart_builder import ChartBuilderTool
from analyst_agent.tools.metric_lookup import MetricLookupTool
from analyst_agent.tools.python_analysis import PythonAnalysisTool
from analyst_agent.tools.schema_inspector import SchemaInspectorTool
from analyst_agent.tools.sql_runner import SqlRunnerTool

log = get_logger(__name__)

# Order matters only for readability, but it is the order a run naturally uses them in.
TOOL_CLASSES: tuple[type[Tool[Any]], ...] = (
    MetricLookupTool,
    SchemaInspectorTool,
    SqlRunnerTool,
    PythonAnalysisTool,
    ChartBuilderTool,
)


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]] | None = None) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools if tools is not None else [cls() for cls in TOOL_CLASSES]:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        log.info("tool registry loaded", tools=sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(
                f"no such tool: {name!r}. Available: {', '.join(self.names)}"
            ) from exc

    def schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        """Anthropic tool definitions, in a stable order.

        Stable because the tool block is part of the cached prompt prefix: reordering it on every
        request would invalidate the cache for no reason.
        """
        names = only or [cls.name for cls in TOOL_CLASSES]
        return [self.get(name).schema() for name in names]

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        run_id: uuid.UUID,
        step_id: uuid.UUID | None = None,
    ) -> ToolResult:
        """Run a tool the model asked for.

        An unknown name is returned as a failure rather than raised: the model asking for a tool
        that does not exist is a mistake it can correct on the next turn.
        """
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolResult.fail(
                f"unknown tool {name!r}", kind="UnknownTool", message=str(exc)
            )
        return tool.invoke(arguments, run_id=run_id, step_id=step_id)


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
