"""The graph, version 1: a linear walking skeleton.

    intake -> clarify_gate -> resolve_metrics -> plan -> author_sql -> execute
           -> interpret -> synthesize

Three conditional edges already, because they are where the policy lives rather than decoration
on a straight line:

* ``clarify_gate`` routes to END when the question cannot be answered as asked. Stopping to ask
  is a correct outcome, and the run stays resumable at ``clarifying`` until the answer arrives.
* ``execute`` routes to END when the guard escalated, so the run parks at ``awaiting_approval``
  rather than working around the block.
* ``author_sql`` routes straight to ``synthesize`` when the budget is spent, which is what turns
  exhaustion into a partial answer instead of an exception.

Step 8 replaces the single ``interpret -> synthesize`` edge with the investigation loop, and that
edge becomes the one that enforces "a material finding needs two tested hypotheses". The shape
here is deliberately the shape that edge will be inserted into.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph

from analyst_agent.agent.checkpointer import get_checkpointer, thread_config
from analyst_agent.agent.llm import LLM, LLMRefusalError, get_llm
from analyst_agent.agent.nodes import (
    NodeContext,
    make_author_sql,
    make_clarify_gate,
    make_execute,
    make_intake,
    make_interpret,
    make_plan,
    make_resolve_metrics,
    make_synthesize,
)
from analyst_agent.agent.nodes.linear import Node
from analyst_agent.agent.state import AnalystState, initial_state
from analyst_agent.config import Settings
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import bound, get_logger
from analyst_agent.tools.registry import ToolRegistry

log = get_logger(__name__)


def _after_clarify(state: AnalystState) -> str:
    """Stopping to ask is a correct outcome, not a failure of the run."""
    return END if state.get("status") == "clarifying" else "resolve_metrics"


def _after_author(state: AnalystState) -> str:
    """A spent budget routes to a partial answer rather than to another query."""
    if state.get("truncation_reason"):
        return "synthesize"
    return "execute" if state.get("_draft") else "synthesize"


def _after_execute(state: AnalystState) -> str:
    """An escalated query parks the run; there is no route around the approval."""
    if state.get("status") == "awaiting_approval":
        return END
    if state.get("_last_result"):
        return "interpret"
    # A rejected statement or a tool error: go and answer with what is established rather than
    # retrying blindly. Step 8 adds the repair path.
    return "synthesize"


def _add_node(builder: StateGraph, name: str, node: Node) -> None:
    """Register a node.

    The ignore is a mypy limitation rather than a defect here: ``add_node``'s overloads infer
    their state type from a concrete ``def``, and cannot bind it when the argument arrives as a
    ``Callable`` alias - which is how it arrives, because the nodes are built by factories so
    that a test can inject a scripted model. Verified empirically: the same call type-checks with
    an inline function and fails with an aliased one. The runtime behaviour is covered by the
    graph tests.
    """
    builder.add_node(name, node)  # type: ignore[call-overload]


def build_graph(
    llm: LLM | None = None,
    tools: ToolRegistry | None = None,
    settings: Settings | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the graph.

    ``llm`` and ``tools`` are injected rather than looked up, which is what lets a test drive the
    whole graph with a scripted model and assert on the *routing* without an API key.
    """
    ctx = NodeContext(llm=llm or get_llm(), tools=tools, settings=settings)

    builder = StateGraph(AnalystState)
    for name, factory in (
        ("intake", make_intake),
        ("clarify_gate", make_clarify_gate),
        ("resolve_metrics", make_resolve_metrics),
        ("plan", make_plan),
        ("author_sql", make_author_sql),
        ("execute", make_execute),
        ("interpret", make_interpret),
        ("synthesize", make_synthesize),
    ):
        _add_node(builder, name, factory(ctx))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "clarify_gate")
    builder.add_conditional_edges("clarify_gate", _after_clarify)
    builder.add_edge("resolve_metrics", "plan")
    builder.add_edge("plan", "author_sql")
    builder.add_conditional_edges("author_sql", _after_author)
    builder.add_conditional_edges("execute", _after_execute)
    builder.add_edge("interpret", "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile(checkpointer=checkpointer if checkpointer is not None else get_checkpointer())


def start_run(
    question: str,
    requested_by: str | None = None,
    graph: Any | None = None,
) -> dict[str, Any]:
    """Create a run and drive it to its first stopping point.

    Returns the final state. "Final" here means completed, failed, truncated, or *parked* -
    waiting on a clarification or an approval - and a parked run is resumed later by
    ``resume_run`` on the same ``thread_id``.
    """
    run_id = repo.create_run(question, requested_by=requested_by)
    thread_id = f"run-{run_id}"
    graph = graph if graph is not None else build_graph()
    state = initial_state(str(run_id), thread_id, question, requested_by)

    with bound(run_id=str(run_id), thread_id=thread_id):
        return _drive(graph, state, thread_id, run_id)


def resume_run(thread_id: str, updates: dict[str, Any] | None = None, graph: Any | None = None) -> dict[str, Any]:
    """Continue a parked run from its checkpoint.

    This is the same call whether the process restarted in between or not - the checkpoint is the
    only thing that carries the run forward, so recovery and ordinary resumption share one path
    rather than being two mechanisms that can disagree.
    """
    graph = graph if graph is not None else build_graph()
    config = thread_config(thread_id)
    snapshot = graph.get_state(config)
    if not snapshot.values:
        raise KeyError(f"no checkpoint for thread {thread_id}")

    run_id = uuid.UUID(snapshot.values["run_id"])
    if updates:
        graph.update_state(config, updates)

    with bound(run_id=str(run_id), thread_id=thread_id):
        return _drive(graph, None, thread_id, run_id)


def _drive(graph: Any, state: AnalystState | None, thread_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    """Invoke the graph, turning the failures it cannot handle into a recorded outcome."""
    config = thread_config(thread_id)
    try:
        return dict(graph.invoke(state, config))
    except LLMRefusalError as exc:
        # A refusal that the server-side fallback could not reroute. Recorded as a failure with
        # its category, rather than surfacing to the user as an unexplained error.
        log.warning("run refused by the model", category=exc.category)
        repo.finish_run(
            run_id,
            "failed",
            error={"type": "LLMRefusal", "message": str(exc), "category": exc.category},
        )
        raise
    except Exception as exc:
        log.exception("run failed", error_type=type(exc).__name__)
        repo.finish_run(
            run_id, "failed", error={"type": type(exc).__name__, "message": str(exc)}
        )
        raise
