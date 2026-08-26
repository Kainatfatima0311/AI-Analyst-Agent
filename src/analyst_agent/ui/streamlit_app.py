"""The analyst interface.

    streamlit run src/analyst_agent/ui/streamlit_app.py

Talks only to the API. The layout is built around one idea: a conclusion is worth as much as the
evidence you can reach from it, so the answer, what was ruled out, and the SQL behind every cited
number are all on the same screen — and the queries the guard *refused* are there too, because
what the agent tried is usually what a reviewer wants to know.

Six pages, and every one of them is built from something the API actually returns. There is no
page here that would look complete on an empty database, because a dashboard that always looks
finished is one you cannot read when it isn't.
"""

from __future__ import annotations

import time
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from analyst_agent.tools.palette import DARK, LIGHT
from analyst_agent.ui import components as ui
from analyst_agent.ui.api_client import AnalystApi, ApiError
from analyst_agent.ui.theme import Mode, chart_layout, status_chip, status_of, stylesheet

POLL_SECONDS = 2.0
LIVE_STATUSES = {"received", "investigating"}
QUESTION_LIMIT = 500

# Glyphs only — the tint behind them is one chrome accent for all four, because a per-card hue
# would read as series identity to anyone who has just looked at a chart.
EXAMPLES: list[tuple[str, str]] = [
    ("▤", "What was monthly revenue in 2018?"),
    ("◔", "Why did revenue drop in March 2018?"),
    ("◈", "Which product categories drove the most revenue?"),
    ("⌁", "How is on-time delivery trending by seller state?"),
    # A second page behind the arrow. The last two are here on purpose: one has no approved
    # definition and one cannot be answered from this data, so the starting questions include
    # cases where the right move is to stop and say so.
    ("◑", "What is the average order value by customer state?"),
    ("◍", "Which sellers concentrate the most revenue?"),
    ("◇", "What is our customer churn rate?"),
    ("◎", "How did the marketing spend affect sales?"),
]

CONTEXTS = [
    "Business context",
    "Finance review",
    "Operations",
    "Category management",
    "Executive summary",
]


st.set_page_config(
    page_title="Analyst",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)


def mode() -> Mode:
    return "dark" if st.session_state.get("dark") else "light"


st.markdown(stylesheet(mode()), unsafe_allow_html=True)


@st.cache_resource
def api() -> AnalystApi:
    return AnalystApi()


@st.cache_data(ttl=300)
def metric_catalogue() -> list[dict[str, Any]]:
    try:
        return api().metrics()
    except Exception:
        return []


@st.cache_data(ttl=300)
def schema_catalogue() -> dict[str, Any]:
    try:
        return api().schema()
    except Exception:
        return {}


def recent_runs(limit: int = 12) -> list[dict[str, Any]]:
    try:
        return api().runs(limit=limit)
    except Exception:
        return []


def open_run(run_id: str) -> None:
    st.session_state["run_id"] = run_id
    st.session_state["page"] = "ask"
    st.rerun()


# --- sidebar ------------------------------------------------------------------


def sidebar() -> tuple[str, str]:
    with st.sidebar:
        ui.brand()
        page = ui.nav(st.session_state.get("page", "ask"))
        if page != st.session_state.get("page", "ask"):
            st.session_state["page"] = page
            st.rerun()

        ui.side_status(api().healthy())
        st.session_state.setdefault("who", "analyst@example.com")
        who = st.text_input(
            "Signed in as",
            help="Recorded with any approval decision you make.",
            key="who",
        )
        ui.side_user(who)

    return page, who


# --- header -------------------------------------------------------------------


def header(title: str, subtitle: str) -> None:
    left, right = st.columns([3, 1.15])
    with left:
        ui.page_header(title, subtitle)
    with right:
        theme_col, new_col = st.columns([1, 1.25])
        with theme_col:
            label = "☀ Light" if st.session_state.get("dark") else "☾ Dark"
            if st.button(label, width="stretch", key="theme-toggle"):
                st.session_state["dark"] = not st.session_state.get("dark")
                st.rerun()
        with new_col:
            if st.button("+  New Analysis", type="primary", width="stretch"):
                st.session_state.pop("run_id", None)
                st.session_state["pending_question"] = ""
                st.session_state["page"] = "ask"
                st.rerun()


# --- ask ----------------------------------------------------------------------


def ask_card() -> None:
    # Streamlit refuses a write to a widget's own state key once that widget has been created in
    # this run, so a suggestion click cannot set `question` directly — it stages the text and
    # this applies it *before* the text area exists.
    if "pending_question" in st.session_state:
        st.session_state["question"] = st.session_state.pop("pending_question")

    # A real bordered container, not a wrapper div: Streamlit closes an unclosed div in
    # st.markdown immediately, so markup cannot wrap widgets. The CSS styles the container.
    with st.container(border=True):
        ui.ask_head()
        question = st.text_area(
            "Business question",
            placeholder="Why did revenue drop in March 2018?",
            height=96,
            max_chars=QUESTION_LIMIT,
            label_visibility="collapsed",
            key="question",
        )
        context, spacer, counter, send = st.columns([1.5, 4.2, 0.9, 0.7])
        with context:
            st.selectbox(
                "Context", CONTEXTS, label_visibility="collapsed", key="context"
            )
        with spacer:
            st.write("")
        with counter:
            ui.char_counter(len(question or ""), QUESTION_LIMIT)
        with send:
            submitted = st.button(
                "➤", type="primary", width="stretch", help="Investigate"
            )

    if submitted:
        if not (question or "").strip():
            st.warning("Type a question first.")
        else:
            try:
                started = api().ask((question or "").strip(), requested_by=st.session_state["who"])
                open_run(started["run_id"])
            except ApiError as exc:
                st.error(f"The API refused this: {exc.detail}")


VISIBLE_SUGGESTIONS = 4


def suggestions() -> None:
    """Four at a time, with an arrow through the rest.

    A click stages the text rather than writing it: ``question`` is a widget's own state key, and
    Streamlit refuses a write to one after the widget exists in the same run.
    """
    ui.section("Try these popular questions")
    page = st.session_state.get("suggest_page", 0)
    pages = max(1, -(-len(EXAMPLES) // VISIBLE_SUGGESTIONS))
    page %= pages
    start = page * VISIBLE_SUGGESTIONS
    shown = EXAMPLES[start : start + VISIBLE_SUGGESTIONS]

    columns = st.columns([*([1] * VISIBLE_SUGGESTIONS), 0.16])
    for column, (glyph, example) in zip(columns, shown, strict=False):
        with column:
            if st.button(f"{glyph}   {example}", key=f"eg-{example}", width="stretch"):
                st.session_state["pending_question"] = example
                st.rerun()
    with columns[-1]:
        # A chevron, not a greater-than: it is typography for "more this way".
        if pages > 1 and st.button("›", key="suggest-next", help="More questions"):  # noqa: RUF001
            st.session_state["suggest_page"] = page + 1
            st.rerun()


def recent_strip() -> None:
    runs = recent_runs(limit=4)
    if not runs:
        return
    head, link = st.columns([5, 0.9])
    with head:
        ui.section("Recent Analyses")
    with link:
        if st.button("View all ›", key="view-all", width="stretch"):  # noqa: RUF001
            st.session_state["page"] = "saved"
            st.rerun()
    columns = st.columns(len(runs))
    current = st.session_state.get("run_id")
    for column, run in zip(columns, runs, strict=True):
        with column:
            label = run["question"]
            label = label if len(label) <= 52 else label[:51] + "…"
            if st.button(label, key=f"recent-{run['run_id']}", width="stretch"):
                open_run(run["run_id"])
            st.markdown(
                f'<div class="recent{" current" if run["run_id"] == current else ""}" '
                'style="margin-top:-.55rem;border-top-left-radius:0;border-top-right-radius:0">'
                f'<div class="meta" style="margin-top:0">'
                f'<span class="when">{ui.ago(run.get("created_at"))}</span>'
                f'{status_chip(run.get("status"))}</div></div>',
                unsafe_allow_html=True,
            )


def ask_page(who: str) -> None:
    header("Hello, Analyst 👋", "Ask a business question. Get answers you can check.")
    ask_card()
    suggestions()
    recent_strip()

    # The result block is part of the page, not a mode it switches into: with no run selected it
    # shows the latest one. An analyst arriving at this page is usually coming back to the answer
    # they just asked for, and an empty page below the box would make them hunt for it.
    run_id = st.session_state.get("run_id")
    if not run_id:
        latest = recent_runs(limit=1)
        run_id = latest[0]["run_id"] if latest else None
    if run_id:
        st.markdown("")
        result_card(run_id, who)


# --- the result ---------------------------------------------------------------


def result_card(run_id: str, who: str) -> None:
    try:
        run = api().run(run_id)
        trace = api().trace(run_id)
    except ApiError as exc:
        st.error(f"Could not load that run: {exc.detail}")
        return

    status = run["status"]

    with st.container(border=True):
        ui.result_head(run)

        if run.get("pending_approvals"):
            st.markdown(
                '<div class="panel-title">Waiting on your decision</div>',
                unsafe_allow_html=True,
            )
            if ui.approval_banner(run_id, run["pending_approvals"], api(), who):
                time.sleep(0.6)
                st.rerun()

        if status == "clarifying":
            st.markdown(
                '<div class="panel-title">The agent has a question</div>',
                unsafe_allow_html=True,
            )
            st.info("It stopped rather than guessing what you meant.")
            for item in run.get("clarifications", []):
                if not item.get("answer"):
                    st.markdown(f"**{item['question']}**")
            reply = st.text_input("Your answer", key=f"clarify-{run_id}")
            if st.button("Send", type="primary", key=f"send-{run_id}") and reply.strip():
                api().answer(run_id, reply.strip())
                time.sleep(0.6)
                st.rerun()

        if run.get("answer"):
            # Three panels, in the order a reader uses them: what it concluded, the shape over
            # time, the split. The evidence line under them is the way back to the SQL.
            left, middle, right = st.columns([1.05, 1.5, 1])
            with left:
                ui.takeaways(run)
            with middle:
                ui.chart_panel(run.get("charts", []), 0, "Trend", mode())
            with right:
                ui.chart_panel(run.get("charts", []), 1, "Breakdown", mode())
            ui.evidence_footer(run, trace)
        else:
            if status in LIVE_STATUSES:
                st.markdown(
                    '<div class="panel-title">Working '
                    '<span class="hint">a few minutes; the steps appear as they finish</span>'
                    "</div>",
                    unsafe_allow_html=True,
                )
            elif run.get("error"):
                st.error(f"{run['error'].get('type')}: {run['error'].get('message')}")
            ui.timeline(trace.get("steps", []))

    if run.get("answer"):
        answer_tab, working_tab = st.tabs(["Answer", "How it got there"])
        with answer_tab:
            ui.conclusion(run["answer"])
            ui.evidence_drawer(run["answer"])
            ui.findings(run.get("findings", []))
        with working_tab:
            ui.stat_row(
                [
                    ("Queries run", trace["summary"].get("queries_executed", 0)),
                    ("Blocked", trace["summary"].get("queries_rejected", 0)),
                    ("Escalated", trace["summary"].get("queries_escalated", 0)),
                    ("Refuted", trace["summary"].get("hypotheses_refuted", 0)),
                    ("Tokens", f"{run.get('tokens_in', 0) + run.get('tokens_out', 0):,}"),
                ]
            )
            ui.timeline(trace.get("steps", []))
            ui.query_audit(trace.get("queries", []))
            decisions(trace)
    else:
        ui.query_audit(trace.get("queries", []))

    if status in LIVE_STATUSES:
        time.sleep(POLL_SECONDS)
        st.rerun()


def decisions(trace: dict[str, Any]) -> None:
    if not trace.get("approvals"):
        return
    st.markdown("## Decisions")
    for approval in trace["approvals"]:
        by = approval.get("decided_by")
        why = approval.get("decision_reason")
        st.markdown(
            f'<div class="chip-row">{status_chip(approval["status"])}</div>'
            f"<div><strong>{approval['kind'].replace('_', ' ')}</strong> — "
            f"{approval['reason']}</div>"
            + (
                f'<div class="tl-meta">decided by {by}' + (f" — {why}" if why else "") + "</div>"
                if by
                else ""
            ),
            unsafe_allow_html=True,
        )


# --- dashboard ----------------------------------------------------------------


def dashboard_page() -> None:
    header("Dashboard", "Every run this instance has done, and what it cost.")
    runs = recent_runs(limit=50)
    if not runs:
        st.info("No runs yet. Ask a question and this fills in.")
        return

    counts: dict[str, int] = {}
    for run in runs:
        counts[run["status"]] = counts.get(run["status"], 0) + 1
    tokens = sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in runs)
    durations = [r["duration_ms"] for r in runs if r.get("duration_ms")]

    ui.stat_row(
        [
            ("Runs", len(runs)),
            ("Completed", counts.get("completed", 0)),
            ("Awaiting a decision", counts.get("awaiting_approval", 0)),
            ("Asked you something", counts.get("clarifying", 0)),
            ("Tokens", f"{tokens:,}"),
            (
                "Median run",
                f"{sorted(durations)[len(durations) // 2] // 1000}s" if durations else "—",
            ),
        ]
    )

    left, right = st.columns([1, 1])
    with left, st.container(border=True):
        st.markdown('<div class="panel-title">Runs by outcome</div>', unsafe_allow_html=True)
        outcome_chart(counts)
    with right, st.container(border=True):
        st.markdown('<div class="panel-title">Latest</div>', unsafe_allow_html=True)
        for run in runs[:6]:
            question = run["question"]
            st.markdown(
                f'<div class="takeaway"><span class="ico">·</span><span class="t">'
                f'{question if len(question) <= 70 else question[:69] + "…"}<br>'
                f'<span style="color:var(--text-muted);font-size:.76rem">'
                f'{ui.ago(run.get("created_at"))}</span></span></div>',
                unsafe_allow_html=True,
            )


def outcome_chart(counts: dict[str, int]) -> None:
    """Run outcomes as a horizontal bar.

    Status colour is the right palette here — these *are* states, not series — and each bar keeps
    its label and icon, so the chart is readable without the colour.
    """
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    labels = [f"{status_of(k).icon} {status_of(k).label}" for k, _ in ordered]
    values = [v for _, v in ordered]
    colours = [status_of(k).colour for k, _ in ordered]

    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={"color": colours, "line": {"width": 0}},
            text=values,
            textposition="outside",
            hovertemplate="%{y}: %{x}<extra></extra>",
            width=0.55,
        )
    )
    layout = chart_layout(mode())
    figure.update_layout(
        **layout,
        height=60 + 42 * len(ordered),
        showlegend=False,
        bargap=0.35,
    )
    figure.update_xaxes(showgrid=True, zeroline=False, title=None)
    figure.update_yaxes(showgrid=False, autorange="reversed", title=None)
    st.plotly_chart(figure, width="stretch")


# --- saved --------------------------------------------------------------------


def saved_page() -> None:
    header("Saved Analyses", "Every question asked, and the answer it reached.")
    runs = recent_runs(limit=40)
    if not runs:
        st.info("Nothing yet.")
        return

    query = st.text_input("Filter", placeholder="Search the questions…", key="saved-filter")
    if query:
        runs = [r for r in runs if query.lower() in r["question"].lower()]

    for run in runs:
        left, right = st.columns([5, 1])
        with left:
            if st.button(run["question"], key=f"saved-{run['run_id']}", width="stretch"):
                open_run(run["run_id"])
        with right:
            st.markdown(
                f'<div style="padding-top:.45rem">{status_chip(run.get("status"))}</div>',
                unsafe_allow_html=True,
            )


# --- metrics ------------------------------------------------------------------


def metrics_page() -> None:
    header("Metrics Catalog", "The approved definitions. The agent may use these, not invent one.")
    catalogue = metric_catalogue()
    if not catalogue:
        st.info("Unavailable while the API is down.")
        return

    st.caption(
        f"{len(catalogue)} approved metrics. Ask for one by name and the registry renders the "
        "statement — no SQL from the model reaches the warehouse for these."
    )
    for row in range(0, len(catalogue), 3):
        for column, metric in zip(st.columns(3), catalogue[row : row + 3], strict=False):
            with column:
                dimensions = ", ".join(metric.get("dimensions", [])) or "—"
                caveats = "".join(
                    f'<div class="tl-meta">— {c}</div>' for c in metric.get("caveats", [])[:2]
                )
                st.markdown(
                    f'<div class="card"><div class="panel-title">{metric["title"]}</div>'
                    f'<div class="tl-meta" style="margin-bottom:.4rem">'
                    f'<code>{metric["name"]}</code> · {metric["definition_version"]}</div>'
                    f'<div class="chip-row">'
                    f'<span class="chip">{metric["unit"]}</span>'
                    f'<span class="chip">per {metric["grain"]}</span>'
                    f'<span class="chip">{metric["shape"]}</span></div>'
                    f'<div class="tl-meta">By: {dimensions}</div>{caveats}</div>',
                    unsafe_allow_html=True,
                )


# --- schema -------------------------------------------------------------------


def schema_page() -> None:
    header("Data Explorer", "What is queryable, and what the column policy protects.")
    catalogue = schema_catalogue()
    objects = catalogue.get("objects", [])
    if not objects:
        st.info("Unavailable while the API is down.")
        return

    restricted = sum(1 for o in objects for c in o["columns"] if c["restricted"])
    ui.stat_row(
        [
            ("Schemas", len(catalogue.get("schemas", []))),
            ("Tables", len(objects)),
            ("Columns", sum(len(o["columns"]) for o in objects)),
            ("Restricted columns", restricted),
        ]
    )
    st.caption(
        "A restricted column is not hidden from the agent — it is listed, never sampled, and "
        "projecting one requires a human decision."
    )

    for obj in objects:
        locked = [c["name"] for c in obj["columns"] if c["restricted"]]
        title = f"{obj['name']}  ·  {len(obj['columns'])} columns"
        if locked:
            title += f"  ·  🔒 {len(locked)} restricted"
        with st.expander(title):
            for column in obj["columns"]:
                mark = "🔒 " if column["restricted"] else ""
                tone = "var(--text)" if not column["restricted"] else "#b23c3c"
                st.markdown(
                    f'<div class="tl-meta" style="color:{tone}">{mark}{column["name"]}</div>',
                    unsafe_allow_html=True,
                )


# --- settings -----------------------------------------------------------------


def settings_page() -> None:
    header("Settings", "What this interface is talking to, and what it is allowed to see.")
    reachable = api().healthy()
    catalogue = metric_catalogue()
    schema = schema_catalogue()

    with st.container(border=True):
        st.markdown('<div class="panel-title">Connection</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="chip-row">{status_chip("completed" if reachable else "failed")}</div>'
            f'<div class="tl-meta">API: <code>{api().base_url}</code></div>'
            f'<div class="tl-meta">Approved metrics loaded: {len(catalogue)}</div>'
            f'<div class="tl-meta">Queryable tables: {len(schema.get("objects", []))}</div>',
            unsafe_allow_html=True,
        )

    with st.container(border=True):
        st.markdown('<div class="panel-title">Appearance</div>', unsafe_allow_html=True)
        st.caption(
            "Dark mode is a selected palette, not an inverted one: the chart series have their own "
            "steps validated against the dark surface."
        )
        swatches = "".join(
            f'<span class="chip" style="background:{c};border-color:{c};color:transparent">···</span>'
            for c in (DARK if st.session_state.get("dark") else LIGHT).categorical
        )
        st.markdown(f'<div class="chip-row">{swatches}</div>', unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown('<div class="panel-title">This interface never touches the database</div>',
                    unsafe_allow_html=True)
        st.caption(
            "Everything above arrived through the API, so the interface cannot display something the "
            "API would not. Approvals you make here are recorded against the name in the sidebar."
        )


# --- routing ------------------------------------------------------------------

PAGES = {
    "ask": ask_page,
    "dashboard": lambda who: dashboard_page(),
    "saved": lambda who: saved_page(),
    "metrics": lambda who: metrics_page(),
    "schema": lambda who: schema_page(),
    "settings": lambda who: settings_page(),
}


def main() -> None:
    page, who = sidebar()
    PAGES.get(page, ask_page)(who)


main()
