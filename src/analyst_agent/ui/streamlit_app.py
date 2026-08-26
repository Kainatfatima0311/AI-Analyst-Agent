"""The analyst interface.

    streamlit run src/analyst_agent/ui/streamlit_app.py

Talks only to the API. The layout is built around one idea: a conclusion is worth as much as the
evidence you can reach from it, so the answer, what was ruled out, and the SQL behind every cited
number are all on the same screen — and the queries the guard *refused* are there too, because
what the agent tried is usually what a reviewer wants to know.
"""

from __future__ import annotations

import time
from typing import Any

import streamlit as st

from analyst_agent.ui import components as ui
from analyst_agent.ui.api_client import AnalystApi, ApiError
from analyst_agent.ui.theme import status_chip, stylesheet

POLL_SECONDS = 2.0
LIVE_STATUSES = {"received", "investigating"}

EXAMPLES = [
    "What was monthly revenue in 2018?",
    "Why did revenue drop in March 2018?",
    "Which product categories drove the most revenue last quarter?",
    "How is on-time delivery trending by seller state?",
]


st.set_page_config(
    page_title="Analyst",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(stylesheet(), unsafe_allow_html=True)


@st.cache_resource
def api() -> AnalystApi:
    return AnalystApi()


@st.cache_data(ttl=300)
def metric_catalogue() -> list[dict[str, Any]]:
    try:
        return api().metrics()
    except Exception:
        return []


def sidebar() -> str:
    with st.sidebar:
        st.markdown("### Analyst")

        if api().healthy():
            st.markdown(status_chip("completed").replace("Completed", "API reachable"),
                        unsafe_allow_html=True)
        else:
            st.markdown(status_chip("failed").replace("Failed", "API unreachable"),
                        unsafe_allow_html=True)
            st.caption("Start it with `make api`, or `docker compose up`.")

        who = st.text_input("You are", value="analyst@example.com", help="Recorded with any decision you make.")

        st.markdown("---")
        st.markdown('<div class="card-title">Approved metrics</div>', unsafe_allow_html=True)
        catalogue = metric_catalogue()
        if catalogue:
            names = [m["name"] for m in catalogue]
            chosen = st.selectbox("Look one up", ["—", *names], label_visibility="collapsed")
            if chosen != "—":
                metric = next(m for m in catalogue if m["name"] == chosen)
                st.caption(f"**{metric['title']}** · {metric['unit']} · per {metric['grain']}")
                st.caption(f"`{metric['definition_version']}`")
                if metric.get("dimensions"):
                    st.caption("By: " + ", ".join(metric["dimensions"]))
                for caveat in metric.get("caveats", []):
                    st.caption(f"— {caveat}")
        else:
            st.caption("Unavailable while the API is down.")

        st.markdown("---")
        st.markdown('<div class="card-title">Recent</div>', unsafe_allow_html=True)
        try:
            for run in api().runs(limit=8):
                label = run["question"]
                label = label if len(label) <= 46 else label[:45] + "…"
                if st.button(label, key=f"run-{run['run_id']}", use_container_width=True):
                    st.session_state["run_id"] = run["run_id"]
                    st.rerun()
        except Exception:
            st.caption("Unavailable.")

    return who


def ask_panel() -> None:
    st.markdown("## Ask")
    question = st.text_area(
        "Business question",
        placeholder="Why did revenue drop in March 2018?",
        height=88,
        label_visibility="collapsed",
        key="question",
    )
    left, right = st.columns([1, 4])
    with left:
        submitted = st.button("Investigate", type="primary", use_container_width=True)
    with right:
        st.caption(
            "The agent resolves your terms to approved definitions, writes SQL that is checked "
            "before it runs, and tests more than one explanation before concluding."
        )

    st.markdown('<div class="card-title">Try</div>', unsafe_allow_html=True)
    columns = st.columns(len(EXAMPLES))
    for column, example in zip(columns, EXAMPLES, strict=True):
        with column, st.container():
            if st.button(example, key=f"eg-{example}", use_container_width=True):
                st.session_state["question"] = example
                st.rerun()

    if submitted and question.strip():
        try:
            started = api().ask(question.strip())
            st.session_state["run_id"] = started["run_id"]
            st.rerun()
        except ApiError as exc:
            st.error(f"The API refused this: {exc.detail}")


def run_panel(run_id: str, who: str) -> None:
    try:
        run = api().run(run_id)
        trace = api().trace(run_id)
    except ApiError as exc:
        st.error(f"Could not load that run: {exc.detail}")
        return

    status = run["status"]
    st.markdown(f"## {run['question']}")
    st.markdown(f'<div class="chip-row">{status_chip(status)}</div>', unsafe_allow_html=True)

    ui.stat_row(
        [
            ("Queries run", trace["summary"].get("queries_executed", 0)),
            ("Blocked", trace["summary"].get("queries_rejected", 0)),
            ("Escalated", trace["summary"].get("queries_escalated", 0)),
            ("Refuted", trace["summary"].get("hypotheses_refuted", 0)),
            ("Tokens", f"{run.get('tokens_in', 0) + run.get('tokens_out', 0):,}"),
        ]
    )

    if run.get("pending_approvals"):
        st.markdown("## Waiting on you")
        if ui.approval_banner(run_id, run["pending_approvals"], api(), who):
            time.sleep(0.6)
            st.rerun()

    if status == "clarifying":
        st.markdown("## The agent has a question")
        st.info("It stopped rather than guessing what you meant.")
        reply = st.text_input("Your answer", key=f"clarify-{run_id}")
        if st.button("Send", type="primary") and reply.strip():
            api().answer(run_id, reply.strip())
            time.sleep(0.6)
            st.rerun()

    answer, evidence = st.tabs(["Answer", "How it got there"]) if run.get("answer") else (
        st.container(),
        st.container(),
    )

    with answer:
        if run.get("answer"):
            ui.conclusion(run["answer"])
            ui.evidence_drawer(run["answer"])
            ui.findings(run.get("findings", []))
            ui.charts(run.get("charts", []))
        elif status in LIVE_STATUSES:
            st.caption("Working…")
            ui.timeline(trace.get("steps", []))
        elif run.get("error"):
            st.error(f"{run['error'].get('type')}: {run['error'].get('message')}")
        else:
            ui.findings(run.get("findings", []))

    with evidence:
        if run.get("answer"):
            st.markdown("## What it did")
            ui.timeline(trace.get("steps", []))
            ui.query_audit(trace.get("queries", []))
            if trace.get("approvals"):
                st.markdown("## Decisions")
                for approval in trace["approvals"]:
                    st.markdown(
                        f'<div class="chip-row">{status_chip(approval["status"])}</div>'
                        f"<div><strong>{approval['kind'].replace('_', ' ')}</strong> — "
                        f"{approval['reason']}</div>"
                        + (
                            f"<div class='tl-meta'>decided by {approval['decided_by']}"
                            + (
                                f" — {approval['decision_reason']}"
                                if approval.get("decision_reason")
                                else ""
                            )
                            + "</div>"
                            if approval.get("decided_by")
                            else ""
                        ),
                        unsafe_allow_html=True,
                    )

    if status in LIVE_STATUSES:
        time.sleep(POLL_SECONDS)
        st.rerun()


def main() -> None:
    ui.masthead()
    who = sidebar()

    run_id = st.session_state.get("run_id")
    if run_id:
        if st.button("← New question"):
            st.session_state.pop("run_id", None)
            st.rerun()
        run_panel(run_id, who)
    else:
        ask_panel()


main()
