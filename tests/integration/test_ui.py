"""The analyst interface, actually run.

A Streamlit page returning HTTP 200 proves almost nothing — the script runs per interaction and
renders client-side, so a broken layout still serves a 200 shell. ``AppTest`` executes the script
the way Streamlit does and surfaces any exception, which is the only way a UI of this kind gets
covered at all.

The API is stubbed rather than started: what is under test is the interface's own behaviour, and
a UI test that depends on a live agent run would be slow and would fail for reasons that have
nothing to do with the UI.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from analyst_agent.ui import components, theme

# Absolute: AppTest resolves a relative path against the *calling file*, not the repo root.
APP = str(
    Path(__file__).resolve().parents[2] / "src" / "analyst_agent" / "ui" / "streamlit_app.py"
)

RUN_ID = str(uuid.uuid4())
QUERY_ID = str(uuid.uuid4())

ANSWERED_RUN: dict[str, Any] = {
    "run_id": RUN_ID,
    "thread_id": f"run-{RUN_ID}",
    "question": "Why did revenue drop in March 2018?",
    "status": "completed",
    "created_at": "2026-08-26T09:00:00Z",
    "finished_at": "2026-08-26T09:02:00Z",
    "duration_ms": 120000,
    "queries_used": 3,
    "tokens_in": 12000,
    "tokens_out": 3000,
    "answer": {
        "conclusion": "Revenue fell 32% in March 2018, driven by a shift away from premium categories.",
        "confidence": "medium",
        "caveats": ["Excludes cancelled orders, per the approved definition."],
        "refuted": ["Delivery delays: lateness was flat across seller states."],
        "evidence": [
            {
                "query_id": QUERY_ID,
                "purpose": "monthly net revenue",
                "row_count": 24,
                "sql": "SELECT ym, sum(revenue) FROM analytics.v_order_revenue GROUP BY 1",
            }
        ],
    },
    "findings": [
        {
            "statement": "Net revenue fell 32% in 2018-03.",
            "material": True,
            "evidence_query_ids": [QUERY_ID],
            "hypotheses": [
                {
                    "statement": "The premium category share collapsed.",
                    "status": "supported",
                    "reasoning": "premium share fell from 0.28 to 0.11",
                    "test_query_ids": [QUERY_ID],
                },
                {
                    "statement": "Delivery delays pushed cancellations up.",
                    "status": "refuted",
                    "reasoning": "lateness was flat across states",
                    "test_query_ids": [QUERY_ID],
                },
            ],
        }
    ],
    "charts": [],
    "clarifications": [],
    "pending_approvals": [],
    "error": None,
}

TRACE: dict[str, Any] = {
    "run_id": RUN_ID,
    "summary": {
        "steps": 9,
        "queries_considered": 4,
        "queries_executed": 3,
        "queries_rejected": 1,
        "queries_escalated": 0,
        "approvals_pending": 0,
        "hypotheses_refuted": 1,
    },
    "steps": [
        {"seq": 1, "node": "intake", "status": "ok", "duration_ms": 5, "summary": None,
         "effort": None, "error": None},
        {"seq": 2, "node": "author_sql", "status": "ok", "duration_ms": 900,
         "summary": "monthly net revenue", "effort": "high", "error": None},
    ],
    "tool_calls": [],
    "queries": [
        {
            "query_id": QUERY_ID,
            "purpose": "monthly net revenue",
            "verdict": "allowed",
            "sql": "SELECT ym, sum(revenue) FROM analytics.v_order_revenue GROUP BY 1",
            "rewritten_sql": None,
            "reasons": [],
            "referenced_objects": ["analytics.v_order_revenue"],
            "sensitive_columns": [],
            "estimated_cost": 1200.0,
            "executed": True,
            "row_count": 24,
            "truncated": False,
            "duration_ms": 41,
        },
        {
            "query_id": str(uuid.uuid4()),
            "purpose": "attempted deletion",
            "verdict": "rejected",
            "sql": "DROP TABLE analytics.orders",
            "rewritten_sql": None,
            "reasons": ["not_a_select: the statement is a Drop, not a SELECT"],
            "referenced_objects": [],
            "sensitive_columns": [],
            "estimated_cost": None,
            "executed": False,
            "row_count": None,
            "truncated": False,
            "duration_ms": None,
        },
    ],
    "approvals": [],
}

METRICS = [
    {
        "name": "revenue",
        "title": "Net revenue",
        "unit": "currency",
        "grain": "order_item",
        "shape": "aggregate",
        "owner": "analytics-team",
        "aliases": ["sales"],
        "dimensions": ["month", "product_category"],
        "caveats": ["Excludes cancelled orders."],
        "definition_version": "revenue@v1",
    }
]


class StubApi:
    """Stands in for the HTTP client, so the UI is tested and the stack is not."""

    def __init__(self, run: dict[str, Any] | None = None) -> None:
        self._run = run or ANSWERED_RUN
        self.decisions: list[tuple[str, bool]] = []

    def healthy(self) -> bool:
        return True

    def metrics(self) -> list[dict[str, Any]]:
        return METRICS

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self._run]

    def run(self, run_id: str) -> dict[str, Any]:
        return self._run

    def trace(self, run_id: str) -> dict[str, Any]:
        return TRACE

    def ask(self, question: str, requested_by: str | None = None) -> dict[str, Any]:
        return {"run_id": RUN_ID, "thread_id": f"run-{RUN_ID}", "status": "received",
                "message": "accepted"}

    def decide(self, run_id: str, approval_id: str, approve: bool, decided_by: str,
               reason: str | None) -> dict[str, Any]:
        self.decisions.append((approval_id, approve))
        return {}

    def answer(self, run_id: str, answer: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    """Build an AppTest whose HTTP client is a stub.

    The patch lands on ``api_client.AnalystApi`` rather than on the page module: AppTest
    re-executes the script in a fresh namespace on every run, so anything patched into the
    page's own module is discarded. Patching the dependency it imports survives, because the
    fresh execution imports the already-patched module.
    """
    import streamlit as st

    from analyst_agent.ui import api_client

    def build(run: dict[str, Any] | None = None, run_id: str | None = None) -> AppTest:
        stub = StubApi(run)
        monkeypatch.setattr(api_client, "AnalystApi", lambda *a, **k: stub)
        # st.cache_resource outlives a single AppTest run, so a cached real client from an
        # earlier test would defeat the patch.
        st.cache_resource.clear()
        st.cache_data.clear()

        test = AppTest.from_file(APP, default_timeout=30)
        if run_id:
            test.session_state["run_id"] = run_id
        return test

    return build


# --- the page runs ----------------------------------------------------------


def test_the_landing_page_renders_without_error(app) -> None:
    test = app().run()
    assert not test.exception
    assert any("Ask" in h.value for h in test.markdown)


def test_a_finished_run_renders_without_error(app) -> None:
    test = app(run_id=RUN_ID).run()
    assert not test.exception


def test_the_conclusion_and_its_confidence_are_shown(app) -> None:
    test = app(run_id=RUN_ID).run()
    page = " ".join(m.value for m in test.markdown)
    assert "Revenue fell 32%" in page
    assert "Medium confidence" in page, "confidence is stated, not implied"


def test_what_was_ruled_out_appears_beside_the_answer(app) -> None:
    """Naming what was disproved is how a reader knows the agent looked."""
    test = app(run_id=RUN_ID).run()
    page = " ".join(m.value for m in test.markdown)
    assert "Ruled out" in page
    assert "Delivery delays" in page


def test_both_hypotheses_are_shown_with_their_verdicts(app) -> None:
    test = app(run_id=RUN_ID).run()
    page = " ".join(m.value for m in test.markdown)
    assert "premium category share collapsed" in page
    assert "Supported" in page
    assert "Refuted" in page


def test_the_sql_behind_the_answer_is_reachable(app) -> None:
    """A conclusion is worth what the evidence you can reach from it is worth."""
    test = app(run_id=RUN_ID).run()
    code = " ".join(block.value for block in test.code)
    assert "v_order_revenue" in code


def test_a_blocked_query_is_visible_not_hidden(app) -> None:
    """What the agent tried is usually what a reviewer wants to know."""
    test = app(run_id=RUN_ID).run()
    code = " ".join(block.value for block in test.code)
    assert "DROP TABLE analytics.orders" in code
    page = " ".join(m.value for m in test.markdown)
    assert "not_a_select" in page


def test_a_run_awaiting_approval_shows_the_statement_and_both_buttons(app) -> None:
    """Rejection is a real answer here, so it is as reachable as approval."""
    parked = {
        **ANSWERED_RUN,
        "status": "awaiting_approval",
        "answer": None,
        "pending_approvals": [
            {
                "approval_id": str(uuid.uuid4()),
                "kind": "sensitive_column",
                "reason": "query projects analytics.customer_contact.email",
                "payload": {
                    "sql": "SELECT email FROM analytics.customer_contact",
                    "sensitive_columns": ["analytics.customer_contact.email"],
                    "estimated_cost": 118.0,
                },
                "status": "pending",
                "requested_at": "2026-08-26T09:01:00Z",
                "expires_at": "2026-08-26T09:31:00Z",
                "decided_at": None,
                "decided_by": None,
                "decision_reason": None,
            }
        ],
    }
    test = app(run=parked, run_id=RUN_ID).run()
    assert not test.exception

    code = " ".join(block.value for block in test.code)
    assert "SELECT email FROM analytics.customer_contact" in code, "the reviewer sees the statement"

    labels = {button.label for button in test.button}
    assert "Approve" in labels
    assert "Reject" in labels


def test_a_clarifying_run_offers_a_reply_box(app) -> None:
    asking = {**ANSWERED_RUN, "status": "clarifying", "answer": None}
    test = app(run=asking, run_id=RUN_ID).run()
    assert not test.exception
    page = " ".join(m.value for m in test.markdown)
    assert "has a question" in page


# --- the design tokens ------------------------------------------------------


def test_status_is_never_carried_by_colour_alone() -> None:
    """A screenshot, a print-out, or a colourblind reader must all still parse the state."""
    for key in ("supported", "refuted", "inconclusive", "rejected", "approved"):
        status = theme.status_of(key)
        assert status.icon.strip(), f"{key} has no icon"
        assert status.label.strip(), f"{key} has no label"
        markup = theme.status_chip(key)
        assert status.label in markup
        assert status.icon in markup


def test_the_ui_accent_comes_from_the_validated_chart_palette() -> None:
    """A chart and the card around it have to read as one system."""
    from analyst_agent.tools.palette import LIGHT

    assert LIGHT.categorical[0] in theme.stylesheet()


def test_the_stylesheet_defines_a_dark_variant() -> None:
    css = theme.stylesheet()
    assert "prefers-color-scheme: dark" in css
    from analyst_agent.tools.palette import DARK

    assert DARK.surface in css


def test_status_colours_are_not_reused_as_series_colours() -> None:
    """Status colour is reserved; reusing a series hue for 'failed' would be a lie."""
    from analyst_agent.tools.palette import CATEGORICAL_DARK, CATEGORICAL_LIGHT

    series = {c.lower() for c in (*CATEGORICAL_LIGHT, *CATEGORICAL_DARK)}
    for key in ("supported", "refuted", "inconclusive"):
        assert theme.status_of(key).colour.lower() not in series


def test_a_chip_renders_label_icon_and_colour() -> None:
    markup = components.st and theme.chip("Blocked", "✕", "#b23c3c")
    assert "Blocked" in markup and "✕" in markup and "#b23c3c" in markup
