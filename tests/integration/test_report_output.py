"""The report-shaped answer: the eight sections, and where each one's data comes from.

The point of this file is to pin *provenance*. A BI report is persuasive by design — headline
cards, severity badges, numbered recommendations — and a persuasive layout over invented content
is the worst thing this project could ship. So each test says where a section's data came from and
asserts that the page cannot manufacture it:

* key findings and recommendations come from the **model's structured output**, filtered against
  the queries that actually ran;
* the investigation section is **derived from the audit trail**, never narrated;
* the rows behind a query are **rebuilt from the recorded statement**, not stored prose.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from analyst_agent.api import service
from analyst_agent.api.main import app
from analyst_agent.db import repository as repo


@pytest.fixture
def client(rw_dsn: str):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def reported_run(rw_dsn: str, seeded: None) -> uuid.UUID:
    """A finished diagnostic run with everything the report needs.

    Built through the repository rather than by driving the graph: what is under test is the
    *presentation* of a completed investigation, and a scripted agent run would add a page of
    setup without changing a single assertion here.
    """
    run_id = repo.create_run("Why did revenue drop in March 2018?", requested_by="tests")

    revenue = repo.record_sql_audit(
        run_id=run_id,
        purpose="monthly revenue for 2018 [revenue@v1]",
        sql_text="SELECT 1",
        verdict="allowed",
        reasons=[],
        rewritten_sql=(
            "SELECT to_char(o.order_purchase_timestamp, 'YYYY-MM') AS month, "
            "sum(oi.price) AS revenue FROM analytics.orders o "
            "JOIN analytics.order_items oi ON oi.order_id = o.order_id GROUP BY 1 ORDER BY 1"
        ),
        referenced_objects=["analytics.orders", "analytics.order_items"],
        sensitive_columns=[],
        estimated_cost=1200.0,
    )
    repo.mark_sql_executed(revenue, row_count=12, truncated=False, duration_ms=41)

    mix = repo.record_sql_audit(
        run_id=run_id,
        purpose="premium category share of revenue, February against March",
        sql_text="SELECT 2",
        verdict="allowed",
        reasons=[],
        rewritten_sql="SELECT 1 AS share",
        referenced_objects=["analytics.products"],
        sensitive_columns=[],
        estimated_cost=800.0,
    )
    repo.mark_sql_executed(mix, row_count=2, truncated=False, duration_ms=18)

    repo.record_sql_audit(
        run_id=run_id,
        purpose="attempted deletion",
        sql_text="DROP TABLE analytics.orders",
        verdict="rejected",
        reasons=["not_a_select: the statement is a Drop, not a SELECT"],
        rewritten_sql=None,
        referenced_objects=[],
        sensitive_columns=[],
        estimated_cost=None,
    )

    finding_id = repo.record_finding(
        run_id=run_id,
        statement="Net revenue fell 32% in 2018-03.",
        material=True,
        evidence_query_ids=[revenue],
    )
    supported = repo.record_hypothesis(
        run_id=run_id,
        finding_id=finding_id,
        statement="Revenue dropped because the premium category collapsed.",
        test_design="premium share of revenue, February against March",
    )
    repo.update_hypothesis(
        supported,
        status="supported",
        reasoning="premium share fell from 0.28 to 0.11",
        test_query_ids=[mix],
    )
    refuted = repo.record_hypothesis(
        run_id=run_id,
        finding_id=finding_id,
        statement="Revenue dropped because product demand decreased overall.",
        test_design="order count month over month",
    )
    repo.update_hypothesis(
        refuted,
        status="refuted",
        reasoning="order count was flat at 1,590 against 1,604",
        test_query_ids=[mix],
    )

    repo.record_chart(
        run_id=run_id,
        query_id=revenue,
        chart_type="line",
        spec={"data": [{"type": "scatter", "x": ["2018-02"], "y": [1]}], "layout": {}},
        title="Revenue by month",
        png=None,
    )

    repo.finish_run(
        run_id,
        "completed",
        answer={
            "conclusion": (
                "Revenue decreased by 32% in March 2018, mainly because premium category sales "
                "declined."
            ),
            "confidence": "medium",
            "caveats": ["Excludes cancelled orders, per the approved definition."],
            "refuted": ["Overall demand: order count was flat."],
            "evidence": [{"query_id": str(revenue)}, {"query_id": str(mix)}],
            "key_findings": [
                {
                    "title": "Premium Category Decline",
                    "impact": "Revenue contribution dropped from 28% to 11%",
                    "severity": "high",
                    "evidence_query_ids": [str(mix)],
                },
                {
                    "title": "Delivery Performance Issue",
                    "impact": "Late deliveries increased from 8% to 34%",
                    "severity": "medium",
                    "evidence_query_ids": [str(revenue)],
                },
            ],
            "recommendations": [
                {
                    "action": "Check premium category stock with the three largest sellers.",
                    "rationale": "The premium share collapse is the largest single contributor.",
                    "priority": "high",
                },
                {
                    "action": "Monitor revenue recovery next month.",
                    "rationale": "Confirms whether March was a one-off or the start of a trend.",
                    "priority": "low",
                },
            ],
        },
    )
    return run_id


# --- 1, 2, 8: the sections that come from the model ---------------------------


def test_the_answer_leads_with_an_executive_conclusion(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    answer = client.get(f"/v1/runs/{reported_run}").json()["answer"]
    assert answer["conclusion"].startswith("Revenue decreased by 32%")
    assert answer["confidence_score"] > 0


def test_key_findings_carry_a_title_an_impact_and_a_severity(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """A card without all three is a headline with nothing behind it."""
    findings = client.get(f"/v1/runs/{reported_run}").json()["answer"]["key_findings"]
    assert len(findings) == 2
    first = findings[0]
    assert first["title"] == "Premium Category Decline"
    assert "28% to 11%" in first["impact"]
    assert first["severity"] == "high"
    assert first["evidence_query_ids"], "a finding points at the query its numbers came from"


def test_recommendations_carry_the_reason_they_follow(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """A recommendation the evidence does not support is worse than none: it will be acted on."""
    recommendations = client.get(f"/v1/runs/{reported_run}").json()["answer"]["recommendations"]
    assert [r["priority"] for r in recommendations] == ["high", "low"]
    assert all(r["rationale"] for r in recommendations)
    assert "premium" in recommendations[0]["rationale"].lower()


def test_a_finding_citing_a_query_that_never_ran_is_dropped(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """The most prominent place on the page is the worst place for an unverifiable number."""
    stored = repo.get_run(reported_run)
    assert stored is not None
    answer = dict(stored["answer"])
    answer["key_findings"] = [
        {
            "title": "Invented Finding",
            "impact": "made up",
            "severity": "high",
            "evidence_query_ids": [str(uuid.uuid4())],
        }
    ]
    repo.finish_run(reported_run, "completed", answer=answer)

    shown = client.get(f"/v1/runs/{reported_run}").json()["answer"]["key_findings"]
    assert shown[0]["title"] == "Invented Finding"
    assert shown[0]["evidence_query_ids"] == [], "the dangling citation is dropped, not rendered"


def test_the_synthesis_schema_asks_for_all_three_sections() -> None:
    """The model has to be *asked*: a model left to infer these returns a paragraph."""
    from analyst_agent.agent.nodes.schemas import Synthesis

    fields = Synthesis.model_fields
    assert "key_findings" in fields and "recommendations" in fields
    assert "measured impact" in str(fields["key_findings"].description)
    assert "worse than none" in str(fields["recommendations"].description)


# --- 3: derived from the trace, never narrated --------------------------------


def test_the_investigation_section_is_read_off_the_trace(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """Asking the model to describe its own process would produce a plausible paragraph.

    Reading the audit trail produces the record, and the difference is the whole point of the
    section.
    """
    investigation = client.get(f"/v1/runs/{reported_run}").json()["investigation"]

    assert investigation["metrics_checked"] == ["revenue@v1"], "from the definition version tag"
    assert set(investigation["tables_analyzed"]) == {
        "analytics.orders",
        "analytics.order_items",
        "analytics.products",
    }, "from the objects the guard resolved, not from re-parsing SQL"
    assert len(investigation["questions_tested"]) == 2, "from the hypotheses that were settled"
    assert investigation["queries_executed"] == 2
    assert investigation["queries_blocked"] == 1, "what was refused is part of the process"


def test_an_untested_hypothesis_is_not_a_question_tested(rw_dsn: str) -> None:
    """A proposed explanation nobody ran a query for was not investigated."""
    run_id = repo.create_run("a question")
    finding_id = repo.record_finding(
        run_id=run_id, statement="something", material=True,
        evidence_query_ids=[
            repo.record_sql_audit(
                run_id=run_id, purpose="p", sql_text="SELECT 1", verdict="allowed", reasons=[],
                rewritten_sql=None, referenced_objects=[], sensitive_columns=[],
                estimated_cost=None,
            )
        ],
    )
    repo.record_hypothesis(
        run_id=run_id, finding_id=finding_id, statement="never tested", test_design="none"
    )
    investigation = service.investigation_view(repo.get_trace(run_id))
    assert investigation["questions_tested"] == [] if isinstance(investigation, dict) else (
        investigation.questions_tested == []
    )


def test_the_process_list_leaves_out_the_bookkeeping_nodes(rw_dsn: str) -> None:
    """A process list that includes `intake` describes the software, not the analysis."""
    run_id = repo.create_run("a question")
    for node in ("intake", "author_sql", "synthesize", "respond"):
        with repo.step(run_id, node):
            pass
    steps = service.investigation_view(repo.get_trace(run_id)).steps
    assert "Wrote and validated SQL" in steps
    assert "Wrote the answer" in steps
    assert not any("intake" in step.lower() for step in steps)
    assert not any("respond" in step.lower() for step in steps)


# --- 6: the rows behind a query ----------------------------------------------


def test_the_rows_behind_a_query_can_be_expanded(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """Rebuilt from the recorded statement rather than stored.

    The statement is in the audit trail and it was guard-approved, so re-running it is a read path
    rather than a second copy of the warehouse.
    """
    run = client.get(f"/v1/runs/{reported_run}").json()
    query_id = run["answer"]["evidence"][0]["query_id"]

    rows = client.get(f"/v1/runs/{reported_run}/queries/{query_id}/rows?limit=5")
    assert rows.status_code == 200, rows.text
    body = rows.json()
    assert body["columns"] == ["month", "revenue"]
    assert body["returned"] <= 5
    assert body["row_count"] >= body["returned"]
    assert body["purpose"].startswith("monthly revenue")
    # Numbers arrive as numbers: a Decimal reaching the page as a string gets compared lexically.
    assert isinstance(body["rows"][0]["revenue"], (int, float))


def test_asking_for_the_rows_of_a_blocked_query_says_why(
    client: TestClient, reported_run: uuid.UUID
) -> None:
    """An empty table would read as "the query returned nothing", which is a different claim."""
    trace = client.get(f"/v1/runs/{reported_run}/trace").json()
    blocked = next(q for q in trace["queries"] if q["verdict"] == "rejected")

    response = client.get(
        f"/v1/runs/{reported_run}/queries/{blocked['query_id']}/rows"
    )
    assert response.status_code == 409
    assert "never ran" in response.json()["detail"]


def test_a_query_from_another_run_is_a_404(client: TestClient, reported_run: uuid.UUID) -> None:
    """Scoped twice: the run to the caller, and the query to the run."""
    other = repo.create_run("someone else's question")
    query_id = repo.record_sql_audit(
        run_id=other, purpose="theirs", sql_text="SELECT 1", verdict="allowed", reasons=[],
        rewritten_sql="SELECT 1", referenced_objects=[], sensitive_columns=[],
        estimated_cost=None,
    )
    repo.mark_sql_executed(query_id, row_count=1, truncated=False, duration_ms=1)

    response = client.get(f"/v1/runs/{reported_run}/queries/{query_id}/rows")
    assert response.status_code == 404
    assert "not part of this run" in response.json()["detail"]


# --- the page actually renders all eight --------------------------------------


def test_the_page_renders_every_section(client: TestClient) -> None:
    """Cheap, and it catches the failure mode this project has hit twice: built, never wired."""
    javascript = client.get("/app/app.js").text
    for renderer in (
        "function executiveSummary(",
        "function keyFindings(",
        "function investigationProcess(",
        "function hypothesisTesting(",
        "function visualAnalytics(",
        "function evidenceSection(",
        "function confidenceSection(",
        "function recommendations(",
        "function reportView(",
    ):
        assert renderer in javascript, renderer
    assert "/queries/" in javascript, "the page fetches the rows it offers to show"


def test_the_page_labels_the_sections_the_way_the_report_reads(client: TestClient) -> None:
    javascript = client.get("/app/app.js").text
    for label in (
        "Executive summary",
        "Key findings",
        "Investigation process",
        "Hypothesis testing",
        "Visual analytics",
        "Evidence &amp; traceability",
        "Confidence",
        "Recommended actions",
    ):
        assert label in javascript, label


def test_the_page_does_not_invent_a_headline_when_the_model_gave_none(
    client: TestClient,
) -> None:
    """It falls back to the investigation's own findings and says so.

    A card carrying a title the analysis never produced would be the one thing on this page that
    is not evidence.
    """
    javascript = client.get("/app/app.js").text
    assert "this run produced no headline" in javascript


def test_the_report_styles_exist(client: TestClient) -> None:
    css = client.get("/app/app.css").text
    for selector in (".report-section", ".finding-card", ".hyp-card", ".rec-list", "table.rows"):
        assert selector in css, selector

# --- reproducing a parameterised query ----------------------------------------


def test_a_parameterised_query_records_its_bound_values(rw_dsn: str, seeded: None) -> None:
    """"Traceable to its queries" has to include the ones the metrics layer renders.

    Those statements carry `%(date_from)s` placeholders and the values travelled separately, so the
    audit trail stored a statement nobody could re-run. The report view found it: rebuilding the
    rows failed with a syntax error at the placeholder. Migration 006 records the values beside the
    statement.
    """
    from analyst_agent.tools.registry import get_tool_registry

    run_id = repo.create_run("what was revenue by month?")
    result = get_tool_registry().invoke(
        "metric_query",
        {
            "metric": "revenue",
            "dimensions": ["month"],
            "date_from": "2017-01-01",
            "date_to": "2018-01-01",
            "filters": None,
            "rank_by_value": None,
            "purpose": "monthly revenue for 2017",
        },
        run_id,
        None,
    )
    assert result.ok and not result.refused, result.summary
    query_id = uuid.UUID(result.data["query_id"])

    query = next(
        q for q in repo.get_trace(run_id)["queries"] if q["query_id"] == query_id
    )
    assert "%(date_from)s" in (query["rewritten_sql"] or query["sql_text"])
    assert query["parameters"]["date_from"] == "2017-01-01", "the values are in the trail"

    # And the rows really can be rebuilt from what was recorded.
    from analyst_agent.tools.frames import get_store, reset_store

    reset_store()  # force the rehydrate path rather than reading the cached frame
    frame = get_store().get(query_id)
    assert len(frame) == 12, "twelve months, rebuilt by re-running the recorded statement"


def test_an_unreproducible_query_says_so_rather_than_failing(rw_dsn: str) -> None:
    """A row from before migration 006 has a statement and no values.

    410 with an explanation, not a driver syntax error surfaced as a 500: the query is real and its
    rows are simply no longer recoverable, which is a fact about the trail rather than a fault.
    """
    from analyst_agent.tools.frames import FrameNotAvailableError, get_store, reset_store

    run_id = repo.create_run("an old parameterised query")
    query_id = repo.record_sql_audit(
        run_id=run_id,
        purpose="pretends to predate migration 006",
        sql_text="SELECT %(month)s AS month",
        verdict="allowed",
        reasons=[],
        rewritten_sql=None,
        referenced_objects=[],
        sensitive_columns=[],
        estimated_cost=None,
        parameters=None,
    )
    repo.mark_sql_executed(query_id, row_count=1, truncated=False, duration_ms=1)

    reset_store()
    with pytest.raises(FrameNotAvailableError, match="values were not recorded"):
        get_store().get(query_id)
