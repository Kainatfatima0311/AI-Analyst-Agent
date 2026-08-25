"""Tool 4: follow-up analysis over a query that has already run.

The design decision here is the operation set. The obvious implementation is to let the model
write pandas code and ``exec`` it. That is more flexible and it opens a sandbox-escape surface
that has to be defended forever. Instead this tool exposes a **fixed enumerated set** of
operations, each implemented here, each validated against the frame's real columns.

The trade is real and is recorded as a known limitation: some analyses are not expressible. What
is bought is that no model-authored code executes anywhere in this system.

Everything operates on a ``query_id`` from ``sql_runner``, so every derived number still traces
back to an audited statement. Frames are bounded by the row cap ``sql_runner`` already applied,
which is what bounds memory here - there is no separate sandbox doing that job.
"""

from __future__ import annotations

import uuid
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from analyst_agent.observability.logging import get_logger
from analyst_agent.tools.base import Tool, ToolResult
from analyst_agent.tools.frames import FrameNotAvailableError, get_store

log = get_logger(__name__)

MAX_RESULT_ROWS = 200

Operation = Literal[
    "describe",
    "group_by",
    "share_of_total",
    "period_over_period",
    "rolling",
    "correlation",
    "top_n",
    "linear_fit",
]
Aggregate = Literal["sum", "mean", "median", "count", "min", "max"]


class PythonAnalysisInput(BaseModel):
    model_config = {"extra": "forbid"}

    query_id: str = Field(description="The query_id returned by a previous sql_runner call.")
    operation: Operation = Field(
        description=(
            "describe: summary statistics per numeric column. "
            "group_by: aggregate `value` by `by`. "
            "share_of_total: each row's share of the total of `value`. "
            "period_over_period: absolute and percentage change in `value` ordered by "
            "`order_column`. "
            "rolling: rolling `agg` of `value` over `window` rows. "
            "correlation: pairwise correlation between numeric `columns`. "
            "top_n: the `top_n` largest rows by `value`. "
            "linear_fit: least-squares slope of `value` against row order, with r-squared."
        )
    )
    by: list[str] | None = Field(default=None, description="Grouping column(s), for group_by.")
    value: str | None = Field(
        default=None, description="The numeric column to analyse. Required by most operations."
    )
    agg: Aggregate | None = Field(default=None, description="Aggregate function. Defaults to sum.")
    order_column: str | None = Field(
        default=None,
        description="Ordering column for period_over_period and rolling. Defaults to the first column.",
    )
    window: int | None = Field(default=None, description="Window size for rolling. Defaults to 3.")
    columns: list[str] | None = Field(
        default=None, description="Columns to include, for correlation. Defaults to all numeric."
    )
    top_n: int | None = Field(default=None, description="How many rows for top_n. Defaults to 10.")


class PythonAnalysisTool(Tool[PythonAnalysisInput]):
    name = "python_analysis"
    description = """
Run one follow-up analysis on the result of a previous query.

Pass the query_id from sql_runner. This works on the full result, not the preview you were
shown, so use it instead of re-querying data you already have.

The operations are a fixed set, not arbitrary code: describe, group_by, share_of_total,
period_over_period, rolling, correlation, top_n, linear_fit. If what you need is not here,
write a different SQL query rather than trying to approximate it.

correlation measures association only. It is not evidence of cause, and an answer that treats
it as such will be wrong. Use it to *generate* a hypothesis, then test that hypothesis with a
query that could refute it.
"""
    input_model = PythonAnalysisInput

    def run(
        self, payload: PythonAnalysisInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        try:
            query_uuid = uuid.UUID(payload.query_id)
        except ValueError:
            return ToolResult.refuse(f"{payload.query_id!r} is not a valid query_id")

        store = get_store()
        try:
            frame = store.get(query_uuid)
        except FrameNotAvailableError as exc:
            return ToolResult.refuse(
                f"no result available for query {payload.query_id}: {exc}",
                guidance="Run the query with sql_runner first, then pass its query_id.",
            )

        if frame.empty:
            return ToolResult.refuse(
                "that query returned no rows, so there is nothing to analyse",
                query_id=payload.query_id,
            )

        handler = getattr(self, f"_op_{payload.operation}")
        missing = self._missing_columns(payload, frame)
        if missing:
            return ToolResult.refuse(
                f"column(s) not in that result: {', '.join(missing)}",
                available_columns=[str(c) for c in frame.columns],
            )

        try:
            result, summary = handler(payload, frame)
        except (ValueError, TypeError, KeyError) as exc:
            return ToolResult.refuse(
                f"{payload.operation} could not be applied to that result: {exc}",
                available_columns=[str(c) for c in frame.columns],
            )

        truncated = len(result) > MAX_RESULT_ROWS
        shown = result.head(MAX_RESULT_ROWS)
        return ToolResult(
            ok=True,
            summary=summary + (f"; showing {MAX_RESULT_ROWS} of {len(result)} rows" if truncated else ""),
            data={
                "query_id": payload.query_id,
                "operation": payload.operation,
                "columns": [str(c) for c in shown.columns],
                "rows": _records(shown),
                "row_count": len(result),
                "truncated": truncated,
            },
            audit={"operation": payload.operation, "input_rows": len(frame), "output_rows": len(result)},
        )

    # --- validation ------------------------------------------------------

    @staticmethod
    def _missing_columns(payload: PythonAnalysisInput, frame: pd.DataFrame) -> list[str]:
        """Every column the call names must exist in the frame.

        Checked up front so the refusal can name the available columns, rather than surfacing as
        a pandas KeyError the model has to decode.
        """
        present = {str(c) for c in frame.columns}
        named: list[str] = []
        if payload.value:
            named.append(payload.value)
        if payload.order_column:
            named.append(payload.order_column)
        named.extend(payload.by or [])
        named.extend(payload.columns or [])
        return [c for c in named if c not in present]

    @staticmethod
    def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
        series = pd.to_numeric(frame[column], errors="coerce")
        if series.notna().sum() == 0:
            raise ValueError(f"column {column!r} holds no numeric values")
        return series

    @staticmethod
    def _require(value: str | None, what: str, operation: str) -> str:
        if not value:
            raise ValueError(f"{operation} needs {what}")
        return value

    # --- operations ------------------------------------------------------

    def _op_describe(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        numeric = frame.apply(lambda s: pd.to_numeric(s, errors="coerce")).dropna(axis=1, how="all")
        if numeric.empty:
            raise ValueError("no numeric columns to describe")
        described = numeric.describe().T.reset_index(names="column")
        return described, f"described {len(described)} numeric column(s) over {len(frame)} rows"

    def _op_group_by(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        by = payload.by or []
        if not by:
            raise ValueError("group_by needs at least one column in `by`")
        value = self._require(payload.value, "a `value` column", "group_by")
        agg = payload.agg or "sum"
        work = frame.copy()
        work[value] = self._numeric(work, value)
        grouped = work.groupby(by, dropna=False)[value].agg(agg).reset_index()
        grouped = grouped.sort_values(value, ascending=False)
        return grouped, f"{agg} of {value} by {', '.join(by)}: {len(grouped)} group(s)"

    def _op_share_of_total(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        value = self._require(payload.value, "a `value` column", "share_of_total")
        work = frame.copy()
        work[value] = self._numeric(work, value)
        if payload.by:
            work = work.groupby(payload.by, dropna=False)[value].sum().reset_index()
        total = float(work[value].sum())
        if total == 0:
            raise ValueError(f"the total of {value} is zero, so shares are undefined")
        work["share"] = work[value] / total
        work = work.sort_values("share", ascending=False)
        work["cumulative_share"] = work["share"].cumsum()
        return work, (
            f"shares of {value} (total {total:,.2f}); "
            f"largest row is {float(work['share'].iloc[0]):.1%}"
        )

    def _op_period_over_period(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        value = self._require(payload.value, "a `value` column", "period_over_period")
        order = payload.order_column or str(frame.columns[0])
        work = frame.copy()
        work[value] = self._numeric(work, value)
        work = work.sort_values(order)
        work["previous"] = work[value].shift(1)
        work["change"] = work[value] - work["previous"]
        work["pct_change"] = work["change"] / work["previous"].replace(0, pd.NA)
        moves = work.dropna(subset=["pct_change"])
        if moves.empty:
            return work, f"{value} over {order}: only one period, so no change to report"
        biggest = moves.loc[moves["pct_change"].abs().idxmax()]
        return work, (
            f"{value} over {order}: largest move {float(biggest['pct_change']):+.1%} "
            f"at {order}={biggest[order]!r}"
        )

    def _op_rolling(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        value = self._require(payload.value, "a `value` column", "rolling")
        order = payload.order_column or str(frame.columns[0])
        window = payload.window or 3
        if window < 2:
            raise ValueError("window must be at least 2")
        agg = payload.agg or "mean"
        work = frame.copy()
        work[value] = self._numeric(work, value)
        work = work.sort_values(order)
        work[f"rolling_{agg}_{window}"] = work[value].rolling(window, min_periods=1).agg(agg)
        return work, f"rolling {agg} of {value} over {window} rows, ordered by {order}"

    def _op_correlation(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        candidates = payload.columns or [str(c) for c in frame.columns]
        numeric = frame[candidates].apply(lambda s: pd.to_numeric(s, errors="coerce"))
        numeric = numeric.dropna(axis=1, how="all")
        if numeric.shape[1] < 2:
            raise ValueError("correlation needs at least two numeric columns")
        matrix = numeric.corr().round(4).reset_index(names="column")

        strongest = numeric.corr().abs().where(lambda m: m < 1.0).stack().sort_values(ascending=False)
        note = ""
        if not strongest.empty:
            pair, strength = strongest.index[0], float(strongest.iloc[0])
            note = f"; strongest pair {pair[0]} vs {pair[1]} at {strength:.2f}"
        return matrix, (
            f"correlation across {numeric.shape[1]} numeric column(s){note}. Association only - "
            "test any causal story with a query that could refute it"
        )

    def _op_top_n(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        value = self._require(payload.value, "a `value` column", "top_n")
        n = payload.top_n or 10
        if n < 1:
            raise ValueError("top_n must be at least 1")
        work = frame.copy()
        work[value] = self._numeric(work, value)
        if payload.by:
            work = work.groupby(payload.by, dropna=False)[value].sum().reset_index()
        ranked = work.sort_values(value, ascending=False).head(n)
        total = float(work[value].sum())
        covered = float(ranked[value].sum()) / total if total else 0.0
        return ranked, f"top {len(ranked)} by {value}, covering {covered:.1%} of the total"

    def _op_linear_fit(
        self, payload: PythonAnalysisInput, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame, str]:
        value = self._require(payload.value, "a `value` column", "linear_fit")
        order = payload.order_column or str(frame.columns[0])
        work = frame.copy()
        work[value] = self._numeric(work, value)
        series = work.sort_values(order)[value].dropna().reset_index(drop=True)
        if len(series) < 3:
            raise ValueError("linear_fit needs at least three points")

        x = pd.Series(range(len(series)), dtype="float64")
        y = series.astype("float64")
        slope = float(x.cov(y) / x.var())
        intercept = float(y.mean() - slope * x.mean())
        r = float(x.corr(y))
        result = pd.DataFrame(
            [
                {
                    "slope_per_step": round(slope, 6),
                    "intercept": round(intercept, 6),
                    "r_squared": round(r * r, 6),
                    "points": len(series),
                    "ordered_by": order,
                }
            ]
        )
        direction = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
        return result, (
            f"{value} is {direction} by {slope:,.3f} per step over {len(series)} points "
            f"(r-squared {r * r:.3f}), ordered by {order}"
        )


def _records(frame: pd.DataFrame) -> list[dict]:
    from analyst_agent.tools.sql_runner import _jsonable

    return _jsonable(frame)
