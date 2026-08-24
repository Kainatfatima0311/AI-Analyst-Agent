"""The SQL guard's security regression net.

This is the most important test file in the project. It runs with **no database**: the guard
takes a committed catalog snapshot, so the whole hostile-query suite runs anywhere, including
in CI without a service container. Any failure here is a security regression, and CI treats it
as a required gate.

Three expectations, kept apart because they mean different things:

* ``rejected``  - the query must never reach the database.
* ``escalated`` - the query is structurally fine but a human must see it first.
* ``allowed``   - ordinary analysis must not be blocked; a guard that refuses real work would
                  simply be turned off.
"""

from __future__ import annotations

import pytest

from analyst_agent.config import Settings
from analyst_agent.sql_guard import STATIC_CATALOG, validate

DUMMY_DSN = "postgresql://u:p@localhost:5432/db"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None, db_rw_dsn=DUMMY_DSN, db_ro_dsn=DUMMY_DSN)


def guard(sql: str, settings: Settings, **kwargs: object):
    return validate(sql, catalog=STATIC_CATALOG, settings=settings, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Must be REJECTED. (sql, expected_reason_code)
# ---------------------------------------------------------------------------

HOSTILE: list[tuple[str, str]] = [
    # --- statement stacking: the classic injection shape ------------------
    ("SELECT 1; DROP TABLE analytics.orders", "multiple_statements"),
    ("SELECT * FROM analytics.orders; DELETE FROM analytics.orders", "multiple_statements"),
    ("SELECT 1;SELECT 2", "multiple_statements"),
    ("SELECT 1 ; ; DROP TABLE analytics.orders", "multiple_statements"),
    (
        "SELECT count(*) FROM analytics.orders;\nUPDATE analytics.orders SET order_status='x'",
        "multiple_statements",
    ),
    # --- DML at the root --------------------------------------------------
    ("DELETE FROM analytics.orders", "not_a_select"),
    ("UPDATE analytics.orders SET order_status = 'tampered'", "not_a_select"),
    ("INSERT INTO analytics.orders (order_id) VALUES ('x')", "not_a_select"),
    ("UPDATE analytics.orders SET order_status='x' RETURNING order_id", "not_a_select"),
    ("DELETE FROM analytics.reviews WHERE review_score < 3 RETURNING *", "not_a_select"),
    # --- DML hidden inside a CTE -----------------------------------------
    # These are the cases a root-type check alone would clear: sqlglot parses all of them
    # with a Select at the root.
    ("WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x", "dml_delete"),
    (
        "WITH x AS (UPDATE analytics.orders SET order_status='x' RETURNING *) SELECT * FROM x",
        "dml_update",
    ),
    (
        "WITH x AS (INSERT INTO analytics.orders (order_id) VALUES ('x') RETURNING *) "
        "SELECT * FROM x",
        "dml_insert",
    ),
    (
        "WITH a AS (SELECT 1 AS n), b AS (DELETE FROM analytics.reviews RETURNING review_id) "
        "SELECT * FROM a JOIN b ON true",
        "dml_delete",
    ),
    (
        "SELECT * FROM analytics.orders WHERE order_id IN "
        "(WITH d AS (DELETE FROM analytics.reviews RETURNING order_id) SELECT order_id FROM d)",
        "dml_delete",
    ),
    # --- DDL --------------------------------------------------------------
    ("DROP TABLE analytics.orders", "not_a_select"),
    ("CREATE TABLE analytics.evil (id int)", "not_a_select"),
    ("ALTER TABLE analytics.orders ADD COLUMN evil text", "not_a_select"),
    ("TRUNCATE analytics.reviews", "not_a_select"),
    ("CREATE VIEW analytics.evil AS SELECT * FROM analytics.orders", "not_a_select"),
    ("CREATE INDEX evil ON analytics.orders (order_id)", "not_a_select"),
    ("DROP SCHEMA analytics CASCADE", "not_a_select"),
    ("COMMENT ON TABLE analytics.orders IS 'owned'", "not_a_select"),
    # --- privileges, session state, and non-SELECT commands ---------------
    ("GRANT ALL ON analytics.orders TO PUBLIC", "not_a_select"),
    ("COPY analytics.customers FROM '/etc/passwd'", "not_a_select"),
    ("COPY (SELECT * FROM analytics.orders) TO '/tmp/leak.csv'", "not_a_select"),
    ("SET statement_timeout = 0", "not_a_select"),
    ("RESET ALL", "not_a_select"),
    ("CALL some_procedure()", "not_a_select"),
    ("DO $$ BEGIN PERFORM 1; END $$", "not_a_select"),
    ("VACUUM FULL analytics.orders", "not_a_select"),
    ("BEGIN", "not_a_select"),
    # --- SELECT-shaped but not a read ------------------------------------
    ("SELECT * INTO evil FROM analytics.orders", "select_into"),
    ("SELECT * FROM analytics.orders FOR UPDATE", "row_lock"),
    ("SELECT order_id FROM analytics.orders FOR NO KEY UPDATE", "row_lock"),
    # --- dangerous functions: all of these parse as exp.Anonymous ---------
    ("SELECT pg_read_file('/etc/passwd')", "fn_file_read"),
    ("SELECT pg_read_binary_file('/etc/shadow')", "fn_file_read"),
    ("SELECT pg_ls_dir('/var/lib/postgresql')", "fn_file_read"),
    ("SELECT lo_import('/etc/passwd')", "fn_large_object"),
    ("SELECT lo_export(1, '/tmp/leak')", "fn_large_object"),
    ("SELECT dblink('host=evil.example', 'SELECT 1')", "fn_cross_database"),
    ("SELECT dblink_exec('host=evil', 'DROP TABLE x')", "fn_cross_database"),
    ("SELECT pg_sleep(300)", "fn_sleep"),
    ("SELECT pg_terminate_backend(pid) FROM analytics.orders", "fn_session_control"),
    ("SELECT set_config('statement_timeout', '0', false)", "fn_session_control"),
    ("SELECT setval('some_seq', 1)", "fn_sequence_write"),
    ("SELECT nextval('some_seq')", "fn_sequence_write"),
    ("SELECT query_to_xml('SELECT * FROM analytics.customer_contact', true, true, '')",
     "fn_dynamic_sql"),
    # buried deep rather than at the top level
    (
        "SELECT o.order_id FROM analytics.orders o WHERE o.order_status = "
        "(SELECT pg_read_file('/etc/passwd'))",
        "fn_file_read",
    ),
    (
        "WITH s AS (SELECT pg_sleep(60) AS z) SELECT count(*) FROM analytics.orders, s",
        "fn_sleep",
    ),
    ("SELECT count(*) FROM analytics.orders HAVING count(*) > (SELECT nextval('s'))",
     "fn_sequence_write"),
    # --- catalog and forbidden schemas -----------------------------------
    ("SELECT rolname, rolpassword FROM pg_catalog.pg_authid", "forbidden_schema"),
    ("SELECT * FROM pg_authid", "forbidden_schema"),
    ("SELECT * FROM pg_shadow", "forbidden_schema"),
    ("SELECT table_name FROM information_schema.tables", "forbidden_schema"),
    ("SELECT column_name FROM information_schema.columns WHERE table_schema='analytics'",
     "forbidden_schema"),
    ("SELECT * FROM pg_catalog.pg_settings", "forbidden_schema"),
    ("SELECT * FROM agent.sql_audit", "forbidden_schema"),
    ("SELECT * FROM agent.runs", "forbidden_schema"),
    (
        "SELECT o.order_id FROM analytics.orders o "
        "JOIN information_schema.tables t ON t.table_name = 'orders'",
        "forbidden_schema",
    ),
    (
        "WITH leak AS (SELECT rolname FROM pg_catalog.pg_authid) SELECT * FROM leak",
        "forbidden_schema",
    ),
    # --- objects outside the allowlist -----------------------------------
    ("SELECT * FROM public.secrets", "schema_not_allowed"),
    ("SELECT * FROM other_schema.orders", "schema_not_allowed"),
    ("SELECT * FROM analytics.no_such_table", "unknown_object"),
    ("SELECT * FROM no_such_table", "unknown_object"),
    ("SELECT * FROM remote_db.analytics.orders", "cross_database_reference"),
    # --- unbounded cartesian products ------------------------------------
    ("SELECT * FROM analytics.orders o, analytics.order_items oi", "unbounded_cross_join"),
    ("SELECT * FROM analytics.orders CROSS JOIN analytics.customers", "unbounded_cross_join"),
    (
        "SELECT count(*) FROM analytics.orders o JOIN analytics.products p ON true "
        "CROSS JOIN analytics.sellers",
        "unbounded_cross_join",
    ),
    # --- unparseable or empty --------------------------------------------
    ("", "empty_statement"),
    ("   \n  ", "empty_statement"),
    ("SELECT FROM WHERE", "parse_error"),
    ("this is not sql at all", "parse_error"),
    # --- comment and casing tricks: none of these change the parse -------
    ("SELECT 1 /* harmless */; DROP TABLE analytics.orders", "multiple_statements"),
    ("sElEcT * FrOm InFoRmAtIoN_ScHeMa.TaBlEs", "forbidden_schema"),
    ("DeLeTe FrOm analytics.orders", "not_a_select"),
    ("/* leading comment */ DROP TABLE analytics.orders", "not_a_select"),
    (
        "SELECT * FROM analytics.orders WHERE order_id = '' OR 1=1; DROP TABLE analytics.orders",
        "multiple_statements",
    ),
    # --- the injection actually planted in the seeded review text --------
    (
        # Stacking is caught before the statement type is even considered, which is the right
        # order: two statements is already disqualifying.
        "DROP TABLE analytics.orders; SELECT * FROM analytics.customer_contact",
        "multiple_statements",
    ),
]


# ---------------------------------------------------------------------------
# Must be ESCALATED: structurally fine, but a human decides first.
# ---------------------------------------------------------------------------

ESCALATING: list[tuple[str, str]] = [
    # direct identifiers - restricted anywhere outside an approved aggregate
    ("SELECT email FROM analytics.customer_contact", "sensitive_column"),
    ("SELECT full_name, phone FROM analytics.customer_contact", "sensitive_column"),
    ("SELECT * FROM analytics.customer_contact", "sensitive_column"),
    ("SELECT cc.* FROM analytics.customer_contact cc", "sensitive_column"),
    # filtering by a direct identifier is a person-level lookup, not analysis
    (
        "SELECT count(*) FROM analytics.customer_contact WHERE email = 'someone@example.com'",
        "sensitive_column",
    ),
    (
        "SELECT count(*) FROM analytics.customer_contact WHERE street_address LIKE 'Rua 1%'",
        "sensitive_column",
    ),
    # wrapping a restricted column in a scalar function does not inherit count()'s permission
    ("SELECT count(DISTINCT lower(email)) FROM analytics.customer_contact", "sensitive_column"),
    ("SELECT max(email) FROM analytics.customer_contact", "sensitive_column"),
    # a direct identifier exposed through a CTE is still exposed
    (
        "WITH c AS (SELECT * FROM analytics.customer_contact) SELECT count(*) FROM c",
        "sensitive_column",
    ),
    # pseudonymous key: projecting it produces one row per person
    ("SELECT customer_unique_id, count(*) FROM analytics.customers GROUP BY 1", "sensitive_column"),
    ("SELECT * FROM analytics.customers", "sensitive_column"),
    # precise coordinates: returning them is a disclosure, and min/max returns a real point
    ("SELECT geolocation_lat, geolocation_lng FROM analytics.geolocation", "sensitive_column"),
    (
        "SELECT min(geolocation_lat), max(geolocation_lng) FROM analytics.geolocation",
        "sensitive_column",
    ),
    # unqualified reference, resolved through the catalog rather than ignored
    ("SELECT email FROM analytics.customer_contact cc", "sensitive_column"),
]


# ---------------------------------------------------------------------------
# Must be ALLOWED: ordinary analysis. A guard that blocks real work gets switched off.
# ---------------------------------------------------------------------------

LEGITIMATE: list[str] = [
    "SELECT count(*) FROM analytics.orders",
    "SELECT year_month, sum(item_revenue) AS revenue FROM analytics.v_order_revenue "
    "GROUP BY 1 ORDER BY 1",
    "SELECT order_status, count(*) FROM analytics.orders GROUP BY 1 ORDER BY 2 DESC",
    # revenue by month excluding cancellations - the approved revenue metric's shape
    "SELECT to_char(o.order_purchase_timestamp, 'YYYY-MM') AS ym, sum(oi.price) AS revenue "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    "WHERE o.order_status <> 'canceled' GROUP BY 1 ORDER BY 1",
    # category mix, one of the planted causes of the seeded shock month
    "SELECT p.product_category_name, sum(oi.price) AS revenue "
    "FROM analytics.order_items oi JOIN analytics.products p ON p.product_id = oi.product_id "
    "JOIN analytics.orders o ON o.order_id = oi.order_id "
    "WHERE to_char(o.order_purchase_timestamp, 'YYYY-MM') = '2018-03' "
    "GROUP BY 1 ORDER BY 2 DESC",
    # delivery lateness by seller state, the other planted cause
    "SELECT s.seller_state, avg(CASE WHEN o.order_delivered_customer_date > "
    "o.order_estimated_delivery_date THEN 1.0 ELSE 0.0 END) AS late_rate "
    "FROM analytics.orders o JOIN analytics.order_items oi ON oi.order_id = o.order_id "
    "JOIN analytics.sellers s ON s.seller_id = oi.seller_id GROUP BY 1",
    # aggregates over restricted columns are exactly what the policy permits
    "SELECT count(DISTINCT email) FROM analytics.customer_contact",
    "SELECT count(DISTINCT customer_unique_id) AS people FROM analytics.customers",
    "SELECT avg(geolocation_lat) FROM analytics.geolocation",
    "SELECT geolocation_state, count(*) FROM analytics.geolocation GROUP BY 1",
    # grouping and joining on the pseudonymous key without projecting it - the approved
    # repeat-customer metric depends on this being allowed
    "SELECT count(*) FROM (SELECT customer_unique_id FROM analytics.customers "
    "GROUP BY 1 HAVING count(*) > 1) AS repeat_buyers",
    "SELECT count(DISTINCT c.customer_unique_id) FROM analytics.customers c "
    "JOIN analytics.orders o ON o.customer_id = c.customer_id",
    # CTEs, window functions, set operations, period-over-period
    "WITH monthly AS (SELECT year_month, sum(item_revenue) AS rev "
    "FROM analytics.v_order_revenue GROUP BY 1) "
    "SELECT year_month, rev, lag(rev) OVER (ORDER BY year_month) AS prev FROM monthly",
    "SELECT 'delivered' AS bucket, count(*) FROM analytics.orders WHERE order_status='delivered' "
    "UNION ALL SELECT 'other', count(*) FROM analytics.orders WHERE order_status<>'delivered'",
    "SELECT d.year_month, count(o.order_id) FROM analytics.dim_date d "
    "LEFT JOIN analytics.orders o ON o.order_purchase_timestamp::date = d.date_key "
    "GROUP BY 1 ORDER BY 1",
    # a cross join is fine once something constrains it
    "SELECT count(*) FROM analytics.orders o, analytics.order_items oi "
    "WHERE oi.order_id = o.order_id",
    "SELECT r.review_score, count(*) FROM analytics.reviews r GROUP BY 1 ORDER BY 1",
    "SELECT t.product_category_name_english, count(*) "
    "FROM analytics.product_category_name_translation t GROUP BY 1",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("sql", "expected_code"), HOSTILE, ids=[s[:60] for s, _ in HOSTILE])
def test_hostile_queries_are_rejected(settings: Settings, sql: str, expected_code: str) -> None:
    verdict = guard(sql, settings)
    assert verdict.verdict == "rejected", f"expected rejection, got {verdict.verdict}"
    assert not verdict.executable
    assert verdict.rewritten_sql is None, "a rejected query must not produce runnable SQL"
    assert expected_code in verdict.codes, (
        f"expected reason {expected_code!r}, got {list(verdict.codes)}"
    )


@pytest.mark.parametrize(
    ("sql", "expected_code"), ESCALATING, ids=[s[:60] for s, _ in ESCALATING]
)
def test_restricted_columns_escalate_rather_than_being_stripped(
    settings: Settings, sql: str, expected_code: str
) -> None:
    verdict = guard(sql, settings)
    assert verdict.verdict == "escalated", f"expected escalation, got {verdict.verdict}"
    assert verdict.allowed, "an escalation is allowed-pending-approval, not a rejection"
    assert not verdict.executable, "it must not run before a human decides"
    assert expected_code in verdict.codes
    assert verdict.sensitive_columns, "the audit must record which columns were involved"


@pytest.mark.parametrize("sql", LEGITIMATE, ids=[s[:60] for s in LEGITIMATE])
def test_ordinary_analysis_is_allowed(settings: Settings, sql: str) -> None:
    verdict = guard(sql, settings)
    assert verdict.verdict == "allowed", (
        f"legitimate query was {verdict.verdict}: {list(verdict.messages)}"
    )
    assert verdict.executable
    assert verdict.rewritten_sql
    assert verdict.referenced_objects, "an analytical query should resolve at least one object"


# ---------------------------------------------------------------------------
# Behaviour beyond the corpus
# ---------------------------------------------------------------------------


def test_a_missing_limit_is_injected(settings: Settings) -> None:
    verdict = guard("SELECT order_id FROM analytics.orders", settings)
    assert verdict.row_limit == settings.sql_default_row_limit
    assert "limit_injected" in verdict.codes
    assert f"LIMIT {settings.sql_default_row_limit}" in (verdict.rewritten_sql or "")


def test_an_oversized_limit_is_clamped(settings: Settings) -> None:
    verdict = guard(f"SELECT order_id FROM analytics.orders LIMIT {10**9}", settings)
    assert verdict.row_limit == settings.sql_max_row_limit
    assert "limit_clamped" in verdict.codes
    assert f"LIMIT {settings.sql_max_row_limit}" in (verdict.rewritten_sql or "")


def test_a_reasonable_limit_is_left_alone(settings: Settings) -> None:
    verdict = guard("SELECT order_id FROM analytics.orders LIMIT 10", settings)
    assert verdict.row_limit == 10
    assert "limit_injected" not in verdict.codes
    assert "limit_clamped" not in verdict.codes


def test_a_requested_row_limit_is_honoured_up_to_the_ceiling(settings: Settings) -> None:
    verdict = guard("SELECT order_id FROM analytics.orders", settings, row_limit=25)
    assert verdict.row_limit == 25
    over = guard(
        "SELECT order_id FROM analytics.orders",
        settings,
        row_limit=settings.sql_max_row_limit * 10,
    )
    assert over.row_limit == settings.sql_max_row_limit


def test_a_set_operation_gets_a_limit_too(settings: Settings) -> None:
    verdict = guard(
        "SELECT order_id FROM analytics.orders UNION SELECT order_id FROM analytics.order_items",
        settings,
    )
    assert verdict.executable
    assert "LIMIT" in (verdict.rewritten_sql or "")


def test_referenced_objects_are_recorded_for_the_audit(settings: Settings) -> None:
    verdict = guard(
        "SELECT o.order_id, p.product_category_name FROM analytics.orders o "
        "JOIN analytics.order_items oi ON oi.order_id = o.order_id "
        "JOIN analytics.products p ON p.product_id = oi.product_id",
        settings,
    )
    assert set(verdict.referenced_objects) == {
        "analytics.orders",
        "analytics.order_items",
        "analytics.products",
    }


def test_cte_names_are_not_checked_against_the_catalog(settings: Settings) -> None:
    """A CTE alias looks like a table in the AST but is not an object."""
    verdict = guard(
        "WITH monthly AS (SELECT year_month FROM analytics.v_order_revenue) "
        "SELECT * FROM monthly",
        settings,
    )
    assert verdict.executable, list(verdict.messages)
    assert verdict.referenced_objects == ("analytics.v_order_revenue",)


def test_a_cte_cannot_shadow_a_forbidden_object(settings: Settings) -> None:
    """Naming a CTE after a catalog table must not let the real table through."""
    verdict = guard(
        "WITH pg_authid AS (SELECT 1 AS n) SELECT * FROM pg_catalog.pg_authid",
        settings,
    )
    assert verdict.verdict == "rejected"
    assert "forbidden_schema" in verdict.codes


def test_restricted_columns_are_recorded_even_when_the_query_is_allowed(
    settings: Settings,
) -> None:
    """The audit records what a query touched, not only what was refused."""
    verdict = guard("SELECT count(DISTINCT email) FROM analytics.customer_contact", settings)
    assert verdict.verdict == "allowed"
    assert "analytics.customer_contact.email" in verdict.sensitive_columns


def test_every_verdict_carries_a_reason(settings: Settings) -> None:
    for sql, _ in HOSTILE:
        verdict = guard(sql, settings)
        assert verdict.reasons, f"no reason recorded for: {sql[:60]}"
    for sql, _ in ESCALATING:
        assert guard(sql, settings).reasons


def test_verdict_states_are_mutually_consistent(settings: Settings) -> None:
    rejected = guard("DELETE FROM analytics.orders", settings)
    escalated = guard("SELECT email FROM analytics.customer_contact", settings)
    allowed = guard("SELECT count(*) FROM analytics.orders", settings)

    assert (rejected.verdict, rejected.allowed, rejected.executable) == ("rejected", False, False)
    assert (escalated.verdict, escalated.allowed, escalated.executable) == ("escalated", True, False)
    assert (allowed.verdict, allowed.allowed, allowed.executable) == ("allowed", True, True)


def test_guard_error_reports_the_reasons(settings: Settings) -> None:
    from analyst_agent.sql_guard import GuardError

    verdict = guard("DROP TABLE analytics.orders", settings)
    error = GuardError(verdict)
    assert "rejected" in str(error)
    assert "not_a_select" in str(error)


def test_allowed_schemas_is_configurable(settings: Settings) -> None:
    """The allowlist is configuration, not a hard-coded constant."""
    narrowed = settings.model_copy(update={"allowed_schemas": ("marts",)})
    verdict = guard("SELECT count(*) FROM analytics.orders", narrowed)
    assert verdict.verdict == "rejected"
    assert "schema_not_allowed" in verdict.codes


def test_no_hostile_query_ever_produces_runnable_sql(settings: Settings) -> None:
    """The single property that matters most: a rejection yields nothing to execute."""
    for sql, _ in HOSTILE:
        verdict = guard(sql, settings)
        assert verdict.rewritten_sql is None
        assert verdict.row_limit is None
        assert not verdict.executable


def test_a_cte_cannot_shadow_an_out_of_allowlist_schema(settings: Settings) -> None:
    """Same bypass class as the catalog case: a qualified name never resolves to a CTE."""
    verdict = guard(
        "WITH secrets AS (SELECT 1 AS n) SELECT * FROM public.secrets", settings
    )
    assert verdict.verdict == "rejected"
    assert "schema_not_allowed" in verdict.codes


def test_a_cte_reference_still_resolves_when_it_shares_a_real_table_name(
    settings: Settings,
) -> None:
    """The fix must not break the legitimate case: an unqualified CTE reference works."""
    verdict = guard(
        "WITH orders AS (SELECT order_id FROM analytics.orders) SELECT count(*) FROM orders",
        settings,
    )
    assert verdict.executable, list(verdict.messages)
    assert verdict.referenced_objects == ("analytics.orders",)
