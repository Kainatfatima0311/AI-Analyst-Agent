"""Control C1: the read-only role must be physically unable to write.

This is the layer that holds when every layer of validation above it fails, so it is asserted
here as a required CI gate rather than trusted from the grant script. `scripts/smoke.py` runs
the same checks interactively with human-readable output.
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.integration

WRITE_STATEMENTS = [
    pytest.param(
        "INSERT INTO analytics.customers (customer_id, customer_unique_id) VALUES ('x', 'x')",
        id="insert",
    ),
    pytest.param("UPDATE analytics.orders SET order_status = 'tampered'", id="update"),
    pytest.param("DELETE FROM analytics.orders", id="delete"),
    pytest.param("TRUNCATE analytics.reviews", id="truncate"),
    pytest.param("CREATE TABLE analytics.evil (id int)", id="create-table-analytics"),
    pytest.param("CREATE TABLE public.evil (id int)", id="create-table-public"),
    pytest.param("DROP TABLE analytics.orders", id="drop-table"),
    pytest.param("ALTER TABLE analytics.orders ADD COLUMN evil text", id="alter-table"),
    pytest.param("CREATE INDEX evil_idx ON analytics.orders (order_id)", id="create-index"),
    pytest.param("GRANT ALL ON analytics.orders TO PUBLIC", id="grant"),
    pytest.param("CREATE ROLE evil LOGIN", id="create-role"),
    pytest.param("SELECT rolname FROM pg_authid", id="read-pg-authid"),
    pytest.param("COPY analytics.customers FROM '/etc/passwd'", id="copy-from-file"),
]

READ_STATEMENTS = [
    pytest.param("SELECT count(*) FROM analytics.orders", id="select-orders"),
    pytest.param("SELECT count(*) FROM analytics.v_order_revenue", id="select-view"),
    pytest.param(
        "SELECT count(*) FROM analytics.orders o "
        "JOIN analytics.order_items oi ON oi.order_id = o.order_id",
        id="join-facts",
    ),
    pytest.param(
        "SELECT count(DISTINCT customer_unique_id) FROM analytics.customers",
        id="aggregate-over-sensitive-column",
    ),
]

# (label, expression, expected) — asserted directly rather than inferred from a failing query,
# because a missing table would "fail" for the wrong reason.
PRIVILEGES = [
    ("usage-analytics", "has_schema_privilege('analytics', 'USAGE')", True),
    ("usage-agent", "has_schema_privilege('agent', 'USAGE')", False),
    ("create-analytics", "has_schema_privilege('analytics', 'CREATE')", False),
    ("create-public", "has_schema_privilege('public', 'CREATE')", False),
    ("select-orders", "has_table_privilege('analytics.orders', 'SELECT')", True),
    ("insert-orders", "has_table_privilege('analytics.orders', 'INSERT')", False),
    ("update-orders", "has_table_privilege('analytics.orders', 'UPDATE')", False),
    ("delete-orders", "has_table_privilege('analytics.orders', 'DELETE')", False),
    ("superuser", "(SELECT usesuper FROM pg_user WHERE usename = current_user)", False),
]


@pytest.mark.parametrize("sql", WRITE_STATEMENTS)
def test_write_statements_are_rejected(ro_dsn: str, sql: str) -> None:
    # A fresh connection per statement: the first error aborts its transaction.
    with psycopg.connect(ro_dsn) as conn:
        with pytest.raises(psycopg.Error), conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()


@pytest.mark.parametrize("sql", READ_STATEMENTS)
def test_analytical_reads_are_allowed(ro_conn: psycopg.Connection, seeded: None, sql: str) -> None:
    with ro_conn.cursor() as cur:
        cur.execute(sql)
        assert cur.fetchone() is not None


@pytest.mark.parametrize(("label", "expression", "expected"), PRIVILEGES)
def test_grant_surface(ro_conn: psycopg.Connection, label: str, expression: str, expected: bool) -> None:
    with ro_conn.cursor() as cur:
        cur.execute(f"SELECT {expression}")
        row = cur.fetchone()
        assert row is not None
        assert bool(row[0]) is expected, f"{label}: expected {expected}, got {row[0]}"


def test_session_is_read_only_by_default(ro_conn: psycopg.Connection) -> None:
    with ro_conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "on"


def test_statement_timeout_fires(ro_dsn: str) -> None:
    with psycopg.connect(ro_dsn) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '500ms'")
        with pytest.raises(psycopg.Error):
            cur.execute("SELECT pg_sleep(5)")


def test_statement_timeout_is_configured_on_the_role(ro_conn: psycopg.Connection) -> None:
    with ro_conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        row = cur.fetchone()
        assert row is not None
        assert row[0] not in ("0", "0ms"), "the role must carry a statement timeout"
