"""The nodes where the model chooses its own tools.

Three of the five tools are only useful if the model decides when to reach for them, because
whether they help depends on what the data turned out to look like. You cannot schedule "look at
the schema first" or "chart this" from outside the run.

* ``gather_context`` — before planning: inspect the schema, resolve business terms. Fixes the
  quiet contradiction where the system prompt told the model to check the schema before writing
  SQL and it had no way to do so.
* ``analyse`` — after a result comes back: period-over-period, share-of-total, correlation on the
  frame that already exists, rather than issuing another query for something already fetched.
* ``visualize`` — before responding: the chart, carrying the ``query_id`` it was built from.

SQL authoring is deliberately **not** here. It stays on the structured-output path so exactly one
statement per turn reaches the guard and the audit; a free-running SQL tool would trade that
determinism for flexibility the agent does not need.
"""

from __future__ import annotations

import uuid
from typing import Any

from analyst_agent.agent.nodes.linear import Node, NodeContext, _history
from analyst_agent.agent.state import AnalystState, executed_query_ids
from analyst_agent.agent.tool_loop import run_tool_loop
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)


def make_gather_context(ctx: NodeContext) -> Node:
    """Let the model look at the schema and resolve terms before it plans anything."""

    def gather_context(state: AnalystState) -> dict[str, Any]:
        run_id = uuid.UUID(state["run_id"])
        budget = ctx.budget(state)

        if budget.exhausted():
            return {}

        with repo.step(
            run_id, "gather_context", effort=ctx.settings.effort_author
        ) as handle:
            loop = run_tool_loop(
                llm=ctx.llm,
                tools=ctx.tools,
                system=ctx.system(),
                messages=_history(
                    state,
                    "Before planning: check the schema for any table you have not used, and "
                    "resolve every business term in the question to an approved definition. "
                    "Filter on real column values, not guessed ones. Call the tools you need, "
                    "then stop.",
                ),
                allowed=["schema_inspector", "metric_lookup"],
                run_id=run_id,
                step_id=handle.step_id,
                effort=ctx.settings.effort_author,
                max_turns=4,
            )
            repo.finish_step(handle, summary=loop.summary, usage=loop.usage)

            # Whatever the model learned about metrics is recorded as resolution, so the answer
            # can cite a definition version rather than the model's memory of one.
            resolved: list[dict[str, Any]] = []
            for call in loop.results_for("metric_lookup"):
                data = call["data"]
                term = call["arguments"].get("term") or ""
                if not term:
                    continue
                resolved.append(
                    {
                        "term": term,
                        "metric": data.get("metric"),
                        "definition_version": data.get("definition_version"),
                        "approved": not call["refused"],
                        "note": call["summary"],
                    }
                )

            update: dict[str, Any] = {
                "budget": ctx.account(state, budget, loop.usage),
                "_context_notes": loop.text or None,
            }
            if resolved:
                update["resolved_metrics"] = resolved
            return update

    return gather_context


def make_compute_metrics(ctx: NodeContext) -> Node:
    """Compute whatever an approved metric already answers, before writing any SQL.

    This is where the metrics layer stops being a lookup table. ``metric_lookup`` told the model
    what a metric *means*; here it can compute one by naming it, and the registry assembles the
    statement. For anything an approved metric covers, no free text from the model reaches SQL —
    which is the claim the layer was built to make and, until this node existed, did not.

    ``sql_runner`` is deliberately not offered here. If the question needs something no approved
    metric covers, the run falls through to ``author_sql``, which goes through structured output
    so exactly one statement per turn reaches the guard.
    """

    def compute_metrics(state: AnalystState) -> dict[str, Any]:
        run_id = uuid.UUID(state["run_id"])
        budget = ctx.budget(state)

        approved = [m for m in state.get("resolved_metrics", []) if m.get("approved")]
        if not approved or budget.exhausted() or budget.would_exceed_queries():
            return {}

        with repo.step(run_id, "compute_metrics", effort=ctx.settings.effort_author) as handle:
            loop = run_tool_loop(
                llm=ctx.llm,
                tools=ctx.tools,
                system=ctx.system(),
                messages=_history(
                    state,
                    "Approved metrics resolved for this question:\n"
                    + "\n".join(
                        f"- {m['term']} = {m['metric']} ({m['definition_version']})"
                        for m in approved
                    )
                    + "\n\nCompute what these already answer with metric_query, naming the "
                    "dimensions and any date window. Do not write SQL for a figure an approved "
                    "metric gives you — the definition is the company's, not your "
                    "reconstruction of it. If the question needs something no approved metric "
                    "covers, say so and stop; you will get to write SQL next.",
                ),
                allowed=["metric_query"],
                run_id=run_id,
                step_id=handle.step_id,
                effort=ctx.settings.effort_author,
                max_turns=4,
            )
            repo.finish_step(handle, summary=loop.summary, usage=loop.usage)

            queries: list[dict[str, Any]] = []
            last: dict[str, Any] | None = None
            for call in loop.results_for("metric_query"):
                data = call["data"]
                query_id = data.get("query_id")
                if not query_id:
                    continue
                budget.record_query()
                record = {
                    "query_id": query_id,
                    "purpose": f"{call['arguments'].get('purpose', '')} "
                    f"[{data.get('definition_version', '')}]".strip(),
                    "verdict": data.get("verdict", "allowed"),
                    "reasons": data.get("reasons", []),
                }
                if not call["refused"] and call["ok"]:
                    record["row_count"] = data.get("row_count", 0)
                    record["truncated"] = bool(data.get("truncated"))
                    last = {
                        "query_id": query_id,
                        "columns": data.get("columns", []),
                        "rows": data.get("rows", []),
                        "row_count": record["row_count"],
                        "summary": call["summary"],
                    }
                queries.append(record)

            update: dict[str, Any] = {"budget": ctx.account(state, budget, loop.usage)}
            if queries:
                update["queries"] = queries
            if last:
                update["_last_result"] = last
            return update

    return compute_metrics


def make_analyse(ctx: NodeContext) -> Node:
    """Follow-up analysis on results that already exist, rather than another query."""

    def analyse(state: AnalystState) -> dict[str, Any]:
        run_id = uuid.UUID(state["run_id"])
        budget = ctx.budget(state)
        executed = executed_query_ids(state)

        if not executed or budget.exhausted():
            return {}

        with repo.step(run_id, "analyse", effort=ctx.settings.effort_author) as handle:
            loop = run_tool_loop(
                llm=ctx.llm,
                tools=ctx.tools,
                system=ctx.system(),
                messages=_history(
                    state,
                    "Results available to analyse further, by query_id:\n"
                    + "\n".join(f"- {query_id}" for query_id in executed)
                    + "\n\nUse python_analysis if a derived view would change what you conclude "
                    "— a period-over-period change, a share of total, a trend fit. Do not "
                    "re-query for data you already have. If nothing further is needed, say so "
                    "and stop.\n\nCorrelation is association only: use it to *generate* a "
                    "hypothesis, never to conclude one.",
                ),
                allowed=["python_analysis"],
                run_id=run_id,
                step_id=handle.step_id,
                effort=ctx.settings.effort_author,
                max_turns=4,
            )
            repo.finish_step(handle, summary=loop.summary, usage=loop.usage)
            return {
                "budget": ctx.account(state, budget, loop.usage),
                "_analysis_notes": loop.text or None,
            }

    return analyse


def make_visualize(ctx: NodeContext) -> Node:
    """Build the chart, if a chart would say something the table does not."""

    def visualize(state: AnalystState) -> dict[str, Any]:
        run_id = uuid.UUID(state["run_id"])
        budget = ctx.budget(state)
        executed = executed_query_ids(state)

        if not executed or budget.exhausted():
            return {}

        with repo.step(run_id, "visualize", effort=ctx.settings.effort_classify) as handle:
            loop = run_tool_loop(
                llm=ctx.llm,
                tools=ctx.tools,
                system=ctx.system(),
                messages=_history(
                    state,
                    "Results you can chart, by query_id:\n"
                    + "\n".join(f"- {query_id}" for query_id in executed)
                    + "\n\nBuild at most two charts, and only where a picture says something a "
                    "number does not — a trend, a mix, a relationship. One measure per chart. "
                    "Skip it entirely for a single figure: a bar chart of one bar is noise.",
                ),
                allowed=["chart_builder"],
                run_id=run_id,
                step_id=handle.step_id,
                effort=ctx.settings.effort_classify,
                max_turns=3,
            )
            repo.finish_step(handle, summary=loop.summary, usage=loop.usage)

            charts = [
                {
                    "chart_id": call["data"].get("chart_id", ""),
                    "query_id": call["data"].get("query_id", ""),
                    "title": call["arguments"].get("title"),
                    "chart_type": call["arguments"].get("chart_type", ""),
                }
                for call in loop.results_for("chart_builder")
                if call["ok"] and not call["refused"]
            ]
            if charts:
                log.info("charts built", run_id=str(run_id), count=len(charts))

            return {
                "budget": ctx.account(state, budget, loop.usage),
                "charts": charts,
            }

    return visualize
