"""The HTTP surface.

Driven with a scripted model, like the graph tests, so what is being checked is the API's own
behaviour — status codes, shapes, and the two things a caller most needs to be true: that a run
id comes back before the work starts, and that the trace shows what the agent *tried*, not only
what it did.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from analyst_agent.api.main import app
from analyst_agent.db import repository as repo

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(rw_dsn: str):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def run_id(rw_dsn: str):
    """A run created directly, so the API's read endpoints have something real to return."""
    rid = repo.create_run("Why did revenue drop in 2018-03?", requested_by="pytest")
    yield rid
    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent.runs WHERE run_id = %s", (rid,))


# --- health -----------------------------------------------------------------


def test_healthz_is_liveness_only(client: TestClient) -> None:
    """It must stay true while the database is down, so it touches nothing."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_verifies_the_read_only_role(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["read_only_verified"] is True


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    """A report of 'my run failed' has to lead to the log lines that describe it."""
    response = client.get("/healthz")
    assert response.headers.get("x-request-id")


def test_a_supplied_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"x-request-id": "trace-me"})
    assert response.headers["x-request-id"] == "trace-me"


# --- catalogue --------------------------------------------------------------


def test_the_metric_catalogue_is_served(client: TestClient) -> None:
    response = client.get("/v1/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert len(metrics) >= 12
    revenue = next(m for m in metrics if m["name"] == "revenue")
    assert revenue["definition_version"] == "revenue@v1"
    assert revenue["caveats"], "a metric without its caveats is a number without its meaning"


def test_the_schema_endpoint_flags_restricted_columns_rather_than_hiding_them(
    client: TestClient, seeded: None
) -> None:
    response = client.get("/v1/schema")
    assert response.status_code == 200
    objects = {o["name"]: o for o in response.json()["objects"]}

    contact = objects["analytics.customer_contact"]
    email = next(c for c in contact["columns"] if c["name"] == "email")
    assert email["restricted"] is True

    orders = objects["analytics.orders"]
    assert all(c["restricted"] is False for c in orders["columns"])


# --- runs -------------------------------------------------------------------


def test_an_unknown_run_is_a_404(client: TestClient) -> None:
    response = client.get(f"/v1/runs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_a_malformed_run_id_is_a_422(client: TestClient) -> None:
    assert client.get("/v1/runs/not-a-uuid").status_code == 422


def test_a_question_must_not_be_empty(client: TestClient) -> None:
    assert client.post("/v1/questions", json={"question": ""}).status_code == 422


def test_an_unknown_field_is_rejected(client: TestClient) -> None:
    """extra=forbid, so a typo in a field name fails loudly rather than being ignored."""
    response = client.post(
        "/v1/questions", json={"question": "what was revenue?", "requestedby": "typo"}
    )
    assert response.status_code == 422


def test_a_run_view_reports_status_and_question(client: TestClient, run_id: uuid.UUID) -> None:
    response = client.get(f"/v1/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["question"] == "Why did revenue drop in 2018-03?"
    assert body["status"] == "received"
    assert body["answer"] is None


def test_the_trace_shows_what_was_refused_not_only_what_ran(
    client: TestClient, run_id: uuid.UUID
) -> None:
    """The question a reviewer usually has is 'what did it try', so a blocked attempt is in the
    trace with its reasons rather than absent from it."""
    repo.record_sql_audit(
        run_id,
        purpose="attempted deletion",
        sql_text="DROP TABLE analytics.orders",
        verdict="rejected",
        reasons=["not_a_select: the statement is a Drop, not a SELECT"],
    )
    executed = repo.record_sql_audit(
        run_id, purpose="monthly revenue", sql_text="SELECT 1", verdict="allowed"
    )
    repo.mark_sql_executed(executed, row_count=24, truncated=False, duration_ms=40)

    response = client.get(f"/v1/runs/{run_id}/trace")
    assert response.status_code == 200
    body = response.json()

    assert body["summary"]["queries_considered"] == 2
    assert body["summary"]["queries_rejected"] == 1
    rejected = next(q for q in body["queries"] if q["verdict"] == "rejected")
    assert rejected["executed"] is False
    assert "not_a_select" in rejected["reasons"][0]


def test_an_answer_resolves_its_evidence_to_real_queries(
    client: TestClient, run_id: uuid.UUID
) -> None:
    """Every number in an answer must lead back to a query that actually ran."""
    query_id = repo.record_sql_audit(
        run_id,
        purpose="monthly net revenue",
        sql_text="SELECT ym, sum(revenue) FROM analytics.v_order_revenue GROUP BY 1",
        verdict="allowed",
    )
    repo.mark_sql_executed(query_id, row_count=24, truncated=False, duration_ms=41)
    repo.finish_run(
        run_id,
        "completed",
        answer={
            "conclusion": "Revenue fell 32% in March 2018.",
            "confidence": "medium",
            "caveats": ["Excludes cancelled orders."],
            "refuted": ["delivery delays"],
            "evidence": [{"query_id": str(query_id)}],
        },
    )

    body = client.get(f"/v1/runs/{run_id}").json()
    answer = body["answer"]
    assert answer["confidence"] == "medium"
    assert answer["refuted"] == ["delivery delays"]
    assert len(answer["evidence"]) == 1
    evidence = answer["evidence"][0]
    assert evidence["query_id"] == str(query_id)
    assert "v_order_revenue" in evidence["sql"], "the SQL travels with the claim"
    assert evidence["row_count"] == 24


def test_findings_carry_their_hypotheses(client: TestClient, run_id: uuid.UUID) -> None:
    query_id = repo.record_sql_audit(
        run_id, purpose="revenue", sql_text="SELECT 1", verdict="allowed"
    )
    repo.mark_sql_executed(query_id, row_count=1, truncated=False, duration_ms=5)
    finding_id = repo.record_finding(run_id, "Revenue fell 32%.", [query_id], material=True)
    supported = repo.record_hypothesis(run_id, finding_id, "Mix shift.", "category share")
    refuted = repo.record_hypothesis(run_id, finding_id, "Delivery delays.", "late rate by state")
    repo.update_hypothesis(supported, "supported", [query_id], "premium share fell")
    repo.update_hypothesis(refuted, "refuted", [query_id], "lateness was flat")

    body = client.get(f"/v1/runs/{run_id}").json()
    finding = body["findings"][0]
    assert finding["material"] is True
    statuses = sorted(h["status"] for h in finding["hypotheses"])
    assert statuses == ["refuted", "supported"]


# --- approvals --------------------------------------------------------------


def test_a_pending_approval_is_surfaced_on_the_run(
    client: TestClient, run_id: uuid.UUID
) -> None:
    repo.create_approval(
        run_id,
        kind="sensitive_column",
        reason="query projects analytics.customer_contact.email",
        payload={"sql": "SELECT email FROM analytics.customer_contact"},
        timeout_seconds=1800,
    )
    body = client.get(f"/v1/runs/{run_id}").json()
    assert len(body["pending_approvals"]) == 1
    assert body["pending_approvals"][0]["kind"] == "sensitive_column"


def test_an_approval_can_be_decided_once(client: TestClient, run_id: uuid.UUID) -> None:
    approval_id = repo.create_approval(
        run_id,
        kind="expensive_query",
        reason="estimated cost above the ceiling",
        payload={"estimated_cost": 9_000_000},
        timeout_seconds=1800,
    )
    response = client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}/approve",
        json={"decided_by": "analyst@example.com", "reason": "acceptable for a one-off"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "analyst@example.com"


def test_deciding_twice_is_refused_rather_than_overwritten(
    client: TestClient, run_id: uuid.UUID
) -> None:
    """The first decision is the one that was made; a second silently replacing it would
    falsify the audit."""
    approval_id = repo.create_approval(
        run_id, kind="export", reason="publishing", payload={}, timeout_seconds=1800
    )
    first = client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}/reject",
        json={"decided_by": "analyst@example.com"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}/approve",
        json={"decided_by": "someone-else@example.com"},
    )
    assert second.status_code == 409

    still = client.get(f"/v1/runs/{run_id}/approvals").json()
    assert still[0]["status"] == "rejected"
    assert still[0]["decided_by"] == "analyst@example.com"


def test_a_decision_must_name_who_made_it(client: TestClient, run_id: uuid.UUID) -> None:
    approval_id = repo.create_approval(
        run_id, kind="export", reason="publishing", payload={}, timeout_seconds=1800
    )
    response = client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}/approve", json={"reason": "no name given"}
    )
    assert response.status_code == 422


def test_answering_a_run_that_is_not_asking_is_a_conflict(
    client: TestClient, run_id: uuid.UUID
) -> None:
    response = client.post(f"/v1/runs/{run_id}/answer", json={"answer": "March 2018"})
    assert response.status_code == 409


# --- listing ----------------------------------------------------------------


def test_recent_runs_are_listed(client: TestClient, run_id: uuid.UUID) -> None:
    body = client.get("/v1/runs?limit=10").json()
    assert any(r["run_id"] == str(run_id) for r in body)


def test_openapi_documents_every_route(client: TestClient) -> None:
    """The API is a deliverable, so /docs has to actually describe it."""
    spec: dict[str, Any] = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    for expected in (
        "/v1/questions",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/trace",
        "/v1/runs/{run_id}/stream",
        "/v1/metrics",
        "/v1/schema",
        "/healthz",
        "/readyz",
    ):
        assert expected in paths, f"{expected} is missing from the OpenAPI document"
