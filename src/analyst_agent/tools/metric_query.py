"""Tool 6: run an approved metric without writing any SQL.

This is the tool that makes the metrics layer a guarantee rather than a lookup table.

``metric_lookup`` tells the model what a metric *means*. Until now the model then wrote its own
SQL for it, which meant the layer's central claim — that for an approved metric no free text from
the model reaches SQL — was true of the registry and not of the agent. The registry was right and
unused.

Here the model supplies only **names**: the metric, which declared dimensions to break it down
by, a date window, and dimension filters. The registry assembles the statement from reviewed
parts and binds every value as a parameter. A hostile filter value stays a value; an invented
dimension name is refused rather than interpolated.

The rendered statement still goes through ``sql_guard`` and still lands in ``sql_audit``. Nothing
here is a bypass — it is a narrower door, and the ordinary ``sql_runner`` stays available for
questions no approved metric answers.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from analyst_agent.metrics.registry import MetricRegistry, get_registry
from analyst_agent.observability.logging import get_logger
from analyst_agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    # Type-only: the tool registry constructs this tool, so importing it at runtime would be
    # circular.
    from analyst_agent.tools.registry import ToolRegistry

log = get_logger(__name__)


class MetricQueryInput(BaseModel):
    model_config = {"extra": "forbid"}

    metric: str = Field(
        description=(
            "The approved metric *name* from metric_lookup, e.g. 'revenue'. Not a formula and "
            "not SQL."
        )
    )
    dimensions: list[str] | None = Field(
        default=None,
        description=(
            "Dimension names the metric declares, e.g. ['month'] or ['month', "
            "'product_category']. A name the metric does not declare is refused - call "
            "metric_lookup to see which it has."
        ),
    )
    date_from: str | None = Field(
        default=None, description="Inclusive start, 'YYYY-MM-DD'. Bound as a parameter."
    )
    date_to: str | None = Field(
        default=None, description="Exclusive end, 'YYYY-MM-DD'. Bound as a parameter."
    )
    filters: dict[str, str] | None = Field(
        default=None,
        description=(
            "Filter a declared dimension to one value, e.g. {'month': '2018-03'}. Values are "
            "bound as parameters, never placed into the statement."
        ),
    )
    rank_by_value: bool | None = Field(
        default=None,
        description="Pass true to order by the measure descending instead of by the dimensions.",
    )
    purpose: str = Field(
        description="One sentence for the audit trail: what this establishes and what you expect."
    )


class MetricQueryTool(Tool[MetricQueryInput]):
    name = "metric_query"
    description = """
Compute an approved business metric. You do not write SQL for this.

Give the metric name, the dimensions to break it down by, and optionally a date window or a
filter. The approved definition is turned into a statement for you, so the figure you get back is
the company's definition rather than your reconstruction of it - and the answer can cite the
definition version.

Prefer this over sql_runner for anything metric_lookup resolved. Use sql_runner when the question
needs something no approved metric covers.

Dimension and filter names must be ones the metric declares; anything else is refused. Values are
bound as parameters, so they cannot change the shape of the query.
"""
    input_model = MetricQueryInput

    def __init__(
        self, registry: MetricRegistry | None = None, tools: ToolRegistry | None = None
    ) -> None:
        self._registry = registry or get_registry()
        # The SQL runner is reached lazily rather than injected, to avoid a circular import at
        # module load: the tool registry builds this tool.
        self._tools = tools

    def _sql_runner(self) -> Any:
        if self._tools is not None:
            return self._tools.get("sql_runner")
        from analyst_agent.tools.registry import get_tool_registry

        return get_tool_registry().get("sql_runner")

    def run(
        self, payload: MetricQueryInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        try:
            definition = self._registry.get(payload.metric)
        except KeyError:
            return ToolResult.refuse(
                f"{payload.metric!r} is not an approved metric",
                approved_metrics=list(self._registry.names),
                guidance="Call metric_lookup to resolve the term, or use sql_runner instead.",
            )

        try:
            rendered = self._registry.render(
                payload.metric,
                dimensions=list(payload.dimensions or []),
                date_from=payload.date_from,
                date_to=payload.date_to,
                dimension_filters=dict(payload.filters or {}),
                order_by_measure=bool(payload.rank_by_value),
            )
        except (KeyError, ValueError) as exc:
            # An undeclared dimension, or a breakdown of a custom-shaped metric. Refused with
            # what *is* available, so the next attempt can be right.
            return ToolResult.refuse(
                str(exc),
                metric=definition.name,
                available_dimensions=sorted(definition.dimensions),
                shape=definition.shape,
            )

        # Through the same door as everything else: guard, audit, read-only role.
        result = self._sql_runner().invoke(
            {
                "sql": rendered.sql,
                "purpose": f"{payload.purpose} [{rendered.definition_version}]",
                "row_limit": None,
                "parameters": rendered.params,
                "approval_id": None,
            },
            run_id,
            step_id,
            audit=False,  # this tool call is already being audited by its own invoke()
        )

        if not result.ok or result.refused:
            return ToolResult(
                ok=result.ok,
                summary=f"{rendered.definition_version}: {result.summary}",
                data={**result.data, "metric": definition.name, "sql": rendered.sql},
                refusal=result.refusal,
                error=result.error,
            )

        log.info(
            "metric computed",
            metric=definition.name,
            definition_version=rendered.definition_version,
            dimensions=list(rendered.dimensions),
        )
        return ToolResult(
            ok=True,
            summary=f"{rendered.definition_version}: {result.summary}",
            data={
                **result.data,
                "metric": definition.name,
                # Carried so the answer can cite the definition it used, not just a number.
                "definition_version": rendered.definition_version,
                "unit": rendered.unit,
                "dimensions": list(rendered.dimensions),
                "caveats": list(rendered.caveats),
                "parameters": rendered.params,
            },
            audit={
                "metric": definition.name,
                "definition_version": rendered.definition_version,
                "query_id": result.data.get("query_id"),
            },
        )
