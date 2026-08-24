"""Prove that the read-only role really is read-only.

This is control C1 in docs/security-controls.md — the layer that holds when every layer of
validation above it fails. It is checked as an executable assertion rather than trusted from
the grant script, and the same assertions run in CI via tests/integration/test_readonly_role.py.

    python scripts/smoke.py

Exit code 0 means every check passed. Any failure is a security regression.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# Statements the read-only role must NOT be able to run. Each is (label, sql).
FORBIDDEN: list[tuple[str, str]] = [
    ("INSERT", "INSERT INTO analytics.customers (customer_id, customer_unique_id) VALUES ('x', 'x')"),
    ("UPDATE", "UPDATE analytics.orders SET order_status = 'tampered'"),
    ("DELETE", "DELETE FROM analytics.orders"),
    ("TRUNCATE", "TRUNCATE analytics.reviews"),
    ("CREATE TABLE", "CREATE TABLE analytics.evil (id int)"),
    ("CREATE TABLE in public", "CREATE TABLE public.evil (id int)"),
    ("DROP TABLE", "DROP TABLE analytics.orders"),
    ("ALTER TABLE", "ALTER TABLE analytics.orders ADD COLUMN evil text"),
    ("CREATE INDEX", "CREATE INDEX evil_idx ON analytics.orders (order_id)"),
    ("GRANT", "GRANT ALL ON analytics.orders TO PUBLIC"),
    ("CREATE ROLE", "CREATE ROLE evil LOGIN"),
    ("read pg_authid", "SELECT rolname FROM pg_authid"),
    ("copy from file", "COPY analytics.customers FROM '/etc/passwd'"),
]

ALLOWED: list[tuple[str, str]] = [
    ("SELECT from orders", "SELECT count(*) FROM analytics.orders"),
    ("SELECT from the revenue view", "SELECT count(*) FROM analytics.v_order_revenue"),
    ("join across facts", (
        "SELECT count(*) FROM analytics.orders o "
        "JOIN analytics.order_items oi ON oi.order_id = o.order_id"
    )),
    ("aggregate over a sensitive column", "SELECT count(DISTINCT customer_unique_id) FROM analytics.customers"),
]


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def check_forbidden(dsn: str) -> tuple[int, int]:
    passed = failed = 0
    for label, sql in FORBIDDEN:
        # A fresh connection per statement: the first failure aborts its transaction.
        with psycopg.connect(dsn) as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.rollback()
            except psycopg.Error as exc:
                code = getattr(exc, "sqlstate", None) or "?"
                print(f"  ok      {label:<34} rejected ({code})")
                passed += 1
            else:
                print(f"  FAIL    {label:<34} SUCCEEDED — this must not be possible")
                failed += 1
    return passed, failed


def check_allowed(dsn: str) -> tuple[int, int]:
    passed = failed = 0
    with psycopg.connect(dsn) as conn:
        for label, sql in ALLOWED:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.fetchone()
                print(f"  ok      {label:<34} allowed")
                passed += 1
            except psycopg.Error as exc:
                print(f"  FAIL    {label:<34} rejected: {exc}")
                failed += 1
                conn.rollback()
    return passed, failed


def check_privileges(dsn: str) -> tuple[int, int]:
    """Assert the grant surface directly, rather than inferring it from a failing statement.

    Querying privileges is the honest check: `SELECT ... FROM agent.runs` would also "fail"
    simply because that table does not exist yet, which would pass the test for the wrong
    reason.
    """
    expectations: list[tuple[str, str, bool]] = [
        ("USAGE on schema analytics", "has_schema_privilege('analytics', 'USAGE')", True),
        ("USAGE on schema agent", "has_schema_privilege('agent', 'USAGE')", False),
        ("CREATE on schema analytics", "has_schema_privilege('analytics', 'CREATE')", False),
        ("CREATE on schema public", "has_schema_privilege('public', 'CREATE')", False),
        ("SELECT on analytics.orders", "has_table_privilege('analytics.orders', 'SELECT')", True),
        ("INSERT on analytics.orders", "has_table_privilege('analytics.orders', 'INSERT')", False),
        ("UPDATE on analytics.orders", "has_table_privilege('analytics.orders', 'UPDATE')", False),
        ("DELETE on analytics.orders", "has_table_privilege('analytics.orders', 'DELETE')", False),
        (
            "SELECT on analytics.customer_contact",
            "has_table_privilege('analytics.customer_contact', 'SELECT')",
            True,
        ),
        ("superuser", "(SELECT usesuper FROM pg_user WHERE usename = current_user)", False),
    ]

    passed = failed = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for label, expression, expected in expectations:
            cur.execute(f"SELECT {expression}")
            actual = cur.fetchone()[0]  # type: ignore[index]
            if bool(actual) is expected:
                print(f"  ok      {label:<40} = {actual}")
                passed += 1
            else:
                print(f"  FAIL    {label:<40} = {actual}, expected {expected}")
                failed += 1
    return passed, failed


def check_timeout(dsn: str) -> tuple[int, int]:
    """The statement timeout must fire, so a runaway query cannot pin the database."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        configured = cur.fetchone()[0]  # type: ignore[index]
        print(f"  info    statement_timeout = {configured}")
        cur.execute("SHOW default_transaction_read_only")
        read_only = cur.fetchone()[0]  # type: ignore[index]
        if read_only != "on":
            print(f"  FAIL    default_transaction_read_only = {read_only}, expected on")
            return 0, 1
        print("  ok      default_transaction_read_only = on")

    # Deliberately exceed the timeout. Uses a short local override so the check stays fast
    # even if the role-level timeout is generous.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        try:
            cur.execute("SET statement_timeout = '750ms'")
            cur.execute("SELECT pg_sleep(5)")
        except psycopg.errors.QueryCanceled:
            print("  ok      statement timeout fires on pg_sleep(5)")
            return 2, 0
        except psycopg.Error as exc:
            print(f"  ok      pg_sleep rejected outright ({getattr(exc, 'sqlstate', '?')})")
            return 2, 0
        print("  FAIL    pg_sleep(5) ran to completion — no statement timeout in effect")
        return 1, 1


def main() -> int:
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DB_RO_DSN")
    if not dsn:
        print("DB_RO_DSN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    total_pass = total_fail = 0

    print("[smoke] statements the read-only role must be unable to run")
    p, f = check_forbidden(dsn)
    total_pass, total_fail = total_pass + p, total_fail + f

    print("\n[smoke] statements it must still be able to run")
    p, f = check_allowed(dsn)
    total_pass, total_fail = total_pass + p, total_fail + f

    print("\n[smoke] the grant surface itself")
    p, f = check_privileges(dsn)
    total_pass, total_fail = total_pass + p, total_fail + f

    print("\n[smoke] session guarantees")
    p, f = check_timeout(dsn)
    total_pass, total_fail = total_pass + p, total_fail + f

    print(f"\n[smoke] {total_pass} passed, {total_fail} failed")
    if total_fail:
        print("[smoke] SECURITY REGRESSION — control C1 is not holding")
        return 1
    print("[smoke] OK — control C1 verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
