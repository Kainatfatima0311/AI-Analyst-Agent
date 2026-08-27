"""The values a query returned have to reach the node that writes the conclusion.

This exists because of a real failure on a live run. ``_history`` told every node downstream of
``interpret`` only that a query had returned twelve rows — never what was in them. Asked to write
a conclusion from that, the model reconstructed the monthly revenue figures from its own earlier
prose: two were exact and the other ten were plausible interpolations between them. Prompting
cannot fix that, because the numbers were genuinely absent from the context.

So these tests pin three things: the values arrive, every cut made to fit them is *stated*, and a
frame that has gone missing is reported as missing rather than as empty.
"""

from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
import pytest

from analyst_agent.agent.nodes.linear import (
    RESULT_MAX_COLUMNS,
    RESULT_MAX_ROWS,
    _cell,
    _render_frame,
    _result_tables,
)
from analyst_agent.tools.frames import FrameMeta, get_store, reset_store


@pytest.fixture(autouse=True)
def _clean_store() -> Any:
    reset_store()
    yield
    reset_store()


def _stored(frame: pd.DataFrame, purpose: str = "monthly revenue") -> str:
    query_id = uuid.uuid4()
    get_store().put(
        query_id,
        frame,
        FrameMeta(
            query_id=query_id,
            purpose=purpose,
            truncated=False,
            row_count=len(frame),
            columns=tuple(str(c) for c in frame.columns),
        ),
    )
    return str(query_id)


def _state(*query_ids: str) -> dict[str, Any]:
    return {
        "queries": [
            {"query_id": q, "verdict": "allowed", "purpose": "p", "row_count": 1}
            for q in query_ids
        ]
    }


# --- the values arrive ---------------------------------------------------------


def test_every_returned_value_is_in_the_context() -> None:
    """The regression: a conclusion cannot cite a figure the context never held."""
    frame = pd.DataFrame(
        {
            "month": ["2017-01", "2017-02", "2017-12"],
            "revenue": [163902.61, 178527.80, 344110.32],
        }
    )
    rendered = _result_tables(_state(_stored(frame)))
    for value in ("163902.61", "178527.80", "344110.32"):
        assert value in rendered
    assert "2017-02" in rendered


def test_the_instruction_forbids_a_number_from_outside_the_tables() -> None:
    rendered = _result_tables(_state(_stored(pd.DataFrame({"a": [1]}))))
    assert "quote figures from here exactly" in rendered
    assert "do not state a number that is not in one of these tables" in rendered


def test_each_table_is_keyed_by_its_query_id_so_a_claim_stays_traceable() -> None:
    first = _stored(pd.DataFrame({"a": [1]}))
    second = _stored(pd.DataFrame({"b": [2]}))
    rendered = _result_tables(_state(first, second))
    assert first in rendered
    assert second in rendered


def test_no_executed_queries_produces_nothing_rather_than_an_empty_heading() -> None:
    assert _result_tables({"queries": []}) == ""
    assert _result_tables({}) == ""


def test_a_rejected_query_contributes_no_table() -> None:
    """Only a query that ran has values, and only those may be cited."""
    state = {
        "queries": [
            {"query_id": str(uuid.uuid4()), "verdict": "rejected", "purpose": "p"},
        ]
    }
    assert _result_tables(state) == ""


def test_a_human_approved_query_contributes_its_table() -> None:
    """`approved` is execution on human authority, and its rows are evidence like any other."""
    query_id = _stored(pd.DataFrame({"total": [42]}))
    state = {
        "queries": [
            {"query_id": query_id, "verdict": "approved", "purpose": "p", "row_count": 1}
        ]
    }
    assert "42" in _result_tables(state)


# --- every cut is stated -------------------------------------------------------


def test_row_truncation_is_declared_and_forbids_reporting_the_remainder() -> None:
    """A silently truncated table invites the model to invent the rest."""
    frame = pd.DataFrame({"n": range(RESULT_MAX_ROWS + 25)})
    rendered, notes = _render_frame(frame)
    assert f"first {RESULT_MAX_ROWS} of {RESULT_MAX_ROWS + 25} rows" in notes[0]
    assert "do not report figures for the rest" in notes[0]
    assert len(rendered.splitlines()) == RESULT_MAX_ROWS + 2  # header + rule + rows


def test_column_truncation_is_declared() -> None:
    frame = pd.DataFrame({f"c{i}": [i] for i in range(RESULT_MAX_COLUMNS + 4)})
    _, notes = _render_frame(frame)
    assert f"showing {RESULT_MAX_COLUMNS} of {RESULT_MAX_COLUMNS + 4} columns" in notes


def test_a_result_that_fits_is_declared_complete_by_saying_nothing() -> None:
    _, notes = _render_frame(pd.DataFrame({"a": [1, 2, 3]}))
    assert notes == []


def test_the_character_budget_stops_early_and_says_so() -> None:
    """A five-thousand-row result must not crowd out the reasoning it supports."""
    wide = pd.DataFrame({f"c{i}": ["x" * 60] * RESULT_MAX_ROWS for i in range(RESULT_MAX_COLUMNS)})
    ids = [_stored(wide) for _ in range(6)]
    rendered = _result_tables(_state(*ids))
    assert "earlier results omitted" in rendered
    # The first tables are present; the last are not, and the omission is visible.
    assert ids[0] in rendered
    assert ids[-1] not in rendered


# --- a missing frame is missing, not empty -------------------------------------


def test_an_unavailable_frame_is_reported_as_unavailable() -> None:
    """Omitting it would read as "this query returned nothing", which is a different claim.

    The store is stubbed rather than left to reach the database. Recovering a frame rebuilds it
    from the audit trail, so with no database this test used to fail on a pool timeout — which was
    a *real* finding about the code, not about the test: the catch was narrowed to KeyError and
    ValueError and a PoolTimeout escaped, aborting the node mid-run. Both are fixed; this keeps
    the unit test a unit test.
    """
    missing = str(uuid.uuid4())

    class Unavailable:
        def get(self, _query_id: object) -> object:
            raise RuntimeError("the pool is not available")

    import analyst_agent.tools.frames as frames

    original = frames.get_store
    frames.get_store = lambda: Unavailable()  # type: ignore[assignment]
    try:
        rendered = _result_tables(_state(missing))
    finally:
        frames.get_store = original  # type: ignore[assignment]

    assert f"{missing}: values no longer available" in rendered


# --- cell formatting -----------------------------------------------------------


def test_a_monetary_value_is_not_rounded_away() -> None:
    """Rounding here is a silent edit to the evidence the answer then cites as exact."""
    assert _cell(163902.61) == "163902.61"
    assert _cell(344110.32) == "344110.32"


def test_null_and_nan_both_read_as_null() -> None:
    assert _cell(None) == "NULL"
    assert _cell(float("nan")) == "NULL"


def test_non_numeric_values_pass_through_as_text() -> None:
    assert _cell("2017-01") == "2017-01"
    assert _cell(12) == "12"


def test_the_header_names_the_columns_the_rows_are_in() -> None:
    rendered, _ = _render_frame(pd.DataFrame({"month": ["2017-01"], "revenue": [1.5]}))
    lines = rendered.splitlines()
    assert lines[0] == "month | revenue"
    assert lines[2] == "2017-01 | 1.50"
