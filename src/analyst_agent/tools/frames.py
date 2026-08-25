"""Result frames, held between tool calls.

``python_analysis`` and ``chart_builder`` work on the output of a *previous* query rather than
on SQL of their own. That output has to live somewhere between calls, and the obvious answer —
keep it in memory — breaks the moment the process restarts mid-run, which Step 10 requires to
be survivable.

So the store is a bounded in-process cache with a **rehydration** path: if a frame is missing,
it is rebuilt by re-running the statement recorded in ``sql_audit`` for that ``query_id``.
Nothing is lost by eviction or by a restart, memory stays bounded, and the frame a tool sees is
always traceable to an audited statement.

Only statements the audit records as ``allowed`` and ``executed`` can be rehydrated, so this
cannot become a route to re-run something the guard refused.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass

import pandas as pd

from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)

MAX_FRAMES = 32
MAX_TOTAL_CELLS = 4_000_000


class FrameNotAvailableError(KeyError):
    """The frame is neither cached nor rebuildable from the audit."""


@dataclass(frozen=True)
class FrameMeta:
    query_id: uuid.UUID
    purpose: str
    truncated: bool
    row_count: int
    columns: tuple[str, ...]


class FrameStore:
    """Bounded LRU cache of result frames, with rehydration from the audit trail."""

    def __init__(self, max_frames: int = MAX_FRAMES, max_cells: int = MAX_TOTAL_CELLS) -> None:
        self._frames: OrderedDict[uuid.UUID, pd.DataFrame] = OrderedDict()
        self._meta: dict[uuid.UUID, FrameMeta] = {}
        self._max_frames = max_frames
        self._max_cells = max_cells

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def cells(self) -> int:
        return sum(frame.size for frame in self._frames.values())

    def put(self, query_id: uuid.UUID, frame: pd.DataFrame, meta: FrameMeta) -> None:
        self._frames[query_id] = frame
        self._meta[query_id] = meta
        self._frames.move_to_end(query_id)
        self._evict()

    def _evict(self) -> None:
        while len(self._frames) > self._max_frames or (
            self.cells > self._max_cells and len(self._frames) > 1
        ):
            evicted, _ = self._frames.popitem(last=False)
            log.debug("frame evicted", query_id=str(evicted), remaining=len(self._frames))

    def meta(self, query_id: uuid.UUID) -> FrameMeta | None:
        return self._meta.get(query_id)

    def get(self, query_id: uuid.UUID) -> pd.DataFrame:
        """Return the frame, rebuilding it from the audit trail if it is no longer cached."""
        cached = self._frames.get(query_id)
        if cached is not None:
            self._frames.move_to_end(query_id)
            return cached

        frame, meta = self._rehydrate(query_id)
        self.put(query_id, frame, meta)
        log.info("frame rehydrated", query_id=str(query_id), rows=len(frame))
        return frame

    def _rehydrate(self, query_id: uuid.UUID) -> tuple[pd.DataFrame, FrameMeta]:
        from analyst_agent.db.engine import ro_conn, rw_conn

        with rw_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT purpose, rewritten_sql, sql_text, verdict, executed, truncated "
                "FROM agent.sql_audit WHERE query_id = %s",
                (query_id,),
            )
            record = cur.fetchone()

        if record is None:
            raise FrameNotAvailableError(f"no audited query {query_id}")
        if record["verdict"] != "allowed" or not record["executed"]:
            # Rehydration must not become a way to run something the guard refused.
            raise FrameNotAvailableError(
                f"query {query_id} was {record['verdict']} and never executed; "
                "it cannot be rebuilt"
            )

        statement = record["rewritten_sql"] or record["sql_text"]
        with ro_conn() as conn, conn.cursor() as cur:
            cur.execute(statement)
            rows = cur.fetchall()

        frame = pd.DataFrame(rows)
        meta = FrameMeta(
            query_id=query_id,
            purpose=record["purpose"],
            truncated=bool(record["truncated"]),
            row_count=len(frame),
            columns=tuple(str(c) for c in frame.columns),
        )
        return frame, meta


_store = FrameStore()


def get_store() -> FrameStore:
    return _store


def reset_store() -> None:
    """For tests: forget everything, so rehydration can be exercised deliberately."""
    global _store
    _store = FrameStore()
