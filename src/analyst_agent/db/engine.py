"""Database access. Two pools, deliberately not interchangeable.

* ``rw_pool()`` — the service's own state: runs, traces, audit, LangGraph checkpoints.
* ``ro_pool()`` — every agent-generated query. Connects as ``analyst_ro`` and additionally
  pins ``default_transaction_read_only=on`` and the statement timeout at the *session* level,
  on top of the role-level settings from ``db/init/sql/01_roles.sql``.

The session-level pinning is redundant with the role settings, and that is the point: control
C1 should not depend on a single ``ALTER ROLE`` having been applied to the cluster someone is
actually running against. If the role were ever created without those settings, the pool still
opens read-only sessions.

Nothing here builds SQL from agent output. Query text reaches the database only through
``tools/sql_runner.py``, and only after ``sql_guard`` has approved it.
"""

from __future__ import annotations

import atexit
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from analyst_agent.config import Settings, get_settings
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

_rw_pool: ConnectionPool | None = None
_ro_pool: ConnectionPool | None = None


def _session_options(read_only: bool, settings: Settings) -> str:
    """libpq ``options`` string applied to every connection the pool opens."""
    parts = [f"-c statement_timeout={settings.sql_statement_timeout_ms}"]
    if read_only:
        parts.append("-c default_transaction_read_only=on")
        parts.append(f"-c idle_in_transaction_session_timeout={settings.sql_idle_tx_timeout_ms}")
    return " ".join(parts)


def _build_pool(dsn: str, *, read_only: bool, settings: Settings, name: str) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
        name=name,
        kwargs={
            "options": _session_options(read_only, settings),
            "row_factory": dict_row,
            "autocommit": read_only,
        },
    )
    pool.open(wait=True, timeout=10)
    log.info(
        "database pool opened",
        pool=name,
        read_only=read_only,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    return pool


def rw_pool() -> ConnectionPool:
    global _rw_pool
    if _rw_pool is None:
        settings = get_settings()
        _rw_pool = _build_pool(
            settings.db_rw_dsn.get_secret_value(),
            read_only=False,
            settings=settings,
            name="app_rw",
        )
    return _rw_pool


def ro_pool() -> ConnectionPool:
    global _ro_pool
    if _ro_pool is None:
        settings = get_settings()
        _ro_pool = _build_pool(
            settings.db_ro_dsn.get_secret_value(),
            read_only=True,
            settings=settings,
            name="analyst_ro",
        )
    return _ro_pool


@contextmanager
def rw_conn() -> Iterator[psycopg.Connection[Any]]:
    """A read-write connection for service state. Commits on success, rolls back on error."""
    with rw_pool().connection() as conn:
        yield conn


@contextmanager
def ro_conn() -> Iterator[psycopg.Connection[Any]]:
    """A read-only connection for analytical queries. Autocommit; nothing here can write."""
    with ro_pool().connection() as conn:
        yield conn


def assert_read_only() -> None:
    """Verify at startup that the read-only pool really is read-only.

    Cheap, and it turns a misconfigured DSN — for example the read-write one pasted into
    ``DB_RO_DSN`` — into a startup failure instead of a security incident discovered later.
    """
    with ro_conn() as conn, conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        row = cur.fetchone()
        if not row or row["default_transaction_read_only"] != "on":
            raise RuntimeError(
                "DB_RO_DSN does not open read-only sessions. Refusing to start: "
                "control C1 is not in force."
            )
        cur.execute("SELECT current_user AS who, (SELECT usesuper FROM pg_user "
                    "WHERE usename = current_user) AS is_super")
        row = cur.fetchone()
        if row and row["is_super"]:
            raise RuntimeError(
                f"DB_RO_DSN connects as superuser {row['who']!r}. Refusing to start."
            )
        log.info("read-only pool verified", user=row["who"] if row else None)


def close_pools() -> None:
    global _rw_pool, _ro_pool
    for pool in (_rw_pool, _ro_pool):
        if pool is not None:
            pool.close()
    _rw_pool = _ro_pool = None


atexit.register(close_pools)
