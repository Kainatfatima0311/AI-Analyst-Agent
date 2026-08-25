"""The approved-metric registry: resolve a business term, then build its SQL.

Two jobs, and the second one is where the guarantee lives.

**Resolution.** ``lookup`` maps a term to a definition, or returns ``NotApproved`` with the
closest names it knows. It never guesses. If a question names a metric with no approved
definition, the agent must ask, or say plainly that it is using an ad-hoc definition - it may
not quietly invent a formula and present the number as if it were the company's.

**Rendering.** For an ``aggregate`` metric the registry assembles the statement itself from the
definition's reviewed parts: the measure, the FROM, the filter, and the SQL expression attached
to each named dimension. Values - dates, dimension filters - travel as bound parameters. So for
an approved metric no free text from the model reaches SQL at all: the model chooses names, and
names map to expressions a human wrote and reviewed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from analyst_agent.metrics.loader import MetricDefinition, load_definitions
from analyst_agent.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class NotApproved:
    """No approved definition exists for this term.

    Carries suggestions so the agent can ask a useful question rather than a blank one.
    """

    term: str
    closest_matches: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        base = f"there is no approved definition for {self.term!r}"
        if self.closest_matches:
            return f"{base}; did you mean {', '.join(self.closest_matches)}?"
        return base


@dataclass(frozen=True)
class RenderedMetric:
    """A metric turned into an executable statement, plus its provenance.

    ``definition_version`` is what a conclusion cites, so an answer names the definition it used
    rather than merely reporting a number.
    """

    metric: str
    definition_version: str
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    dimensions: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    unit: str = ""


class MetricRegistry:
    """Every approved metric, indexed by name and alias."""

    def __init__(self, definitions: list[MetricDefinition]) -> None:
        self._by_name: dict[str, MetricDefinition] = {}
        self._by_key: dict[str, MetricDefinition] = {}

        for definition in definitions:
            if definition.name in self._by_name:
                raise ValueError(f"duplicate metric name: {definition.name}")
            self._by_name[definition.name] = definition

            for key in definition.lookup_keys:
                normalised = key.strip().lower()
                existing = self._by_key.get(normalised)
                if existing is not None and existing.name != definition.name:
                    # An ambiguous alias is worse than a missing one: it would silently answer
                    # a question about one metric with another.
                    raise ValueError(
                        f"alias {normalised!r} maps to both {existing.name!r} and "
                        f"{definition.name!r}"
                    )
                self._by_key[normalised] = definition

        log.info("metric registry loaded", metrics=len(self._by_name), lookup_keys=len(self._by_key))

    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def all(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._by_name[name] for name in self.names)

    def get(self, name: str) -> MetricDefinition:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"no approved metric named {name!r}") from exc

    def lookup(self, term: str) -> MetricDefinition | NotApproved:
        """Resolve a business term. Returns NotApproved rather than a best guess."""
        normalised = " ".join(term.strip().lower().split())
        found = self._by_key.get(normalised) or self._by_key.get(normalised.replace(" ", "_"))
        if found is not None:
            return found

        suggestions = difflib.get_close_matches(normalised, self._by_key.keys(), n=3, cutoff=0.6)
        # Report metric names rather than whichever alias happened to match.
        names = tuple(dict.fromkeys(self._by_key[s].name for s in suggestions))
        log.info("metric not approved", term=term, suggestions=names)
        return NotApproved(term=term, closest_matches=names)

    # --- rendering -------------------------------------------------------

    def render(
        self,
        name: str,
        dimensions: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        dimension_filters: dict[str, str] | None = None,
        order_by_measure: bool = False,
    ) -> RenderedMetric:
        """Build the statement for an approved metric.

        ``dimensions`` and the keys of ``dimension_filters`` must be dimension *names* the
        definition declares; anything else raises. Filter values are bound parameters and are
        never interpolated into the statement.
        """
        definition = self.get(name)
        dimensions = dimensions or []
        dimension_filters = dimension_filters or {}

        # Checked before the unknown-dimension check, so a custom metric gets the message that
        # explains why no dimension will work rather than "available: none".
        if definition.shape == "custom" and (dimensions or dimension_filters):
            raise ValueError(
                f"metric {name!r} is a custom statement and cannot be broken down; "
                "it owns its own grouping"
            )

        unknown = [d for d in [*dimensions, *dimension_filters] if d not in definition.dimensions]
        if unknown:
            raise KeyError(
                f"metric {name!r} does not declare dimension(s) {', '.join(sorted(unknown))}; "
                f"available: {', '.join(sorted(definition.dimensions)) or 'none'}"
            )

        if definition.shape == "custom":
            if definition.custom_sql is None:  # guaranteed by the loader
                raise ValueError(f"metric {name!r} has no custom_sql")
            return RenderedMetric(
                metric=definition.name,
                definition_version=definition.qualified_version,
                sql=definition.custom_sql.strip(),
                params={"date_from": date_from, "date_to": date_to},
                caveats=tuple(definition.caveats),
                unit=definition.unit,
            )

        return self._render_aggregate(
            definition, dimensions, date_from, date_to, dimension_filters, order_by_measure
        )

    def _render_aggregate(
        self,
        definition: MetricDefinition,
        dimensions: list[str],
        date_from: str | None,
        date_to: str | None,
        dimension_filters: dict[str, str],
        order_by_measure: bool,
    ) -> RenderedMetric:
        measure = (definition.measure or "").strip()
        from_sql = (definition.from_sql or "").strip()
        date_column = definition.date_column or ""

        select_parts = [f"{definition.dimensions[d].sql} AS {d}" for d in dimensions]
        select_parts.append(f"{measure} AS {definition.name}")

        # Only the joins the chosen dimensions actually need, deduplicated. Carrying every
        # possible join would slow the common case and risk changing the row count through a
        # join that fans out.
        joins: list[str] = []
        for dimension_name in [*dimensions, *dimension_filters]:
            join = definition.dimensions[dimension_name].join
            if join and join not in joins:
                joins.append(join)

        where: list[str] = []
        params: dict[str, Any] = {}
        if definition.where_sql:
            where.append(f"({definition.where_sql})")
        if date_from is not None:
            where.append(f"{date_column} >= %(date_from)s::timestamp")
            params["date_from"] = date_from
        if date_to is not None:
            where.append(f"{date_column} < %(date_to)s::timestamp")
            params["date_to"] = date_to
        for dimension_name, value in sorted(dimension_filters.items()):
            placeholder = f"dim_{dimension_name}"
            where.append(f"{definition.dimensions[dimension_name].sql} = %({placeholder})s")
            params[placeholder] = value

        lines = [f"SELECT {', '.join(select_parts)}", f"FROM {from_sql}"]
        lines.extend(joins)
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if dimensions:
            group_by = ", ".join(str(i + 1) for i in range(len(dimensions)))
            lines.append(f"GROUP BY {group_by}")
            lines.append(
                f"ORDER BY {len(dimensions) + 1} DESC" if order_by_measure else f"ORDER BY {group_by}"
            )

        return RenderedMetric(
            metric=definition.name,
            definition_version=definition.qualified_version,
            sql="\n".join(lines),
            params=params,
            dimensions=tuple(dimensions),
            caveats=tuple(definition.caveats),
            unit=definition.unit,
        )


@lru_cache(maxsize=1)
def get_registry(directory: Path | None = None) -> MetricRegistry:
    """Cached registry. Definitions do not change while the service is running."""
    return MetricRegistry(load_definitions(directory))
