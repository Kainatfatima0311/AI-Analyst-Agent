"""Shared test fixtures.

Integration tests need a running Postgres. They are marked ``integration`` and skip cleanly
when no database is reachable, so ``pytest`` still passes on a machine that has not run
``docker compose up -d db`` yet. CI starts the service container, so nothing is skipped there.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")


def _reachable(dsn: str | None) -> bool:
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def ro_dsn() -> str:
    dsn = os.environ.get("DB_RO_DSN")
    if not _reachable(dsn):
        pytest.skip("DB_RO_DSN not reachable — run `docker compose up -d db`")
    assert dsn is not None
    return dsn


@pytest.fixture(scope="session")
def rw_dsn() -> str:
    dsn = os.environ.get("DB_RW_DSN")
    if not _reachable(dsn):
        pytest.skip("DB_RW_DSN not reachable — run `docker compose up -d db`")
    assert dsn is not None
    return dsn


@pytest.fixture
def ro_conn(ro_dsn: str):
    """A fresh read-only connection per test.

    ``row_factory=dict_row`` mirrors what ``db/engine.py`` configures on the real pools, so a
    test exercises the same row shape the production code assumes.
    """
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(ro_dsn, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture(scope="session")
def seeded(rw_dsn: str) -> None:
    """Skip a test that needs data if the analytics tables are empty."""
    import psycopg

    with psycopg.connect(rw_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM analytics.orders")
        if int(cur.fetchone()[0]) == 0:  # type: ignore[index]
            pytest.skip("analytics.orders is empty — run `python scripts/seed_db.py`")
