"""Tool 1: resolve a business term to an approved metric definition.

This is the tool that makes "business metrics use approved definitions" true rather than
aspirational. It either returns a reviewed definition — with its version, its caveats and the
dimensions it supports — or refuses, and a refusal is a real answer the graph must act on.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from analyst_agent.metrics.registry import MetricRegistry, NotApproved, get_registry
from analyst_agent.tools.base import Tool, ToolResult


class MetricLookupInput(BaseModel):
    model_config = {"extra": "forbid"}

    term: str = Field(
        description=(
            "The business term as the question phrased it, for example 'revenue', 'AOV', "
            "'delivery performance'. Do not translate it into a formula."
        )
    )
    include_all: bool | None = Field(
        default=None,
        description=(
            "Pass true to list every approved metric instead of resolving one term. Use this "
            "when you need to see what definitions exist."
        ),
    )


class MetricLookupTool(Tool[MetricLookupInput]):
    name = "metric_lookup"
    description = """
Resolve a business term to its approved metric definition.

Always call this before computing any business metric. The returned definition names the
measure, the tables, the filter, the dimensions you may break it down by, and the caveats you
must carry into the answer.

If the term has no approved definition, this returns refused=true with the closest matches. In
that case you must either ask the user which definition they mean, or state explicitly in your
answer that you are using an ad-hoc definition. You must not invent a formula and present the
number as if it were an approved company metric.
"""
    input_model = MetricLookupInput

    def __init__(self, registry: MetricRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def run(
        self, payload: MetricLookupInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        if payload.include_all:
            catalogue = [
                {
                    "name": d.name,
                    "title": d.title,
                    "unit": d.unit,
                    "grain": d.grain,
                    "dimensions": sorted(d.dimensions),
                    "aliases": sorted(d.aliases),
                }
                for d in self._registry.all()
            ]
            return ToolResult.succeed(
                f"{len(catalogue)} approved metrics", metrics=catalogue
            )

        found = self._registry.lookup(payload.term)
        if isinstance(found, NotApproved):
            return ToolResult.refuse(
                found.message,
                term=payload.term,
                closest_matches=list(found.closest_matches),
                approved_metrics=list(self._registry.names),
                guidance=(
                    "Ask the user which definition they mean, or say plainly in your answer "
                    "that this figure uses an ad-hoc definition rather than an approved one."
                ),
            )

        return ToolResult.succeed(
            f"{payload.term!r} resolves to {found.qualified_version} ({found.title})",
            metric=found.name,
            definition_version=found.qualified_version,
            title=found.title,
            description=" ".join(found.description.split()),
            unit=found.unit,
            grain=found.grain,
            shape=found.shape,
            dimensions={
                name: dimension.label for name, dimension in found.dimensions.items()
            },
            caveats=[" ".join(c.split()) for c in found.caveats],
            owner=found.owner,
        )
