"""Tool 5: turn a query result into a chart.

Every chart carries the ``query_id`` it was built from, so a chart in the UI is one click from
the SQL behind it. That is the whole reason charts go through a tool rather than being drawn from
whatever the model last said.

Three rules are enforced here rather than left to the model's judgement:

* **No dual axis.** Two measures on two y-scales is the most common way to make a chart lie. A
  request for it is refused with the alternatives (two charts, or index both to a common base).
* **Categorical hues in fixed slot order, never cycled.** Past the eighth series the tail folds
  into "Other", and the fold is *reported* - a silent cap reads as "this is everything" when it
  is not. Scatter compares every pair at once, so it caps at three series (see ``palette.py``).
* **A legend for two or more series, none for one.** With one series the title already names it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import pandas as pd
import plotly.graph_objects as go
from pydantic import BaseModel, Field

from analyst_agent.db import repository as repo
from analyst_agent.observability.logging import get_logger
from analyst_agent.tools.base import Tool, ToolResult
from analyst_agent.tools.frames import FrameNotAvailableError, get_store
from analyst_agent.tools.palette import (
    MAX_SERIES,
    MAX_SERIES_ALL_PAIRS,
    OTHER_COLOUR,
    OTHER_LABEL,
    theme,
)

log = get_logger(__name__)

ChartType = Literal[
    "line", "bar", "grouped_bar", "stacked_bar", "area", "scatter", "donut"
]

# Part-to-whole. Every slice touches two others, so the ring carries a surface gap and the
# legend carries the label *and* the share - identity is never left to the colour alone.
PART_TO_WHOLE_FORMS = {"donut"}

# Forms where every series is compared against every other, not just its neighbour.
ALL_PAIRS_FORMS = {"scatter"}


class ChartBuilderInput(BaseModel):
    model_config = {"extra": "forbid"}

    query_id: str = Field(description="The query_id returned by a previous sql_runner call.")
    chart_type: ChartType = Field(
        description=(
            "line or area for a value over time; bar for magnitude across categories; "
            "grouped_bar or stacked_bar to add a second dimension; scatter for the "
            "relationship between two numeric columns; donut for how one total splits "
            "between a handful of categories."
        )
    )
    x: str = Field(description="Column for the horizontal axis - the time or category column.")
    y: str = Field(description="Numeric column for the vertical axis.")
    series: str | None = Field(
        default=None,
        description=(
            "Optional column to split into series. Leave null for a single series. Do not pass "
            "a second measure here - use one axis per chart."
        ),
    )
    title: str = Field(description="A title that states what the chart shows, not just its axes.")


class ChartBuilderTool(Tool[ChartBuilderInput]):
    name = "chart_builder"
    description = """
Build a chart from the result of a previous query.

Pass the query_id from sql_runner, the chart type, and the columns for each axis. The chart is
stored against that query_id, so whoever reads the answer can get from the picture to the SQL.

One measure per chart. There is no two-y-axis option: if you want to show two measures of
different scale, either build two charts or index both to a common base. A request for a second
measure on a second axis is refused.

Series colours are assigned in a fixed order. Past eight series the smallest fold into 'Other'
and the summary tells you so - do not treat a folded chart as showing every category. Scatter
caps at three series.

donut shows how one total divides between categories: pass the category column as x and the
measure as y, and leave series null. Each slice's share appears in the legend beside its label.
Use it only for a genuine part-to-whole - a handful of shares of one total - not to compare
independent quantities, and never with a measure that can go negative.

If the columns are not suitable - a text column on the y axis, a scatter against a category -
this refuses and suggests a chart type that fits.
"""
    input_model = ChartBuilderInput

    def run(
        self, payload: ChartBuilderInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        try:
            query_uuid = uuid.UUID(payload.query_id)
        except ValueError:
            return ToolResult.refuse(f"{payload.query_id!r} is not a valid query_id")

        try:
            frame = get_store().get(query_uuid)
        except FrameNotAvailableError as exc:
            return ToolResult.refuse(
                f"no result available for query {payload.query_id}: {exc}",
                guidance="Run the query with sql_runner first, then pass its query_id.",
            )

        if frame.empty:
            return ToolResult.refuse(
                "that query returned no rows, so there is nothing to chart",
                query_id=payload.query_id,
            )

        present = {str(c) for c in frame.columns}
        missing = [c for c in (payload.x, payload.y, payload.series) if c and c not in present]
        if missing:
            return ToolResult.refuse(
                f"column(s) not in that result: {', '.join(missing)}",
                available_columns=sorted(present),
            )

        work = frame.copy()
        work[payload.y] = pd.to_numeric(work[payload.y], errors="coerce")
        if work[payload.y].notna().sum() == 0:
            return ToolResult.refuse(
                f"{payload.y!r} holds no numeric values, so it cannot be the vertical axis",
                guidance="Pick a numeric column for y, or aggregate first with python_analysis.",
            )

        if payload.chart_type == "donut" and (work[payload.y] < 0).any():
            # A share of a total is meaningless once a part is negative - the slices would not
            # sum to the whole, and a reader has no way to see that from the picture.
            return ToolResult.refuse(
                f"{payload.y!r} contains negative values, so it cannot be shown as a share "
                "of a total",
                guidance="Use bar, which can show a negative magnitude honestly.",
            )

        if payload.chart_type == "scatter":
            work[payload.x] = pd.to_numeric(work[payload.x], errors="coerce")
            if work[payload.x].notna().sum() == 0:
                return ToolResult.refuse(
                    f"scatter needs a numeric x axis, and {payload.x!r} is categorical",
                    guidance="Use bar for a category on the x axis.",
                )

        if payload.chart_type == "donut":
            return self._deliver_donut(payload, work, run_id, query_uuid)

        groups, folded = self._series(work, payload)
        cap = MAX_SERIES_ALL_PAIRS if payload.chart_type in ALL_PAIRS_FORMS else MAX_SERIES
        if folded and payload.chart_type in ALL_PAIRS_FORMS:
            note = (
                f"scatter compares every pair of colours at once, so it is capped at {cap} "
                f"series; {folded} smaller one(s) were folded into '{OTHER_LABEL}'"
            )
        elif folded:
            note = (
                f"{folded} smaller series were folded into '{OTHER_LABEL}' - the chart shows "
                f"the largest {cap}, not every category"
            )
        else:
            note = ""

        figure = self._figure(payload, groups)
        chart_id = repo.record_chart(
            run_id=run_id,
            query_id=query_uuid,
            chart_type=payload.chart_type,
            spec=_spec(figure),
            title=payload.title,
            png=self._png(figure),
        )

        summary = f"{payload.chart_type} of {payload.y} by {payload.x}"
        if payload.series:
            summary += f", split by {payload.series} ({len(groups)} series)"
        if note:
            summary += f". Note: {note}"

        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "chart_id": str(chart_id),
                "query_id": payload.query_id,
                "chart_type": payload.chart_type,
                "title": payload.title,
                "series": [name for name, _ in groups],
                "series_folded": folded,
                "points": int(sum(len(part) for _, part in groups)),
            },
            audit={"chart_id": str(chart_id), "series": len(groups), "folded": folded},
        )

    # --- series ----------------------------------------------------------

    @staticmethod
    def _series(
        frame: pd.DataFrame, payload: ChartBuilderInput
    ) -> tuple[list[tuple[str, pd.DataFrame]], int]:
        """Split into series in a stable order, folding the tail if there are too many.

        Ordered by total magnitude so slot 1 is the largest series, and so the same entity keeps
        the same colour whichever subset is on screen - colour follows the entity, not its rank
        within the current filter.
        """
        if not payload.series:
            return [(payload.y, frame)], 0

        cap = MAX_SERIES_ALL_PAIRS if payload.chart_type in ALL_PAIRS_FORMS else MAX_SERIES
        totals = (
            frame.groupby(payload.series, dropna=False)[payload.y]
            .sum()
            .sort_values(ascending=False)
        )
        keep = [str(name) for name in totals.index[:cap]]
        folded_names = [str(name) for name in totals.index[cap:]]

        groups: list[tuple[str, pd.DataFrame]] = [
            (name, frame[frame[payload.series].astype(str) == name]) for name in keep
        ]
        if folded_names:
            tail = frame[frame[payload.series].astype(str).isin(folded_names)]
            aggregated = (
                tail.groupby(payload.x, dropna=False)[payload.y].sum().reset_index()
            )
            groups.append((OTHER_LABEL, aggregated))
        return groups, len(folded_names)

    # --- figure ----------------------------------------------------------

    def _deliver_donut(
        self,
        payload: ChartBuilderInput,
        work: pd.DataFrame,
        run_id: uuid.UUID,
        query_uuid: uuid.UUID,
    ) -> ToolResult:
        """Record a part-to-whole chart and report what it does and does not show."""
        totals = (
            work.groupby(payload.x, dropna=False)[payload.y].sum().sort_values(ascending=False)
        )
        folded = max(0, len(totals) - MAX_SERIES)
        figure = self._donut(payload, work)
        chart_id = repo.record_chart(
            run_id=run_id,
            query_id=query_uuid,
            chart_type=payload.chart_type,
            spec=_spec(figure),
            title=payload.title,
            png=self._png(figure),
        )

        shown = min(len(totals), MAX_SERIES)
        summary = f"donut of {payload.y} split by {payload.x} ({shown} slices)"
        if folded:
            summary += (
                f". Note: {folded} smaller categories were folded into '{OTHER_LABEL}' - the "
                "chart shows the largest, not every category"
            )
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "chart_id": str(chart_id),
                "query_id": payload.query_id,
                "chart_type": payload.chart_type,
                "title": payload.title,
                "slices": [str(name) for name in totals.index[:MAX_SERIES]],
                "series_folded": folded,
                "points": len(work),
            },
            audit={"chart_id": str(chart_id), "slices": shown, "folded": folded},
        )

    def _donut(self, payload: ChartBuilderInput, work: pd.DataFrame) -> go.Figure:
        """A total split between categories.

        The legend carries ``label - share%`` rather than the slices carrying numbers: a ring of
        percentages is unreadable at small sizes, and putting the share in the legend means the
        chart survives being screenshotted, printed in grey, or read by someone who cannot
        separate two of the hues.
        """
        light = theme("light")
        totals = (
            work.groupby(payload.x, dropna=False)[payload.y].sum().sort_values(ascending=False)
        )
        if len(totals) > MAX_SERIES:
            kept = totals.iloc[:MAX_SERIES]
            folded_total = float(totals.iloc[MAX_SERIES:].sum())
            names = [str(name) for name in kept.index] + [OTHER_LABEL]
            values = [float(v) for v in kept] + [folded_total]
        else:
            names = [str(name) for name in totals.index]
            values = [float(v) for v in totals]

        whole = sum(values) or 1.0
        labels = [f"{name} - {value / whole * 100:.1f}%" for name, value in zip(names, values, strict=True)]
        colours = [
            light.colour(index) if name != OTHER_LABEL else OTHER_COLOUR["light"]
            for index, name in enumerate(names)
        ]

        figure = go.Figure(
            go.Pie(
                labels=labels,
                values=values,
                hole=0.62,
                sort=False,
                direction="clockwise",
                textinfo="none",
                marker={
                    "colors": colours,
                    # The 2px surface ring: adjacent slices read as separate marks.
                    "line": {"color": light.surface, "width": 2},
                },
                hovertemplate="%{label}<br>%{value:,.2f}<extra></extra>",
            )
        )
        figure.update_layout(
            template="plotly_white",
            title={"text": payload.title, "font": {"size": 16, "color": light.text_primary}},
            paper_bgcolor=light.surface,
            plot_bgcolor=light.surface,
            font={"color": light.text_secondary, "size": 12},
            showlegend=True,
            legend={"orientation": "v", "yanchor": "middle", "y": 0.5, "x": 1.0},
            margin={"l": 16, "r": 16, "t": 64, "b": 24},
            meta={
                "dark_theme": {
                    "surface": theme("dark").surface,
                    "text_primary": theme("dark").text_primary,
                    "text_secondary": theme("dark").text_secondary,
                    "series": [theme("dark").colour(i) for i in range(len(names))],
                },
                "query_id": payload.query_id,
            },
        )
        return figure

    def _figure(
        self, payload: ChartBuilderInput, groups: list[tuple[str, pd.DataFrame]]
    ) -> go.Figure:
        light = theme("light")
        figure = go.Figure()

        for index, (name, part) in enumerate(groups):
            colour = light.colour(index)
            figure.add_trace(self._trace(payload, name, part, colour))

        # A legend for two or more series; with one, the title already names it.
        show_legend = len(groups) > 1
        figure.update_layout(
            template="plotly_white",
            title={"text": payload.title, "font": {"size": 16, "color": light.text_primary}},
            paper_bgcolor=light.surface,
            plot_bgcolor=light.surface,
            font={"color": light.text_secondary, "size": 12},
            showlegend=show_legend,
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
            margin={"l": 64, "r": 24, "t": 64, "b": 56},
            hovermode="x unified" if payload.chart_type in ("line", "area") else "closest",
            barmode=(
                "stack"
                if payload.chart_type == "stacked_bar"
                else "group"
                if payload.chart_type == "grouped_bar"
                else "relative"
            ),
        )
        # Recessive grid and axes: the data should be the most prominent thing on the chart.
        figure.update_xaxes(
            title_text=payload.x,
            showgrid=False,
            zeroline=False,
            linecolor=light.axis,
            tickfont={"color": light.text_muted},
        )
        figure.update_yaxes(
            title_text=payload.y,
            gridcolor=light.grid,
            zeroline=False,
            linecolor=light.axis,
            tickfont={"color": light.text_muted},
        )
        # The dark palette travels with the spec so the UI can restyle without re-rendering,
        # rather than flipping the light colours and hoping.
        figure.update_layout(
            meta={
                "dark_theme": {
                    "surface": theme("dark").surface,
                    "text_primary": theme("dark").text_primary,
                    "text_secondary": theme("dark").text_secondary,
                    "grid": theme("dark").grid,
                    "axis": theme("dark").axis,
                    "series": [theme("dark").colour(i) for i in range(len(groups))],
                },
                "query_id": payload.query_id,
            }
        )
        return figure

    @staticmethod
    def _trace(
        payload: ChartBuilderInput, name: str, part: pd.DataFrame, colour: str
    ) -> go.Scatter | go.Bar:
        ordered = part.sort_values(payload.x)
        x, y = ordered[payload.x], ordered[payload.y]

        if payload.chart_type in ("bar", "grouped_bar", "stacked_bar"):
            return go.Bar(
                x=x,
                y=y,
                name=name,
                marker={
                    "color": colour,
                    # A 2px surface gap between adjacent and stacked fills, so segments read as
                    # separate marks rather than one block.
                    "line": {"color": theme("light").surface, "width": 2},
                },
            )
        if payload.chart_type == "scatter":
            return go.Scatter(
                x=x,
                y=y,
                name=name,
                mode="markers",
                marker={
                    "color": colour,
                    "size": 9,
                    "line": {"color": theme("light").surface, "width": 2},
                },
            )
        return go.Scatter(
            x=x,
            y=y,
            name=name,
            mode="lines",
            fill="tozeroy" if payload.chart_type == "area" else None,
            line={"color": colour, "width": 2},
        )

    @staticmethod
    def _png(figure: go.Figure) -> bytes | None:
        """Static image for the report. A rendering failure is not worth losing the chart over."""
        try:
            return bytes(figure.to_image(format="png", width=960, height=540, scale=2))
        except Exception as exc:
            log.warning("png rendering failed", error=str(exc))
            return None


def _spec(figure: go.Figure) -> dict[str, Any]:
    """The figure as plain JSON.

    ``to_plotly_json()`` leaves numpy arrays in place, which psycopg cannot store as jsonb.
    Plotly's own serialiser knows how to flatten them, so round-tripping through it is both
    correct and cheaper than writing a converter.
    """
    return json.loads(figure.to_json())
