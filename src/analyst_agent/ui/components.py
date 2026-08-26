"""Rendering pieces for the analyst interface.

Each function renders one thing and returns nothing, so the app file reads as a layout rather
than as a wall of markup. The one rule they all share: **no claim appears without a way to get to
the query behind it.**
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

from analyst_agent.ui.theme import chip, confidence_chip, status_chip, status_of


def masthead() -> None:
    st.markdown(
        '<div class="masthead">'
        '<span class="title">Analyst</span>'
        '<span class="tagline">every number leads back to the query behind it</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def stat_row(stats: list[tuple[str, Any]]) -> None:
    cells = "".join(
        f'<div class="stat"><div class="value">{value}</div>'
        f'<div class="label">{label}</div></div>'
        for label, value in stats
    )
    st.markdown(f'<div class="card flush"><div class="stat-row">{cells}</div></div>',
                unsafe_allow_html=True)


def conclusion(answer: dict[str, Any]) -> None:
    """The answer, with its confidence and what it ruled out.

    Refuted explanations are shown next to the conclusion rather than tucked below the fold:
    naming what was disproved is how a reader knows the agent looked.
    """
    st.markdown(
        f'<div class="conclusion"><div class="text">{answer["conclusion"]}</div></div>',
        unsafe_allow_html=True,
    )

    chips = [confidence_chip(answer.get("confidence"))]
    evidence_count = len(answer.get("evidence", []))
    chips.append(chip(f"{evidence_count} queries cited", "⛓", "#6b6a66"))
    st.markdown(f'<div class="chip-row">{"".join(chips)}</div>', unsafe_allow_html=True)

    if answer.get("refuted"):
        st.markdown('<div class="card-title">Ruled out</div>', unsafe_allow_html=True)
        for item in answer["refuted"]:
            st.markdown(f"- {item}")

    if answer.get("caveats"):
        st.markdown('<div class="card-title">Caveats</div>', unsafe_allow_html=True)
        for item in answer["caveats"]:
            st.markdown(f"- {item}")


def evidence_drawer(answer: dict[str, Any]) -> None:
    """The SQL behind each cited number, one click away."""
    evidence = answer.get("evidence", [])
    if not evidence:
        st.caption("No queries were cited — nothing ran that this answer rests on.")
        return

    with st.expander(f"Show the evidence · {len(evidence)} quer" + ("y" if len(evidence) == 1 else "ies")):
        for item in evidence:
            rows = item.get("row_count")
            st.markdown(
                f'<div class="evidence"><div class="purpose">{item["purpose"]}</div>'
                f'<div class="meta">{item["query_id"]}'
                + (f" · {rows} rows" if rows is not None else "")
                + "</div></div>",
                unsafe_allow_html=True,
            )
            st.code(item["sql"], language="sql")


def findings(items: list[dict[str, Any]]) -> None:
    """Findings, each with the explanations that were tested against it."""
    if not items:
        return

    st.markdown("## Findings")
    for finding in items:
        material = " material" if finding.get("material") else ""
        st.markdown(
            f'<div class="finding{material}">'
            f'<div class="statement">{finding["statement"]}</div>',
            unsafe_allow_html=True,
        )
        if finding.get("material"):
            st.markdown(
                f'<div class="chip-row">{chip("Needs explaining", "!", "#9a6b00")}</div>',
                unsafe_allow_html=True,
            )
        for hypothesis in finding.get("hypotheses", []):
            st.markdown(
                '<div class="hypothesis">'
                f'<div class="claim">{hypothesis["statement"]}</div>'
                f'<div class="chip-row">{status_chip(hypothesis.get("status"))}</div>'
                + (
                    f'<div class="why">{hypothesis["reasoning"]}</div>'
                    if hypothesis.get("reasoning")
                    else ""
                )
                + "</div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)


def charts(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    st.markdown("## Charts")
    for chart in items:
        figure = go.Figure(chart["spec"])
        st.plotly_chart(figure, use_container_width=True)
        st.caption(f"From query {chart['query_id']}")


def timeline(steps: list[dict[str, Any]]) -> None:
    """What the agent did, in order, with the summary each node wrote."""
    if not steps:
        st.caption("Nothing has run yet.")
        return

    rows = []
    for step in steps:
        state = "error" if step["status"] == "error" else (
            "active" if step["status"] in ("started", "paused") else ""
        )
        duration = f" · {step['duration_ms']} ms" if step.get("duration_ms") else ""
        effort = f" · {step['effort']}" if step.get("effort") else ""
        summary = step.get("summary") or ""
        rows.append(
            f'<div class="tl-row {state}">'
            f'<div class="tl-node">{step["node"]}</div>'
            f'<div class="tl-meta">{summary}{effort}{duration}</div>'
            "</div>"
        )
    st.markdown(f'<div class="timeline">{"".join(rows)}</div>', unsafe_allow_html=True)


def query_audit(queries: list[dict[str, Any]]) -> None:
    """Every query considered, including the ones the guard refused.

    Blocked attempts are shown, not hidden: what the agent *tried* is usually what a reviewer
    wants to know, and a run where three statements were refused is more informative than one
    where they silently vanished.
    """
    if not queries:
        return

    st.markdown("## Queries considered")
    st.caption("Including the ones that never ran.")

    for query in queries:
        verdict = query["verdict"]
        with st.expander(
            f"{status_of(verdict).icon}  {query['purpose']}", expanded=verdict == "rejected"
        ):
            chips = [status_chip(verdict)]
            if query.get("executed"):
                chips.append(chip(f"{query.get('row_count', 0)} rows", "▤", "#6b6a66"))
            if query.get("truncated"):
                chips.append(chip("Truncated", "✂", "#9a6b00"))
            if query.get("sensitive_columns"):
                chips.append(
                    chip(f"{len(query['sensitive_columns'])} restricted", "🔒", "#b23c3c")
                )
            st.markdown(f'<div class="chip-row">{"".join(chips)}</div>', unsafe_allow_html=True)

            if query.get("reasons"):
                for reason in query["reasons"]:
                    st.markdown(f"- {reason}")

            st.code(query.get("rewritten_sql") or query["sql"], language="sql")


def approval_banner(run_id: str, approvals: list[dict[str, Any]], api: Any, who: str) -> bool:
    """The pending decision, with everything needed to make it. Returns True if one was made.

    Both buttons are equally prominent. Rejection is a real answer here, not the discouraged
    path — the run continues either way and reports what it could establish.
    """
    if not approvals:
        return False

    decided = False
    for approval in approvals:
        payload = approval.get("payload", {})
        st.markdown(
            f'<div class="approval">'
            f'<div class="kind">{status_of(approval["kind"]).label if approval["kind"] in ("expensive_query",) else approval["kind"].replace("_", " ").title()}'
            f"</div>"
            f'<div class="why">{approval["reason"]}</div>'
            "</div>",
            unsafe_allow_html=True,
        )

        if payload.get("sensitive_columns"):
            st.markdown(
                '<div class="chip-row">'
                + "".join(chip(c, "🔒", "#b23c3c") for c in payload["sensitive_columns"])
                + "</div>",
                unsafe_allow_html=True,
            )
        if payload.get("estimated_cost"):
            st.caption(f"Estimated plan cost: {payload['estimated_cost']:,.0f}")
        if payload.get("sql"):
            st.code(payload["sql"], language="sql")

        reason = st.text_input(
            "Reason (recorded with your decision)",
            key=f"reason-{approval['approval_id']}",
            placeholder="optional, but it is what a later reader will see",
        )
        left, right, _ = st.columns([1, 1, 3])
        with left:
            if st.button("Approve", key=f"ok-{approval['approval_id']}", type="primary"):
                api.decide(run_id, approval["approval_id"], True, who, reason or None)
                decided = True
        with right:
            if st.button("Reject", key=f"no-{approval['approval_id']}"):
                api.decide(run_id, approval["approval_id"], False, who, reason or None)
                decided = True

    return decided
