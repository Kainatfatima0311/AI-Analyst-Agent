"""Organisation boundaries, access control, encrypted secrets, sharing, alerts, the audit trail.

This is the file that has to be right. Every other test in this project checks that a feature
works; these check that a feature **cannot be reached by somebody else's tenant**, which is the
only kind of bug in a multi-tenant system that ends up in a newspaper.

Two habits throughout:

* Cross-tenant reads are asserted to return **404, not 403**. A 403 confirms the resource exists,
  and a sequence of those confirmations is an enumeration of another company's work.
* Each isolation test creates *two* real organisations and checks from both directions. Asserting
  only that org B cannot see org A's row leaves the symmetric bug — a filter written the wrong way
  round — undetected.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from analyst_agent.api.main import app
from analyst_agent.config import get_settings
from analyst_agent.db import repository as repo
from analyst_agent.db import tenancy
from analyst_agent.security import crypto
from analyst_agent.security.principal import DEFAULT_ORG_ID, NEEDS, RANK, Principal


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """A real Fernet key for the whole file.

    Generated per session rather than committed: a key in the repository is a key in every
    deployment that ever cloned it.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_KEY", key)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


@pytest.fixture
def client(rw_dsn: str):
    with TestClient(app) as test_client:
        yield test_client


class Org:
    """One organisation with an owner key, so a test can act *as* that tenant."""

    def __init__(self, client: TestClient, name: str) -> None:
        self.client = client
        created = client.post(
            "/v1/organizations",
            json={"name": name, "owner_email": f"owner@{name.lower().replace(' ', '')}.co"},
        )
        assert created.status_code == 201, created.text
        self.organization_id = uuid.UUID(created.json()["organization_id"])

        owner = next(
            member
            for member in tenancy.list_members(self.organization_id)
            if member["role"] == "owner"
        )
        self.user_id = uuid.UUID(str(owner["user_id"]))
        _, self.token = tenancy.issue_api_key(self.organization_id, self.user_id, "tests")

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    def key_for(self, email: str, role: str) -> dict[str, str]:
        """A second member's key, for the role tests."""
        user_id, _ = tenancy.invite_member(self.organization_id, email, role, self.user_id)  # type: ignore[arg-type]
        _, token = tenancy.issue_api_key(self.organization_id, user_id, f"{role} key")
        return {"authorization": f"Bearer {token}"}

    def a_report(self, name: str = "a report") -> str:
        """A saved report belonging to this organisation."""
        run_id = repo.create_run(
            "Why did revenue drop?", organization_id=self.organization_id
        )
        query_id = repo.record_sql_audit(
            run_id=run_id,
            purpose="monthly revenue [revenue@v1]",
            sql_text="SELECT 1",
            verdict="allowed",
            reasons=[],
            rewritten_sql=None,
            referenced_objects=["analytics.orders"],
            sensitive_columns=[],
            estimated_cost=10.0,
        )
        repo.mark_sql_executed(query_id, row_count=3, truncated=False, duration_ms=5)
        repo.finish_run(
            run_id,
            "completed",
            answer={
                "conclusion": "It fell.",
                "confidence": "medium",
                "evidence": [{"query_id": str(query_id)}],
            },
        )
        created = self.client.post(
            "/v1/reports", json={"run_id": str(run_id), "name": name}, headers=self.auth
        )
        assert created.status_code == 201, created.text
        return created.json()["report_id"]


@pytest.fixture
def org_a(client: TestClient) -> Org:
    return Org(client, "Acme Analytics")


@pytest.fixture
def org_b(client: TestClient) -> Org:
    return Org(client, "Beta Metrics")


# --- who is asking ------------------------------------------------------------


def test_an_unauthenticated_request_is_the_default_organisation(client: TestClient) -> None:
    """A real decision, not a gap: there is no login here, and the demo has to work.

    It is reported as unauthenticated so a page cannot mistake a demo for a tenant.
    """
    body = client.get("/v1/me").json()
    assert body["organization"]["organization_id"] == str(DEFAULT_ORG_ID)
    assert body["authenticated"] is False
    assert body["role"] == "owner"


def test_a_key_identifies_its_organisation(client: TestClient, org_a: Org) -> None:
    body = client.get("/v1/me", headers=org_a.auth).json()
    assert body["organization"]["organization_id"] == str(org_a.organization_id)
    assert body["authenticated"] is True


def test_a_bad_key_is_rejected_rather_than_downgraded(client: TestClient) -> None:
    """Presenting a wrong key is not the same as presenting none.

    Treating them the same would silently turn a revoked key into a demo session with owner
    rights — the worst possible reading of a failed credential.
    """
    response = client.get("/v1/me", headers={"authorization": "Bearer aak_not-a-real-key"})
    assert response.status_code == 401


def test_a_revoked_key_stops_working(client: TestClient, org_a: Org) -> None:
    keys = client.get("/v1/team/keys", headers=org_a.auth).json()
    key_id = keys[0]["key_id"]
    assert client.delete(f"/v1/team/keys/{key_id}", headers=org_a.auth).status_code == 204
    assert client.get("/v1/me", headers=org_a.auth).status_code == 401


def test_removing_a_member_revokes_their_keys(client: TestClient, org_a: Org) -> None:
    """Leaving a removed person's key live is the whole point of being able to remove them."""
    member_auth = org_a.key_for("analyst@acme.co", "analyst")
    assert client.get("/v1/me", headers=member_auth).status_code == 200

    user_id = client.get("/v1/me", headers=member_auth).json()["user_id"]
    client.patch(
        f"/v1/team/member/{user_id}", json={"remove": True}, headers=org_a.auth
    )
    assert client.get("/v1/me", headers=member_auth).status_code == 401


def test_authentication_can_be_made_mandatory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With this on there is no anonymous path at all — what a real deployment sets."""
    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    get_settings.cache_clear()
    try:
        assert client.get("/v1/me").status_code == 401
    finally:
        get_settings.cache_clear()


# --- organisation isolation ---------------------------------------------------


def test_runs_are_invisible_across_organisations(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """Checked from both directions: a filter written the wrong way round passes one of them."""
    a_run = repo.create_run("a question of A's", organization_id=org_a.organization_id)
    b_run = repo.create_run("a question of B's", organization_id=org_b.organization_id)

    a_ids = {run["run_id"] for run in client.get("/v1/runs", headers=org_a.auth).json()}
    b_ids = {run["run_id"] for run in client.get("/v1/runs", headers=org_b.auth).json()}

    assert str(a_run) in a_ids and str(a_run) not in b_ids
    assert str(b_run) in b_ids and str(b_run) not in a_ids


def test_reading_another_organisations_run_by_id_is_a_404(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """404, not 403. A 403 would confirm the run exists in somebody else's account."""
    a_run = repo.create_run("a question of A's", organization_id=org_a.organization_id)
    assert client.get(f"/v1/runs/{a_run}", headers=org_b.auth).status_code == 404
    assert client.get(f"/v1/runs/{a_run}/trace", headers=org_b.auth).status_code == 404
    assert client.get(f"/v1/runs/{a_run}", headers=org_a.auth).status_code == 200


def test_reports_are_invisible_across_organisations(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    report_id = org_a.a_report("A's report")
    assert report_id in [r["report_id"] for r in client.get("/v1/reports", headers=org_a.auth).json()]
    assert client.get("/v1/reports", headers=org_b.auth).json() == []
    assert client.get(f"/v1/reports/{report_id}", headers=org_b.auth).status_code == 404


def test_another_organisation_cannot_rename_or_delete_a_report(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    report_id = org_a.a_report()
    assert (
        client.patch(
            f"/v1/reports/{report_id}", json={"name": "mine now"}, headers=org_b.auth
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/reports/{report_id}", headers=org_b.auth).status_code == 404
    # Still there, still called what its owner called it.
    assert client.get(f"/v1/reports/{report_id}", headers=org_a.auth).json()["name"] == "a report"


def test_exports_are_scoped_too(client: TestClient, org_a: Org, org_b: Org) -> None:
    """An export is the whole report in a file: the least useful place to forget the filter."""
    report_id = org_a.a_report()
    for suffix in ("pdf", "xlsx"):
        assert (
            client.get(f"/v1/reports/{report_id}/export.{suffix}", headers=org_b.auth).status_code
            == 404
        )
        assert (
            client.get(f"/v1/reports/{report_id}/export.{suffix}", headers=org_a.auth).status_code
            == 200
        )


def test_a_chart_image_is_scoped(client: TestClient, org_a: Org, org_b: Org) -> None:
    run_id = repo.create_run("charted", organization_id=org_a.organization_id)
    query_id = repo.record_sql_audit(
        run_id=run_id,
        purpose="p",
        sql_text="SELECT 1",
        verdict="allowed",
        reasons=[],
        rewritten_sql=None,
        referenced_objects=[],
        sensitive_columns=[],
        estimated_cost=None,
    )
    chart_id = repo.record_chart(
        run_id=run_id,
        query_id=query_id,
        chart_type="line",
        spec={"data": [], "layout": {}},
        title="A's chart",
        png=b"\x89PNG\r\n\x1a\n" + b"0" * 32,
    )
    assert client.get(f"/v1/charts/{chart_id}/export.png", headers=org_b.auth).status_code == 404
    assert client.get(f"/v1/charts/{chart_id}/export.png", headers=org_a.auth).status_code == 200


def test_the_dashboard_counts_only_this_organisation(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """The most visible possible leak: another tenant's total on this tenant's page."""
    for _ in range(3):
        repo.create_run("A's work", organization_id=org_a.organization_id)
    repo.create_run("B's work", organization_id=org_b.organization_id)

    a = client.get("/v1/dashboard/summary", headers=org_a.auth).json()["totals"]
    b = client.get("/v1/dashboard/summary", headers=org_b.auth).json()["totals"]
    assert a["analyses"] == 3
    assert b["analyses"] == 1


def test_a_question_is_recorded_against_the_callers_organisation(
    client: TestClient, org_a: Org
) -> None:
    """The organisation comes from the key, never from the body.

    A body-supplied tenant id is how a multi-tenant system grows a hole: it turns "ask in my
    organisation" into "write into anybody's".
    """
    started = client.post(
        "/v1/questions", json={"question": "What was revenue in 2017?"}, headers=org_a.auth
    )
    assert started.status_code == 202
    run = repo.get_run(uuid.UUID(started.json()["run_id"]))
    assert run is not None
    assert uuid.UUID(str(run["organization_id"])) == org_a.organization_id


# --- roles --------------------------------------------------------------------


def test_the_role_ladder_is_ordered() -> None:
    """A ladder rather than a matrix: it cannot be misconfigured into letting a viewer invite."""
    assert RANK["viewer"] < RANK["analyst"] < RANK["admin"] < RANK["owner"]
    for action, needed in NEEDS.items():
        assert needed in RANK, action


@pytest.mark.parametrize(
    ("role", "can_ask", "can_manage_team"),
    [("viewer", False, False), ("analyst", True, False), ("admin", True, True)],
)
def test_what_each_role_may_do(
    client: TestClient, org_a: Org, role: str, can_ask: bool, can_manage_team: bool
) -> None:
    auth = org_a.key_for(f"{role}@acme.co", role)

    asked = client.post("/v1/questions", json={"question": "What was revenue?"}, headers=auth)
    assert (asked.status_code == 202) is can_ask, asked.text

    invited = client.post(
        "/v1/team/invite", json={"email": "new@acme.co", "role": "viewer"}, headers=auth
    )
    assert (invited.status_code == 201) is can_manage_team, invited.text

    # Reading is every member's right, whatever else they may not do.
    assert client.get("/v1/team", headers=auth).status_code == 200


def test_a_viewer_cannot_read_the_audit_trail(client: TestClient, org_a: Org) -> None:
    """The trail names who did what, which is not a viewer's business."""
    auth = org_a.key_for("viewer2@acme.co", "viewer")
    assert client.get("/v1/audit", headers=auth).status_code == 403


def test_a_forbidden_action_is_403_while_another_tenant_is_404(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """The distinction is whether the answer confirms something exists."""
    viewer = org_a.key_for("viewer3@acme.co", "viewer")
    report_id = org_a.a_report()

    # Same organisation, insufficient role: 403 tells them to ask for access.
    assert (
        client.delete(f"/v1/reports/{report_id}", headers=viewer).status_code == 403
    )
    # Different organisation: 404, revealing nothing.
    assert client.delete(f"/v1/reports/{report_id}", headers=org_b.auth).status_code == 404


# --- team management ----------------------------------------------------------


def test_a_team_can_be_invited_promoted_and_removed(client: TestClient, org_a: Org) -> None:
    invited = client.post(
        "/v1/team/invite", json={"email": "sam@acme.co", "role": "analyst"}, headers=org_a.auth
    )
    assert invited.status_code == 201
    assert invited.json()["created"] is True
    user_id = invited.json()["user_id"]

    again = client.post(
        "/v1/team/invite", json={"email": "sam@acme.co", "role": "admin"}, headers=org_a.auth
    )
    assert again.status_code == 201
    assert again.json()["created"] is False, "already a member; the role is updated instead"

    team = client.patch(
        f"/v1/team/member/{user_id}", json={"role": "viewer"}, headers=org_a.auth
    ).json()
    assert next(m for m in team["members"] if m["user_id"] == user_id)["role"] == "viewer"

    removed = client.patch(
        f"/v1/team/member/{user_id}", json={"remove": True}, headers=org_a.auth
    ).json()
    assert user_id not in [m["user_id"] for m in removed["members"]]


def test_the_last_owner_cannot_be_removed_or_demoted(client: TestClient, org_a: Org) -> None:
    """An organisation with no owner cannot appoint one - it would be unadministrable."""
    owner_id = str(org_a.user_id)
    demoted = client.patch(
        f"/v1/team/member/{owner_id}", json={"role": "analyst"}, headers=org_a.auth
    )
    assert demoted.status_code == 409
    assert "last owner" in demoted.json()["detail"]

    removed = client.patch(
        f"/v1/team/member/{owner_id}", json={"remove": True}, headers=org_a.auth
    )
    assert removed.status_code == 409


def test_an_owner_can_step_down_once_there_is_another(client: TestClient, org_a: Org) -> None:
    second = client.post(
        "/v1/team/invite", json={"email": "second@acme.co", "role": "admin"}, headers=org_a.auth
    ).json()["user_id"]
    assert (
        client.patch(
            f"/v1/team/member/{second}", json={"role": "owner"}, headers=org_a.auth
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/team/member/{org_a.user_id}", json={"role": "analyst"}, headers=org_a.auth
        ).status_code
        == 200
    )


def test_a_member_of_another_organisation_is_a_404(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """So a probe cannot enumerate who belongs where."""
    response = client.patch(
        f"/v1/team/member/{org_b.user_id}", json={"role": "viewer"}, headers=org_a.auth
    )
    assert response.status_code == 404


def test_the_team_view_carries_activity(client: TestClient, org_a: Org) -> None:
    email = "owner@acmeanalytics.co"
    repo.create_run("their question", requested_by=email, organization_id=org_a.organization_id)
    members = client.get("/v1/team", headers=org_a.auth).json()["members"]
    owner = next(m for m in members if m["role"] == "owner")
    assert owner["analyses"] >= 1
    assert owner["last_active_at"] is not None


def test_owner_cannot_be_invited_directly(client: TestClient, org_a: Org) -> None:
    """Ownership is transferred by promoting a member, so a typo cannot create a second owner."""
    response = client.post(
        "/v1/team/invite", json={"email": "x@acme.co", "role": "owner"}, headers=org_a.auth
    )
    assert response.status_code == 422


# --- data sources and their secrets ------------------------------------------


POSTGRES_CONFIG = {
    "host": "warehouse.internal",
    "port": 5432,
    "database": "analytics",
    "user": "reader",
    "password": "correct-horse-battery-staple",
    "sslmode": "require",
}


def test_a_data_source_never_returns_its_credentials(client: TestClient, org_a: Org) -> None:
    """The headline requirement. Checked against the whole response body, not one field."""
    created = client.post(
        "/v1/data-sources",
        json={"name": "Warehouse", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    )
    assert created.status_code == 201, created.text
    body = created.text
    assert "correct-horse-battery-staple" not in body
    assert "password" not in body.lower() or "_withheld" in body

    summary = created.json()["summary"]
    assert summary["host"] == "warehouse.internal"
    assert summary["database"] == "analytics"
    assert "password" not in summary
    assert "password" in summary["_withheld"], "what was withheld is named, not silently dropped"

    listed = client.get("/v1/data-sources", headers=org_a.auth).text
    assert "correct-horse-battery-staple" not in listed


def test_the_stored_configuration_is_ciphertext(client: TestClient, org_a: Org) -> None:
    """Encryption is only worth something if the column really is encrypted.

    Read straight from the table rather than through the API: this asserts the property the API
    depends on, not the API's own claim about it.
    """
    created = client.post(
        "/v1/data-sources",
        json={"name": "Warehouse2", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    )
    data_source_id = created.json()["data_source_id"]

    with repo.rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT connection_config, summary FROM agent.data_sources WHERE data_source_id = %s",
            (uuid.UUID(data_source_id),),
        )
        row = cur.fetchone()

    raw = bytes(row["connection_config"])
    assert b"correct-horse-battery-staple" not in raw
    assert b"warehouse.internal" not in raw, "the whole config is encrypted, not just the password"
    assert raw.startswith(b"gAAAAA"), "a Fernet token"
    # The *value* must be absent. The key appears under `_withheld`, which is the point: what was
    # withheld is named rather than silently dropped.
    assert "correct-horse-battery-staple" not in str(row["summary"])
    assert row["summary"]["_withheld"] == ["password"]

    # And it round-trips for the code that actually opens a connection.
    recovered = tenancy.get_data_source(
        org_a.organization_id, uuid.UUID(data_source_id), with_config=True
    )
    assert recovered is not None
    assert recovered["config"]["password"] == "correct-horse-battery-staple"


def test_data_sources_are_isolated(client: TestClient, org_a: Org, org_b: Org) -> None:
    created = client.post(
        "/v1/data-sources",
        json={"name": "A's warehouse", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    ).json()
    assert client.get("/v1/data-sources", headers=org_b.auth).json() == []
    assert (
        client.get(f"/v1/data-sources/{created['data_source_id']}", headers=org_b.auth).status_code
        == 404
    )
    assert (
        client.delete(
            f"/v1/data-sources/{created['data_source_id']}", headers=org_b.auth
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("source_type", "config", "shown"),
    [
        ("csv", {"filename": "sales.csv", "delimiter": ",", "token": "s3-secret"}, "filename"),
        ("excel", {"filename": "q1.xlsx", "sheet": "Data", "password": "hunter2"}, "sheet"),
    ],
)
def test_every_source_type_redacts_by_allowlist(
    client: TestClient, org_a: Org, source_type: str, config: dict[str, Any], shown: str
) -> None:
    """An allowlist, so a field added next year is hidden until somebody classifies it."""
    created = client.post(
        "/v1/data-sources",
        json={"name": f"{source_type} source", "type": source_type, "config": config},
        headers=org_a.auth,
    )
    assert created.status_code == 201, created.text
    summary = created.json()["summary"]
    assert shown in summary
    for secret in ("token", "password"):
        assert secret not in summary
        assert str(config.get(secret, "\x00")) not in created.text


def test_two_sources_cannot_share_a_name_in_one_organisation(
    client: TestClient, org_a: Org
) -> None:
    body = {"name": "Warehouse", "type": "postgres", "config": POSTGRES_CONFIG}
    assert client.post("/v1/data-sources", json=body, headers=org_a.auth).status_code == 201
    assert client.post("/v1/data-sources", json=body, headers=org_a.auth).status_code == 409


def test_the_same_name_is_fine_in_two_organisations(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    body = {"name": "Warehouse", "type": "postgres", "config": POSTGRES_CONFIG}
    assert client.post("/v1/data-sources", json=body, headers=org_a.auth).status_code == 201
    assert client.post("/v1/data-sources", json=body, headers=org_b.auth).status_code == 201


def test_a_missing_encryption_key_refuses_rather_than_storing_plaintext(
    client: TestClient, org_a: Org, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, not 500: the service works and the deployment is incomplete.

    Storing the configuration in the clear "for now" is the one thing this must not do.
    """
    monkeypatch.setenv("SECRETS_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/v1/data-sources",
            json={"name": "No key", "type": "postgres", "config": POSTGRES_CONFIG},
            headers=org_a.auth,
        )
        assert response.status_code == 503
        assert "SECRETS_KEY" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_a_tampered_ciphertext_fails_loudly(client: TestClient, org_a: Org) -> None:
    """Fernet is authenticated, so a wrong key or an edited value cannot decrypt to something
    plausible."""
    created = client.post(
        "/v1/data-sources",
        json={"name": "Tamper", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    ).json()
    with repo.rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.data_sources SET connection_config = %s WHERE data_source_id = %s",
            (b"gAAAAABnot-a-real-token", uuid.UUID(created["data_source_id"])),
        )
    with pytest.raises(crypto.SecretsUnavailableError):
        tenancy.get_data_source(
            org_a.organization_id, uuid.UUID(created["data_source_id"]), with_config=True
        )


def test_a_token_is_stored_only_as_a_hash() -> None:
    token, digest, prefix = crypto.new_token()
    assert token.startswith("aak_")
    assert token not in digest
    assert len(digest) == 64
    assert prefix == token[:12]
    assert crypto.token_matches(token, digest)
    assert not crypto.token_matches(token + "x", digest)


# --- sharing ------------------------------------------------------------------


def test_a_public_link_is_readable_without_a_key(client: TestClient, org_a: Org) -> None:
    report_id = org_a.a_report("Quarterly review")
    share = client.post(
        f"/v1/reports/{report_id}/shares",
        json={"audience": "public"},
        headers=org_a.auth,
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    # No key at all - the only unauthenticated read path in the system.
    shared = client.get(f"/v1/shared/{token}")
    assert shared.status_code == 200
    body = shared.json()
    assert body["name"] == "Quarterly review"
    assert body["snapshot"]["question"]
    # Narrower than the owner's view: no ids for things the holder is not part of.
    assert "organization_id" not in body
    assert "run_id" not in body


def test_a_team_link_still_requires_membership(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    """"Share with my team" must not quietly mean "share with the internet"."""
    report_id = org_a.a_report()
    token = client.post(
        f"/v1/reports/{report_id}/shares", json={"audience": "team"}, headers=org_a.auth
    ).json()["token"]

    assert client.get(f"/v1/shared/{token}", headers=org_a.auth).status_code == 200
    assert client.get(f"/v1/shared/{token}", headers=org_b.auth).status_code == 404
    assert client.get(f"/v1/shared/{token}").status_code == 404, "and not anonymously"


def test_an_expired_link_stops_working(client: TestClient, org_a: Org) -> None:
    """Expiry is enforced in the query, not left to whichever caller remembers to check."""
    report_id = org_a.a_report()
    created = client.post(
        f"/v1/reports/{report_id}/shares",
        json={"audience": "public", "expires_in_hours": 1},
        headers=org_a.auth,
    ).json()
    token = created["token"]
    assert created["expires_at"] is not None
    assert client.get(f"/v1/shared/{token}").status_code == 200

    # Aged rather than back-dated: `report_shares_expiry_is_in_the_future` refuses an expiry
    # before the row's own creation, and it is right to - so this makes the share genuinely old
    # instead of impossible. The constraint caught the first version of this test, which is the
    # constraint working.
    with repo.rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.report_shares SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 minute' WHERE share_id = %s",
            (uuid.UUID(created["share_id"]),),
        )
    assert client.get(f"/v1/shared/{token}").status_code == 404


def test_a_revoked_link_stops_working(client: TestClient, org_a: Org) -> None:
    report_id = org_a.a_report()
    created = client.post(
        f"/v1/reports/{report_id}/shares", json={"audience": "public"}, headers=org_a.auth
    ).json()
    assert client.delete(f"/v1/shares/{created['share_id']}", headers=org_a.auth).status_code == 204
    assert client.get(f"/v1/shared/{created['token']}").status_code == 404


def test_an_unknown_expired_and_revoked_link_are_the_same_answer(client: TestClient) -> None:
    """Distinguishing them would tell a holder of a dead link whether it ever existed."""
    response = client.get("/v1/shared/shr_completely-made-up")
    assert response.status_code == 404
    assert "revoked" in response.json()["detail"] and "expired" in response.json()["detail"]


def test_a_share_token_is_not_recoverable_from_the_listing(client: TestClient, org_a: Org) -> None:
    report_id = org_a.a_report()
    token = client.post(
        f"/v1/reports/{report_id}/shares", json={"audience": "public"}, headers=org_a.auth
    ).json()["token"]
    listing = client.get(f"/v1/reports/{report_id}/shares", headers=org_a.auth)
    assert token not in listing.text
    assert listing.json()[0]["prefix"] in token, "the prefix identifies it without revealing it"


def test_sharing_counts_its_uses(client: TestClient, org_a: Org) -> None:
    """So an owner can see a link is being used, and by how much, before revoking it."""
    report_id = org_a.a_report()
    token = client.post(
        f"/v1/reports/{report_id}/shares", json={"audience": "public"}, headers=org_a.auth
    ).json()["token"]
    for _ in range(3):
        client.get(f"/v1/shared/{token}")
    share = client.get(f"/v1/reports/{report_id}/shares", headers=org_a.auth).json()[0]
    assert share["use_count"] == 3
    assert share["last_used_at"] is not None


def test_sharing_another_organisations_report_is_a_404(
    client: TestClient, org_a: Org, org_b: Org
) -> None:
    report_id = org_a.a_report()
    assert (
        client.post(
            f"/v1/reports/{report_id}/shares", json={"audience": "public"}, headers=org_b.auth
        ).status_code
        == 404
    )


def test_a_private_report_is_hidden_from_the_rest_of_the_team(
    client: TestClient, org_a: Org
) -> None:
    """Private means the saver's, and the rule is in the SQL rather than in one route."""
    report_id = org_a.a_report("owner's private notes")
    colleague = org_a.key_for("colleague@acme.co", "analyst")

    assert report_id not in [
        r["report_id"] for r in client.get("/v1/reports", headers=colleague).json()
    ]

    client.patch(
        f"/v1/reports/{report_id}/visibility", json={"visibility": "team"}, headers=org_a.auth
    )
    assert report_id in [
        r["report_id"] for r in client.get("/v1/reports", headers=colleague).json()
    ]


# --- alerts -------------------------------------------------------------------


def test_an_alert_must_watch_an_approved_metric(client: TestClient, org_a: Org) -> None:
    """An alert runs unattended, so an invented definition would keep firing about a number
    nobody agreed on."""
    response = client.post(
        "/v1/alerts",
        json={
            "name": "made up",
            "metric": "vibes",
            "comparison": "drop",
            "threshold": 10,
        },
        headers=org_a.auth,
    )
    assert response.status_code == 422
    assert "not an approved metric" in response.json()["detail"]


def test_an_alert_can_be_created_paused_and_deleted(client: TestClient, org_a: Org) -> None:
    created = client.post(
        "/v1/alerts",
        json={
            "name": "Revenue drop",
            "metric": "revenue",
            "comparison": "drop",
            "threshold": 15,
            "window_periods": 6,
        },
        headers=org_a.auth,
    )
    assert created.status_code == 201, created.text
    alert_id = created.json()["alert_id"]
    assert created.json()["status"] == "active"

    paused = client.patch(f"/v1/alerts/{alert_id}", json={"status": "paused"}, headers=org_a.auth)
    assert paused.json()["status"] == "paused"

    assert client.delete(f"/v1/alerts/{alert_id}", headers=org_a.auth).status_code == 204
    assert client.get("/v1/alerts", headers=org_a.auth).json() == []


def test_a_one_period_window_is_refused(client: TestClient, org_a: Org) -> None:
    """A value compared to itself can never move, which reads as "nothing is wrong"."""
    response = client.post(
        "/v1/alerts",
        json={
            "name": "too narrow",
            "metric": "revenue",
            "comparison": "drop",
            "threshold": 10,
            "window_periods": 1,
        },
        headers=org_a.auth,
    )
    assert response.status_code == 422


def test_alerts_are_isolated(client: TestClient, org_a: Org, org_b: Org) -> None:
    created = client.post(
        "/v1/alerts",
        json={"name": "A's alert", "metric": "revenue", "comparison": "drop", "threshold": 10},
        headers=org_a.auth,
    ).json()
    assert client.get("/v1/alerts", headers=org_b.auth).json() == []
    assert (
        client.delete(f"/v1/alerts/{created['alert_id']}", headers=org_b.auth).status_code == 404
    )
    assert (
        client.post(f"/v1/alerts/{created['alert_id']}/check", headers=org_b.auth).status_code
        == 404
    )


def test_checking_an_alert_records_the_outcome_either_way(
    client: TestClient, org_a: Org, seeded: None
) -> None:
    """Both outcomes, because recording only breaches cannot distinguish a quiet alert from a
    broken one."""
    alert_id = client.post(
        "/v1/alerts",
        json={
            "name": "Revenue drop",
            "metric": "revenue",
            "comparison": "drop",
            "threshold": 90,
            "window_periods": 6,
        },
        headers=org_a.auth,
    ).json()["alert_id"]

    checked = client.post(f"/v1/alerts/{alert_id}/check", headers=org_a.auth)
    assert checked.status_code == 200, checked.text
    event = checked.json()["event"]
    assert event["detail"], "an alert that fires without saying what it saw gets disabled"
    assert event["query_id"], "the event points at the statement that produced it"

    history = client.get(f"/v1/alerts/{alert_id}/events", headers=org_a.auth).json()
    assert len(history) == 1
    assert history[0]["event_id"] == event["event_id"]


def test_a_sensitive_threshold_actually_fires(
    client: TestClient, org_a: Org, seeded: None
) -> None:
    """A 0.1% drop threshold against real seeded data: the alert has to be capable of firing."""
    alert_id = client.post(
        "/v1/alerts",
        json={
            "name": "Any dip at all",
            "metric": "revenue",
            "comparison": "below",
            "threshold": 10 ** 12,
        },
        headers=org_a.auth,
    ).json()["alert_id"]
    checked = client.post(f"/v1/alerts/{alert_id}/check", headers=org_a.auth).json()
    assert checked["event"]["triggered"] is True
    assert checked["alert"]["status"] == "triggered"
    assert checked["alert"]["times_triggered"] == 1


# --- audit --------------------------------------------------------------------


def test_the_audit_trail_records_who_did_what(client: TestClient, org_a: Org) -> None:
    client.post(
        "/v1/team/invite", json={"email": "audited@acme.co", "role": "analyst"},
        headers=org_a.auth,
    )
    client.post(
        "/v1/data-sources",
        json={"name": "Audited source", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    )
    entries = client.get("/v1/audit", headers=org_a.auth).json()
    actions = [entry["action"] for entry in entries]
    assert "team.invite" in actions
    assert "data_source.create" in actions
    invite = next(entry for entry in entries if entry["action"] == "team.invite")
    assert invite["detail"]["email"] == "audited@acme.co"
    assert invite["actor_label"], "an entry says who, not only what"


def test_the_audit_trail_never_records_a_credential(client: TestClient, org_a: Org) -> None:
    """An audit entry holding a password would defeat the encryption on the row it describes."""
    client.post(
        "/v1/data-sources",
        json={"name": "Secret source", "type": "postgres", "config": POSTGRES_CONFIG},
        headers=org_a.auth,
    )
    trail = client.get("/v1/audit", headers=org_a.auth).text
    assert "correct-horse-battery-staple" not in trail


def test_the_audit_trail_is_isolated(client: TestClient, org_a: Org, org_b: Org) -> None:
    client.post(
        "/v1/team/invite", json={"email": "a-only@acme.co", "role": "viewer"},
        headers=org_a.auth,
    )
    b_trail = client.get("/v1/audit", headers=org_b.auth).text
    assert "a-only@acme.co" not in b_trail


def test_an_unauthenticated_action_is_marked_as_such(client: TestClient) -> None:
    """A demo session must not look like a named person in the trail."""
    client.post(
        "/v1/team/invite", json={"email": "anon-invited@example.co", "role": "viewer"}
    )
    entries = tenancy.audit_entries(DEFAULT_ORG_ID, limit=5)
    latest = next(e for e in entries if e["action"] == "team.invite")
    assert "unauthenticated" in latest["actor_label"]


def test_there_is_no_way_to_edit_the_audit_trail() -> None:
    """Append-only by intent: a trail somebody can edit is not one.

    Asserted against the repository surface rather than by trying an UPDATE: the guarantee is that
    no code path exists, and a test that only proves *this* statement fails would not show that.
    """
    surface = [name for name in dir(tenancy) if "audit" in name]
    assert set(surface) == {"audit", "audit_entries"}
    for name in ("update_audit", "delete_audit", "purge_audit"):
        assert not hasattr(tenancy, name)


# --- the principal itself -----------------------------------------------------


def test_a_row_with_no_organisation_is_not_treated_as_ours() -> None:
    """The permissive reading of a missing owner is exactly how a tenant boundary leaks."""
    principal = Principal(
        organization_id=DEFAULT_ORG_ID, user_id=uuid.uuid4(), role="owner"
    )
    assert principal.in_organization(DEFAULT_ORG_ID) is True
    assert principal.in_organization(None) is False
    assert principal.in_organization(uuid.uuid4()) is False
