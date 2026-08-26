"""Rendering pieces for the analyst interface.

Each function renders one thing and returns nothing (or a decision), so the app file reads as a
layout rather than as a wall of markup. The one rule they all share: **no claim appears without a
way to get to the query behind it.**

Nothing here invents data to fill a panel. If a run produced no chart, the chart panel says so
rather than drawing something plausible — a dashboard that always looks complete is a dashboard
you cannot trust when it is.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from analyst_agent.ui.theme import (
    Mode,
    chart_layout,
    chip,
    confidence_chip,
    status_chip,
    status_of,
)

NAV: list[tuple[str, str, str]] = [
    ("ask", "⌂", "Ask Question"),
    ("dashboard", "⊞", "Dashboard"),
    ("saved", "❏", "Saved Analyses"),
    ("metrics", "≣", "Metrics Catalog"),
    ("schema", "⌕", "Data Explorer"),
    ("reports", "▤", "Reports"),
    ("settings", "⚙", "Settings"),
]

# The four tints behind the suggestion glyphs. Chrome pastels at 12% alpha — light enough that
# they read as decoration rather than as the series identity a chart assigns.
TILE_TINTS: tuple[tuple[str, str], ...] = (
    ("#1f7a4d", "rgba(31, 122, 77, .12)"),
    ("#6c5ce7", "rgba(108, 92, 231, .12)"),
    ("#c07000", "rgba(192, 112, 0, .12)"),
    ("#2a78d6", "rgba(42, 120, 214, .12)"),
)


# --- sidebar ------------------------------------------------------------------


def brand() -> None:
    st.markdown(
        '<div class="brand">'
        '<div class="mark">◆</div>'
        '<div><div class="name">Analyst</div>'
        '<div class="kind">AI Data Analyst</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )


def nav(current: str) -> str:
    """The nav list. Returns the page to show.

    The current row is a styled div and the rest are buttons wearing the same shape, so the only
    visual difference between them is *state* rather than kind — and the current row is not a
    control, because clicking where you already are should not be offered.
    """
    chosen = current
    for key, icon, label in NAV:
        if key == current:
            st.markdown(
                f'<div class="nav-current"><span class="ico">{icon}</span>{label}</div>',
                unsafe_allow_html=True,
            )
        elif st.button(f"{icon}   {label}", key=f"nav-{key}", width="stretch"):
            chosen = key
    return chosen


def side_status(reachable: bool) -> None:
    tone = "#1f7a4d" if reachable else "#b23c3c"
    label = "API Reachable" if reachable else "API Unreachable"
    sub = "All systems operational" if reachable else "Start it with `make api`"
    st.markdown(
        f'<div class="side-status"><div class="row">'
        f'<span class="dot" style="background:{tone}"></span>'
        f'<span style="color:{tone}">{label}</span></div>'
        f'<div class="sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )


def side_user(who: str) -> None:
    initial = (who or "?").strip()[:1].upper()
    st.markdown(
        f'<div class="side-user"><div class="avatar">{initial}</div>'
        f'<div><div class="who">{who}</div><div class="role">Analyst</div></div></div>',
        unsafe_allow_html=True,
    )


# --- page furniture -----------------------------------------------------------


def page_header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="greeting">{title}</div>'
        f'<div class="greeting-sub">{subtitle}</div>',
        unsafe_allow_html=True,
    )


def section(heading: str) -> None:
    st.markdown(
        f'<div class="section-head"><div class="h">{heading}</div></div>',
        unsafe_allow_html=True,
    )


def stat_row(stats: list[tuple[str, Any]]) -> None:
    cells = "".join(
        f'<div class="stat"><div class="value">{value}</div>'
        f'<div class="label">{label}</div></div>'
        for label, value in stats
    )
    st.markdown(
        f'<div class="card flush"><div class="stat-row">{cells}</div></div>',
        unsafe_allow_html=True,
    )


def ask_head() -> None:
    st.markdown(
        '<div class="ask-head"><span class="spark">✦</span>'
        '<span class="t">Ask anything about your business</span></div>',
        unsafe_allow_html=True,
    )


def char_counter(used: int, limit: int) -> None:
    st.markdown(
        f'<div class="counter">{used} / {limit}</div>',
        unsafe_allow_html=True,
    )


def tile(index: int, glyph: str) -> str:
    """One coloured glyph tile, cycling the four chrome tints."""
    ink, wash = TILE_TINTS[index % len(TILE_TINTS)]
    return f'<div class="ico" style="background:{wash};color:{ink}">{glyph}</div>'


def suggestion(index: int, glyph: str, question: str) -> None:
    st.markdown(
        f'<div class="suggest">{tile(index, glyph)}<div class="q">{question}</div></div>',
        unsafe_allow_html=True,
    )


def recent_card(run: dict[str, Any], current: bool) -> None:
    question = run["question"]
    question = question if len(question) <= 62 else question[:61] + "…"
    st.markdown(
        f'<div class="recent{" current" if current else ""}">'
        f'<div class="q">{question}</div>'
        f'<div class="meta"><span class="when">{ago(run.get("created_at"))}</span>'
        f'{status_chip(run.get("status"))}</div></div>',
        unsafe_allow_html=True,
    )


def sidebar_art() -> None:
    """The decorative panel at the foot of the nav.

    Inline SVG rather than an image file: it inherits the accent, costs no request, and cannot
    fail to load. Marked ``aria-hidden`` because it says nothing a screen reader needs.
    """
    st.markdown(
        '<div class="side-art" aria-hidden="true">'
        '<svg viewBox="0 0 200 140" width="100%" height="120">'
        '<defs><linearGradient id="bar" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#8f7cff" stop-opacity=".95"/>'
        '<stop offset="1" stop-color="#5a49dd" stop-opacity=".55"/>'
        "</linearGradient></defs>"
        '<path d="M22 96 L60 62 L96 78 L134 34 L172 20" fill="none" stroke="#a99bff"'
        ' stroke-width="2.5" stroke-linecap="round" opacity=".75"/>'
        '<circle cx="60" cy="62" r="4" fill="#c9c0ff"/>'
        '<circle cx="134" cy="34" r="4" fill="#c9c0ff"/>'
        '<rect x="34" y="92" width="26" height="34" rx="5" fill="url(#bar)"/>'
        '<rect x="72" y="76" width="26" height="50" rx="5" fill="url(#bar)"/>'
        '<rect x="110" y="58" width="26" height="68" rx="5" fill="url(#bar)"/>'
        "</svg></div>",
        unsafe_allow_html=True,
    )


def ago(timestamp: str | None) -> str:
    """How long ago, in the units a person would use.

    Falls back to the raw value rather than to "unknown": a timestamp this cannot parse is still
    more informative than nothing.
    """
    if not timestamp:
        return ""
    try:
        when = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp[:16]
    seconds = (dt.datetime.now(dt.UTC) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


# --- the result ---------------------------------------------------------------


def result_head(run: dict[str, Any]) -> None:
    st.markdown(
        f'<div class="result-head"><div>'
        f'<div class="q">{run["question"]}</div>'
        f'<div class="chip-row">{status_chip(run["status"])}'
        f'<span class="chip" style="color:var(--text-muted);border-color:var(--border-strong)">'
        f'{ago(run.get("created_at"))}</span></div>'
        f"</div></div>",
        unsafe_allow_html=True,
    )


def takeaways(run: dict[str, Any]) -> None:
    """The findings, as the panel a reader looks at first.

    Only what the agent actually recorded. A material finding is marked as needing an
    explanation, because that is the state the investigation loop cares about.
    """
    st.markdown('<div class="panel-title">Key Takeaways</div>', unsafe_allow_html=True)
    findings_list = run.get("findings") or []
    if not findings_list:
        st.markdown(
            '<div class="takeaway"><div class="ico">·</div>'
            '<span class="t">No separate findings were recorded for this question.</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        return

    for index, finding in enumerate(findings_list[:5]):
        material = finding.get("material")
        tested = [
            h for h in finding.get("hypotheses", [])
            if h.get("status") in ("supported", "refuted")
        ]
        # A glyph and a border tone, never colour alone: a material finding has to be legible in
        # a screenshot and to a reader who cannot separate the hues.
        klass, glyph = ("material", "!") if material else ("good", "✓")
        note = ""
        if material:
            note = (
                f' <span style="color:var(--text-muted)">· {len(tested)} '
                f'explanation{"s" if len(tested) != 1 else ""} tested</span>'
            )
        st.markdown(
            f'<div class="takeaway {klass}">{tile(index, glyph)}'
            f'<span class="t">{finding["statement"]}{note}</span></div>',
            unsafe_allow_html=True,
        )


def chart_panel(
    charts_data: list[dict[str, Any]], index: int, title: str, mode: Mode = "light"
) -> None:
    """One of the agent's figures, or an honest blank.

    The series colours in the spec are left exactly as ``chart_builder`` chose them — only the
    surface and the ink are re-themed. Repainting series by UI mode would break the rule that
    colour follows the entity rather than the context it is viewed in.
    """
    with st.container(border=True):
        chart = charts_data[index] if index < len(charts_data) else None
        heading = chart["title"] if chart and chart.get("title") else title
        st.markdown(
            f'<div class="panel-title">{heading}'
            f'<span class="hint" title="Built by the agent from the query below">ⓘ</span></div>',
            unsafe_allow_html=True,
        )
        if chart is None:
            st.markdown(
                '<div class="blank">No chart here — the agent judged a figure would not add to '
                "the numbers. The values are in the evidence below.</div>",
                unsafe_allow_html=True,
            )
            return

        figure = go.Figure(chart["spec"])
        is_pie = any(getattr(trace, "type", "") == "pie" for trace in figure.data)
        figure.update_layout(
            **_panel_layout(mode, is_pie),
            height=300,
            showlegend=is_pie or len(figure.data) > 1,
        )
        st.plotly_chart(figure, width="stretch", key=f"chart-{chart.get('chart_id', index)}")
        st.markdown(
            f'<div class="evidence"><div class="meta">from query {chart["query_id"]}</div></div>',
            unsafe_allow_html=True,
        )


def _panel_layout(mode: Mode, is_pie: bool) -> dict[str, Any]:
    """Panel-sized layout. A pie has no axes, and axis overrides on one are ignored noise."""
    layout = dict(chart_layout(mode))
    layout["title"] = {"text": ""}  # the panel heading already says what this is
    layout["margin"] = (
        {"l": 8, "r": 8, "t": 8, "b": 8}
        if is_pie
        else {"l": 52, "r": 12, "t": 12, "b": 34}
    )
    if is_pie:
        layout.pop("xaxis", None)
        layout.pop("yaxis", None)
        layout["legend"] = {**layout.get("legend", {}), "orientation": "v", "x": 1.0, "y": 0.5}
    return layout


def result_actions(run_id: str) -> None:
    """Share, Save, View Details — the three the design puts on a finished answer.

    Each does what its label says and nothing more. Share reveals the link to this run rather
    than posting it anywhere; Save is a bookmark in this session. Neither publishes, because
    publishing an answer is approval point 4 and that gate has no implementation behind it — a
    button that quietly did it would be the worst kind of shortcut.
    """
    share, save, details = st.columns([1, 1, 1.2])
    with share:
        if st.button("⤴ Share", key=f"share-{run_id}", width="stretch"):
            st.session_state[f"share-open-{run_id}"] = not st.session_state.get(
                f"share-open-{run_id}", False
            )
    with save:
        saved: set[str] = st.session_state.setdefault("bookmarks", set())
        if st.button(
            "★ Saved" if run_id in saved else "☆ Save", key=f"save-{run_id}", width="stretch"
        ):
            saved.symmetric_difference_update({run_id})
            st.rerun()
    with details:
        if st.button(
            "▤ View Details", type="primary", key=f"details-{run_id}", width="stretch"
        ):
            st.session_state["details_open"] = not st.session_state.get("details_open", False)
            st.rerun()


def evidence_footer(run: dict[str, Any], trace: dict[str, Any]) -> bool:
    """The line back to the SQL. Returns True when the reader asked to see it.

    Blocked queries are counted here beside the executed ones. A footer that said "3 queries
    executed" while the guard had refused two would be telling half the story, and the refused
    half is usually the interesting one.
    """
    executed = trace.get("summary", {}).get("queries_executed", 0)
    blocked = trace.get("summary", {}).get("queries_rejected", 0)
    label = f"{executed} quer{'y' if executed == 1 else 'ies'} executed"
    if blocked:
        label += f" · {blocked} blocked by the guard"

    left, right = st.columns([3.4, 1])
    with left:
        st.markdown(
            f'<div class="evidence-foot"><span class="l">Evidence &amp; Queries</span>'
            f'<span class="n">{label}</span></div>',
            unsafe_allow_html=True,
        )
    with right:
        run_id = run.get("run_id", "")
        return bool(
            st.button("≣ View SQL & Data", key=f"sql-{run_id}", width="stretch")
        )


# --- the answer, in full ------------------------------------------------------


def conclusion(answer: dict[str, Any]) -> None:
    """The answer, with its confidence and what it ruled out.

    Refuted explanations sit beside the conclusion rather than below the fold: naming what was
    disproved is how a reader knows the agent looked.
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

    plural = "y" if len(evidence) == 1 else "ies"
    with st.expander(f"View SQL & Data · {len(evidence)} quer{plural}"):
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


def charts(items: list[dict[str, Any]], mode: Mode = "light") -> None:
    if not items:
        return
    st.markdown("## Charts")
    for index, chart in enumerate(items):
        figure = go.Figure(chart["spec"])
        figure.update_layout(**chart_layout(mode))
        st.plotly_chart(
            figure, width="stretch", key=f"full-chart-{chart.get('chart_id', index)}"
        )
        st.caption(f"From query {chart['query_id']}")


def timeline(steps: list[dict[str, Any]]) -> None:
    """What the agent did, in order, with the summary each node wrote."""
    if not steps:
        st.caption("Nothing has run yet.")
        return

    rows = []
    for step in steps:
        state = (
            "error"
            if step["status"] == "error"
            else ("active" if step["status"] in ("started", "paused") else "")
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
            f'<div class="kind">{approval["kind"].replace("_", " ").title()}</div>'
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
