"""Durable graph state (design document section 10).

LangGraph's Postgres checkpointer writes state after every node, keyed by ``thread_id``. That is
what makes three separate requirements work with one mechanism:

* a crash inside a node resumes from the **last completed node**, not from the beginning;
* an API restart mid-investigation does not lose the run;
* an approval that arrives an hour later resumes a run whose process has long since exited.

It uses the ``app_rw`` DSN. The checkpoint tables are the service's own state, and ``analyst_ro``
has no privileges on that schema.
"""

from __future__ import annotations

import atexit

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from analyst_agent.config import get_settings
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None
_saver: PostgresSaver | None = None


def _checkpoint_pool() -> ConnectionPool:
    """A pool of the shape the checkpointer needs.

    Separate from ``db/engine.py``'s pools on purpose: the checkpointer requires autocommit and
    its own row factory, and quietly changing those on the pool the repository uses would be a
    surprising side effect.
    """
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.db_rw_dsn.get_secret_value(),
            min_size=1,
            max_size=max(2, settings.db_pool_max // 2),
            open=False,
            name="checkpointer",
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        _pool.open(wait=True, timeout=10)
    return _pool


def get_checkpointer(setup: bool = True) -> PostgresSaver:
    """The process-wide checkpointer.

    ``setup()`` creates the checkpoint tables if they are absent and is idempotent, so a fresh
    database works without a separate migration step for LangGraph's own schema.
    """
    global _saver
    if _saver is None:
        _saver = PostgresSaver(_checkpoint_pool())  # type: ignore[arg-type]
        if setup:
            _saver.setup()
        log.info("checkpointer ready")
    return _saver


def close_checkpointer() -> None:
    global _pool, _saver
    if _pool is not None:
        _pool.close()
    _pool = None
    _saver = None


atexit.register(close_checkpointer)


def thread_config(thread_id: str) -> dict:
    """The config LangGraph needs to address one run's checkpoint."""
    return {"configurable": {"thread_id": thread_id}}
