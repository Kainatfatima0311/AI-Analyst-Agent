"""Loading and validating the approved KPI definitions.

A definition is a YAML file, not a paragraph in a prompt. That difference is the point: a file
is versioned, diffable, reviewable and testable, and a prompt is none of those. The agent may
look a metric up; it may not invent one.

The shape is deliberately **not** "a metric is a blob of SQL". A metric declares an aggregate
expression, the tables it reads, its filter, its date column, and an allow-list of dimensions
with a vetted SQL expression for each. The registry assembles the statement from those parts.
The consequence is structural: for an approved metric, no free text from the model ever reaches
SQL — the model picks a *name*, and named things map to reviewed expressions. Values travel as
bound parameters.

Metrics that genuinely do not fit that mould (a ratio over a subquery, a concentration measure
needing a window) declare ``shape: custom`` and carry their own statement. Those are held to the
same bar a different way: every rendered metric, custom or not, is asserted to pass ``sql_guard``
in the test suite.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

Unit = Literal["currency", "count", "ratio", "days", "score"]
Shape = Literal["aggregate", "custom"]


class Dimension(BaseModel):
    """One dimension a metric may be broken down by.

    ``sql`` is a reviewed expression, so the model chooses ``month`` rather than writing
    ``to_char(...)`` itself.
    """

    model_config = {"extra": "forbid"}

    sql: str
    label: str
    join: str | None = None
    """A JOIN clause this dimension needs, appended only when the dimension is used.

    Without this, every metric's base query would have to carry every join any of its
    dimensions might want - which would both slow the common case and, worse, silently change
    the row count through a join that fans out.
    """
    description: str | None = None


class MetricDefinition(BaseModel):
    """One approved business metric."""

    model_config = {"extra": "forbid"}

    name: str
    version: int = Field(ge=1)
    title: str
    description: str
    owner: str
    grain: str
    unit: Unit
    shape: Shape = "aggregate"

    # aggregate shape
    measure: str | None = None
    from_sql: str | None = None
    where_sql: str | None = None
    date_column: str | None = None

    # custom shape
    custom_sql: str | None = None

    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    sensitive: bool = False

    @field_validator("name")
    @classmethod
    def _name_is_an_identifier(cls, value: str) -> str:
        if not IDENTIFIER.match(value):
            raise ValueError(f"metric name {value!r} must be lower_snake_case")
        return value

    @field_validator("dimensions")
    @classmethod
    def _dimension_names_are_identifiers(cls, value: dict[str, Dimension]) -> dict[str, Dimension]:
        for key in value:
            if not IDENTIFIER.match(key):
                raise ValueError(f"dimension name {key!r} must be lower_snake_case")
        return value

    @field_validator("aliases")
    @classmethod
    def _aliases_are_normalised(cls, value: list[str]) -> list[str]:
        return [alias.strip().lower() for alias in value if alias.strip()]

    @model_validator(mode="after")
    def _shape_is_complete(self) -> MetricDefinition:
        """Each shape requires its own fields and forbids the other's.

        Enforced here rather than trusted, because a half-filled definition would otherwise
        fail much later, as a confusing SQL error during a run.
        """
        if self.shape == "aggregate":
            missing = [
                field
                for field in ("measure", "from_sql", "date_column")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(
                    f"metric {self.name!r} has shape=aggregate and is missing: "
                    f"{', '.join(missing)}"
                )
            if self.custom_sql is not None:
                raise ValueError(
                    f"metric {self.name!r} has shape=aggregate but also defines custom_sql"
                )
        else:
            if not self.custom_sql:
                raise ValueError(f"metric {self.name!r} has shape=custom but no custom_sql")
            if any(
                getattr(self, field) is not None
                for field in ("measure", "from_sql", "where_sql", "date_column")
            ):
                raise ValueError(
                    f"metric {self.name!r} has shape=custom and must not also define the "
                    "aggregate fields"
                )
            if self.dimensions:
                raise ValueError(
                    f"metric {self.name!r} has shape=custom, so it cannot declare dimensions: "
                    "a custom statement owns its own grouping"
                )
        return self

    @property
    def lookup_keys(self) -> tuple[str, ...]:
        """Every term that should resolve to this metric."""
        keys = {self.name, self.name.replace("_", " "), self.title.lower(), *self.aliases}
        return tuple(sorted(keys))

    @property
    def qualified_version(self) -> str:
        """What a conclusion cites, so an answer names the definition it used."""
        return f"{self.name}@v{self.version}"


def load_definition(path: Path) -> MetricDefinition:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} does not contain a YAML mapping")
    try:
        return MetricDefinition.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"{path.name} is not a valid metric definition: {exc}") from exc


def load_definitions(directory: Path | None = None) -> list[MetricDefinition]:
    """Load every definition, failing on the first invalid one.

    Startup is the right place to find a broken definition — not the middle of a run.
    """
    directory = directory or DEFINITIONS_DIR
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise RuntimeError(f"no metric definitions found in {directory}")
    return [load_definition(path) for path in paths]
