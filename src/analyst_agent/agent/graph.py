"""The graph.

    intake -> clarify_gate -> resolve_metrics -> gather_context -> plan
           -> author_sql -> execute -> interpret -> analyse
           -> materiality_check -> generate_hypotheses -> [test each] -> reconcile
           -> synthesize -> visualize

Two things about this shape are deliberate.

**There is no `interpret -> synthesize` edge.** Every path to an answer passes the materiality
gate, which is what makes "a material finding needs two tested explanations" structural rather
than a request in the prompt.

**SQL authoring does not use tool calling; three other nodes do.** `author_sql` goes through
structured output so exactly one statement per turn reaches the guard and the audit. But
`gather_context`, `analyse` and `visualize` each run a bounded tool loop, because whether looking
at the schema, deriving a period-over-period view or drawing a chart *helps* depends on what the
data turned out to look like - and that cannot be scheduled from outside the run.

The conditional edges are where the policy lives:

* `clarify_gate` routes to END when the question cannot be answered as asked. Stopping to ask is
  a correct outcome, and the run stays resumable until the answer arrives.
* `execute` routes to END when the guard escalated, so the run parks rather than working around
  the block - and back to `execute` when a human has approved, so the *same* statement runs.
* `author_sql`, `materiality_check` and the test loop all route to `synthesize` when the budget
  is spent, which turns exhaustion into a partial answer instead of an exception.
* `materiality_check` is the gate: while a material finding lacks two tested explanations, the
  only edge out goes to hypothesis generation.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph

from analyst_agent.agent.budget import Budget
from analyst_agent.agent.checkpointer import get_checkpointer, thread_config
from analyst_agent.agent.llm import LLM, LLMRefusalError, get_llm
from analyst_agent.agent.nodes import (
    NodeContext,
    make_analyse,
    make_author_sql,
    make_clarify_gate,
    make_compute_metrics,
    make_execute,
    make_gather_context,
    make_intake,
    make_interpret,
    make_plan,
    make_resolve_metrics,
    make_synthesize,
    make_visualize,
)
from analyst_agent.agent.nodes.investigate import (
    MIN_HYPOTHESES,
    make_generate_hypotheses,
    make_materiality_check,
    make_reconcile,
    make_test_hypothesis,
)
from analyst_agent.agent.nodes.linear import Node
from analyst_agent.agent.state import (
    AnalystState,
    initial_state,
    material_findings,
    tested_hypotheses,
)
from analyst_agent.config import Settings
from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import bound, get_logger
from analyst_agent.tools.registry import ToolRegistry

log = get_logger(__name__)


def _after_clarify(state: AnalystState) -> str:
    """Stopping to ask is a correct outcome, not a failure of the run."""
    return END if state.get("status") == "clarifying" else "resolve_metrics"


def _after_compute(state: AnalystState) -> str:
    """Straight to interpretation when an approved metric already answered the question.

    Falling through to `author_sql` regardless would mean re-asking in SQL for a figure the
    registry just produced - a second query for the same number, and one the model wrote itself.
    """
    if state.get("truncation_reason"):
        return "synthesize"
    return "interpret" if state.get("_last_result") else "author_sql"


def _after_author(state: AnalystState) -> str:
    """A spent budget asks for more, or routes to a partial answer.

    Parking here is approval point 3: the run stops and waits rather than truncating silently,
    because whether an unfinished investigation deserves more budget is a judgement a person is
    better placed to make than the agent.
    """
    if state.get("status") == "awaiting_approval":
        return END
    if state.get("truncation_reason"):
        return "synthesize"
    return "execute" if state.get("_draft") else "synthesize"


def _after_execute(state: AnalystState) -> str:
    """An escalated query parks the run; there is no route around the approval."""
    if state.get("status") == "awaiting_approval":
        return END

    # Resuming after a human said yes: the *same* statement runs again, now carrying the
    # approval id. Re-authoring it would mean running something the reviewer never saw.
    draft = state.get("_draft")
    if draft and draft.get("approval_id"):
        return "execute"

    if state.get("_last_result"):
        return "interpret"
    # A rejected statement or a tool error: answer with what is established rather than
    # retrying blindly.
    return "synthesize"


def _after_materiality(state: AnalystState) -> str:
    """**The gate the whole project is built around.**

    While a material finding has fewer than two hypotheses in a terminal state, the route to
    synthesis is *unavailable* - the only edge out of here goes to hypothesis generation. This
    is why the requirement is enforced in the graph rather than asked for in the prompt: there
    is nothing here for a model to talk its way past.

    `materiality_check` sets `_investigating_finding_id` only when such a finding exists and the
    budget still allows the work, so this edge reads a fact rather than re-deciding it.
    """
    if state.get("truncation_reason"):
        return "synthesize"
    return "generate_hypotheses" if state.get("_investigating_finding_id") else "synthesize"


def _after_hypotheses(state: AnalystState) -> str:
    """With nothing to test, reconciling would be theatre - go and answer."""
    if state.get("truncation_reason"):
        return "synthesize"
    return "test_hypothesis" if state.get("_hypothesis_queue") else "reconcile"


def _after_test(state: AnalystState) -> str:
    """Keep testing while the queue holds, then reconcile what came back."""
    if state.get("truncation_reason"):
        return "synthesize"
    return "test_hypothesis" if state.get("_hypothesis_queue") else "reconcile"


def _after_reconcile(state: AnalystState) -> str:
    """Back to the gate, which decides whether another finding still needs explaining."""
    return "synthesize" if state.get("truncation_reason") else "materiality_check"


def synthesis_is_blocked(state: AnalystState) -> tuple[bool, str | None]:
    """Whether the gate would currently refuse to let this run conclude.

    Exposed as a function rather than left inside the edge so that a test can assert the rule
    directly - "a material finding cannot reach synthesis untested" is a claim worth checking on
    its own, not only through whichever path the graph happened to take.
    """
    for finding in material_findings(state):
        tested = tested_hypotheses(state, finding["finding_id"])
        if len(tested) < MIN_HYPOTHESES:
            already_flagged = any(
                u["finding_id"] == finding["finding_id"] for u in state.get("_under_tested", [])
            )
            if not already_flagged:
                return True, (
                    f"{finding['statement'][:80]!r} is material and has {len(tested)} of "
                    f"{MIN_HYPOTHESES} tested explanations"
                )
    return False, None


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
        ("gather_context", make_gather_context),
        ("plan", make_plan),
        ("compute_metrics", make_compute_metrics),
        ("author_sql", make_author_sql),
        ("execute", make_execute),
        ("interpret", make_interpret),
        ("analyse", make_analyse),
        ("materiality_check", make_materiality_check),
        ("generate_hypotheses", make_generate_hypotheses),
        ("test_hypothesis", make_test_hypothesis),
        ("reconcile", make_reconcile),
        ("synthesize", make_synthesize),
        ("visualize", make_visualize),
    ):
        _add_node(builder, name, factory(ctx))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "clarify_gate")
    builder.add_conditional_edges("clarify_gate", _after_clarify)
    # The model gets to look at the schema and resolve terms *before* it plans. Without this
    # the system prompt told it to check the schema and gave it no way to.
    builder.add_edge("resolve_metrics", "gather_context")
    builder.add_edge("gather_context", "plan")
    # Approved metrics first: anything the registry already answers is computed by naming
    # it, so no free text reaches SQL for those figures. Whatever is left falls through to
    # author_sql.
    builder.add_edge("plan", "compute_metrics")
    builder.add_conditional_edges("compute_metrics", _after_compute)
    builder.add_conditional_edges("author_sql", _after_author)
    builder.add_conditional_edges("execute", _after_execute)

    # The investigation loop. Note there is no `interpret -> synthesize` edge any more: every
    # path to an answer now goes through the materiality gate.
    # Follow-up analysis on the frame that already exists, before deciding whether the
    # finding needs explaining - a period-over-period view often settles that question.
    builder.add_edge("interpret", "analyse")
    builder.add_edge("analyse", "materiality_check")
    builder.add_conditional_edges("materiality_check", _after_materiality)
    builder.add_conditional_edges("generate_hypotheses", _after_hypotheses)
    builder.add_conditional_edges("test_hypothesis", _after_test)
    builder.add_conditional_edges("reconcile", _after_reconcile)
    # The design document's flow ends synthesize -> visualize -> respond. The chart is built
    # last because what is worth charting depends on what the conclusion turned out to be.
    builder.add_edge("synthesize", "visualize")
    builder.add_edge("visualize", END)

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
        return drive(graph, state, thread_id, run_id)


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
        return drive(graph, None, thread_id, run_id)


def resume_after_decision(run_id: uuid.UUID, graph: Any | None = None) -> dict[str, Any]:
    """Carry on once a human has decided on the approval this run was waiting for.

    Both outcomes continue the run; neither is an error.

    * **Approved** - the *same* statement runs again, carrying the approval id. It is
      deliberately not re-authored: what was agreed to was that text.
    * **Rejected or timed out** - the draft is dropped and the run proceeds to answer with what
      it could establish, recording why it could not do more. Refusal is a first-class path.
    """
    run = repo.get_run(run_id)
    if run is None:
        raise KeyError(f"no run {run_id}")

    pending = repo.pending_approvals(run_id)
    if pending:
        raise ValueError(
            f"run {run_id} still has {len(pending)} undecided approval(s); nothing to resume"
        )

    decided = [
        a for a in repo.get_trace(run_id)["approvals"] if a["status"] != "pending"
    ]
    if not decided:
        raise ValueError(f"run {run_id} has no decided approval to act on")

    latest = decided[-1]

    if latest["kind"] == "budget_extension":
        return _resume_after_budget_decision(run_id, run["thread_id"], latest, graph)
    graph = graph if graph is not None else build_graph()
    snapshot = graph.get_state(thread_config(run["thread_id"]))
    draft = (snapshot.values or {}).get("_draft") or {}

    if latest["status"] == "approved":
        updates: dict[str, Any] = {
            "status": "investigating",
            "_draft": {**draft, "approval_id": str(latest["approval_id"])},
            "_pending_approval": None,
        }
        log.info("resuming with approval", run_id=str(run_id), approval_id=str(latest["approval_id"]))
    else:
        updates = {
            "status": "investigating",
            "_draft": None,
            "_pending_approval": None,
            "errors": [
                {
                    "node": "execute",
                    "kind": "ApprovalRefused",
                    "message": (
                        f"the query was {latest['status']}"
                        + (f": {latest['decision_reason']}" if latest.get("decision_reason") else "")
                    ),
                    "recoverable": False,
                    "attempt": 1,
                }
            ],
        }
        log.info("resuming without approval", run_id=str(run_id), decision=latest["status"])

    return resume_run(run["thread_id"], updates=updates, graph=graph)


def _resume_after_budget_decision(
    run_id: uuid.UUID, thread_id: str, approval: dict[str, Any], graph: Any
) -> dict[str, Any]:
    """Carry on after a decision on approval point 3.

    Granted: the ceilings are raised and the investigation continues from where it stopped.
    Refused: the run answers with what it established and says it was cut short. Neither is an
    error, and the refusal is the more common outcome by design - the point of asking is that the
    answer can be no.
    """
    snapshot = graph.get_state(thread_config(thread_id))
    budget = Budget.restore((snapshot.values or {}).get("budget") or {})

    if approval["status"] == "approved":
        budget.grant_extension()
        log.info("budget extended", run_id=str(run_id), extensions=budget.extensions_granted)
        updates: dict[str, Any] = {
            "status": "investigating",
            "budget": budget.to_state(),
            "_pending_approval": None,
        }
    else:
        reason = (
            f"budget extension {approval['status']}"
            + (f": {approval['decision_reason']}" if approval.get("decision_reason") else "")
        )
        log.info("budget extension refused", run_id=str(run_id), decision=approval["status"])
        updates = {
            "status": "investigating",
            "truncation_reason": reason,
            "_pending_approval": None,
        }

    return resume_run(thread_id, updates=updates, graph=graph)


def drive(
    graph: Any, state: AnalystState | None, thread_id: str, run_id: uuid.UUID
) -> dict[str, Any]:
    """Invoke the graph, turning the failures it cannot handle into a recorded outcome.

    Public because the API drives a run whose row it already created, rather than going through
    ``start_run`` and getting a second one.
    """
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
