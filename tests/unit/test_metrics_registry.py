"""The approved-metric registry.

The property this file exists to protect: **the agent cannot invent a metric formula.** A term
either resolves to a reviewed definition or comes back as `NotApproved`, and a rendered metric
is assembled from reviewed parts with values bound as parameters — so no free text from the
model reaches SQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from analyst_agent.metrics.loader import MetricDefinition, load_definition, load_definitions
from analyst_agent.metrics.registry import MetricRegistry, NotApproved, get_registry


@pytest.fixture(scope="module")
def registry() -> MetricRegistry:
    return get_registry()


# --- the definitions themselves --------------------------------------------


def test_every_definition_loads_and_validates() -> None:
    definitions = load_definitions()
    assert len(definitions) >= 12
    assert len({d.name for d in definitions}) == len(definitions)


def test_every_definition_documents_itself(registry: MetricRegistry) -> None:
    """A definition nobody can read is not reviewable, which defeats the point of the layer."""
    for definition in registry.all():
        assert definition.description.strip(), f"{definition.name} has no description"
        assert definition.owner.strip(), f"{definition.name} has no owner"
        assert definition.grain.strip(), f"{definition.name} has no grain"
        assert definition.caveats, f"{definition.name} states no caveats"


def test_every_definition_has_at_least_one_alias(registry: MetricRegistry) -> None:
    """People do not ask for `on_time_delivery_rate`; they ask about delivery performance."""
    for definition in registry.all():
        assert definition.aliases, f"{definition.name} has no aliases"


def test_a_definition_missing_its_shape_fields_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "broken",
                "version": 1,
                "title": "Broken",
                "description": "no measure",
                "owner": "nobody",
                "grain": "order",
                "unit": "count",
                "shape": "aggregate",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing"):
        load_definition(path)


def test_a_custom_metric_cannot_also_declare_aggregate_fields(tmp_path: Path) -> None:
    path = tmp_path / "mixed.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "mixed",
                "version": 1,
                "title": "Mixed",
                "description": "both shapes at once",
                "owner": "nobody",
                "grain": "order",
                "unit": "count",
                "shape": "custom",
                "custom_sql": "SELECT 1",
                "measure": "count(*)",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not also define"):
        load_definition(path)


def test_a_custom_metric_cannot_declare_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "dims.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "dims",
                "version": 1,
                "title": "Dims",
                "description": "custom with dimensions",
                "owner": "nobody",
                "grain": "order",
                "unit": "count",
                "shape": "custom",
                "custom_sql": "SELECT 1",
                "dimensions": {"month": {"sql": "1", "label": "Month"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot declare dimensions"):
        load_definition(path)


def test_a_metric_name_must_be_snake_case(tmp_path: Path) -> None:
    path = tmp_path / "bad_name.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Revenue Metric",
                "version": 1,
                "title": "x",
                "description": "x",
                "owner": "x",
                "grain": "order",
                "unit": "count",
                "shape": "custom",
                "custom_sql": "SELECT 1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lower_snake_case"):
        load_definition(path)


def test_an_unknown_yaml_field_is_rejected(tmp_path: Path) -> None:
    """extra=forbid, so a typo in a field name fails at startup rather than being ignored."""
    path = tmp_path / "typo.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "typo",
                "version": 1,
                "title": "x",
                "description": "x",
                "owner": "x",
                "grain": "order",
                "unit": "count",
                "shape": "custom",
                "custom_sql": "SELECT 1",
                "caveat": ["singular, and therefore a typo"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="caveat"):
        load_definition(path)


# --- resolution -------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("revenue", "revenue"),
        ("Revenue", "revenue"),
        ("  sales  ", "revenue"),
        ("net sales", "revenue"),
        ("GMV", "revenue"),
        ("total revenue", "revenue"),
        ("average order value", "aov"),
        ("basket size", "aov"),
        ("aov", "aov"),
        ("order volume", "orders"),
        ("cancel rate", "cancellation_rate"),
        ("delivery performance", "on_time_delivery_rate"),
        ("otd", "on_time_delivery_rate"),
        ("lead time", "avg_delivery_days"),
        ("csat", "avg_review_score"),
        ("repeat rate", "repeat_customer_rate"),
        ("retention", "repeat_customer_rate"),
        ("seller concentration", "seller_concentration"),
        ("freight share", "freight_ratio"),
        # written with underscores, as a model often will
        ("on_time_delivery_rate", "on_time_delivery_rate"),
        # extra internal whitespace
        ("average   order    value", "aov"),
    ],
)
def test_business_terms_resolve_to_the_right_metric(
    registry: MetricRegistry, term: str, expected: str
) -> None:
    found = registry.lookup(term)
    assert isinstance(found, MetricDefinition), f"{term!r} did not resolve"
    assert found.name == expected


@pytest.mark.parametrize(
    "term",
    [
        "customer lifetime value",
        "ltv",
        "churn",
        "net promoter score",
        "gross margin",
        "profit",
        "conversion rate",
        "market share",
    ],
)
def test_an_unapproved_term_is_refused_rather_than_guessed(
    registry: MetricRegistry, term: str
) -> None:
    """The whole point of the layer: no approved definition means no invented formula."""
    found = registry.lookup(term)
    assert isinstance(found, NotApproved)
    assert term in found.message
    assert "no approved definition" in found.message


def test_a_near_miss_gets_suggestions(registry: MetricRegistry) -> None:
    """A useful refusal beats a blank one: the agent can ask a specific question."""
    found = registry.lookup("revenu")
    assert isinstance(found, NotApproved)
    assert "revenue" in found.closest_matches


def test_get_raises_for_an_unknown_metric_name(registry: MetricRegistry) -> None:
    with pytest.raises(KeyError, match="no approved metric"):
        registry.get("does_not_exist")


def test_duplicate_names_are_rejected() -> None:
    definition = load_definitions()[0]
    with pytest.raises(ValueError, match="duplicate metric name"):
        MetricRegistry([definition, definition])


def test_an_ambiguous_alias_is_rejected() -> None:
    """An alias mapping to two metrics would silently answer the wrong question."""
    revenue, orders = (
        next(d for d in load_definitions() if d.name == "revenue"),
        next(d for d in load_definitions() if d.name == "orders"),
    )
    clashing = orders.model_copy(update={"aliases": [*orders.aliases, "gmv"]})
    with pytest.raises(ValueError, match="maps to both"):
        MetricRegistry([revenue, clashing])


# --- rendering --------------------------------------------------------------


def test_rendering_carries_the_definition_version(registry: MetricRegistry) -> None:
    """An answer cites the definition it used, not just a number."""
    rendered = registry.render("revenue", dimensions=["month"])
    assert rendered.definition_version == "revenue@v1"
    assert rendered.caveats
    assert rendered.unit == "currency"


def test_a_date_range_becomes_bound_parameters(registry: MetricRegistry) -> None:
    """Values are never interpolated into the statement."""
    rendered = registry.render("revenue", date_from="2018-01-01", date_to="2018-04-01")
    assert "%(date_from)s" in rendered.sql
    assert "%(date_to)s" in rendered.sql
    assert rendered.params == {"date_from": "2018-01-01", "date_to": "2018-04-01"}
    assert "2018-01-01" not in rendered.sql


def test_a_dimension_filter_value_is_bound_not_interpolated(registry: MetricRegistry) -> None:
    rendered = registry.render("revenue", dimension_filters={"month": "2018-03"})
    assert "%(dim_month)s" in rendered.sql
    assert rendered.params == {"dim_month": "2018-03"}
    assert "2018-03" not in rendered.sql


def test_a_sql_injection_attempt_in_a_filter_value_stays_a_value(
    registry: MetricRegistry,
) -> None:
    """A hostile filter value cannot become SQL, because it is never part of the statement."""
    hostile = "2018-03'; DROP TABLE analytics.orders; --"
    rendered = registry.render("revenue", dimension_filters={"month": hostile})
    assert "DROP" not in rendered.sql
    assert rendered.params["dim_month"] == hostile


def test_an_undeclared_dimension_is_refused(registry: MetricRegistry) -> None:
    """The model chooses a dimension name; a name it invents is an error, not a passthrough."""
    with pytest.raises(KeyError, match="does not declare dimension"):
        registry.render("revenue", dimensions=["customer_email"])
    with pytest.raises(KeyError, match="does not declare dimension"):
        registry.render("revenue", dimension_filters={"nonsense": "x"})


def test_only_the_joins_a_dimension_needs_are_added(registry: MetricRegistry) -> None:
    plain = registry.render("revenue", dimensions=["month"])
    assert "analytics.products" not in plain.sql
    assert "analytics.sellers" not in plain.sql

    with_category = registry.render("revenue", dimensions=["product_category"])
    assert "analytics.products p" in with_category.sql
    assert "analytics.sellers" not in with_category.sql


def test_a_join_is_not_duplicated_when_two_dimensions_share_it(
    registry: MetricRegistry,
) -> None:
    rendered = registry.render(
        "revenue",
        dimensions=["product_category"],
        dimension_filters={"product_category": "brinquedos"},
    )
    assert rendered.sql.count("JOIN analytics.products") == 1


def test_multiple_dimensions_group_and_order_by_position(registry: MetricRegistry) -> None:
    rendered = registry.render("revenue", dimensions=["month", "product_category"])
    assert "GROUP BY 1, 2" in rendered.sql
    assert "ORDER BY 1, 2" in rendered.sql


def test_order_by_measure_ranks_by_the_metric(registry: MetricRegistry) -> None:
    rendered = registry.render("revenue", dimensions=["product_category"], order_by_measure=True)
    assert "ORDER BY 2 DESC" in rendered.sql


def test_a_custom_metric_cannot_be_broken_down(registry: MetricRegistry) -> None:
    with pytest.raises(ValueError, match="cannot be broken down"):
        registry.render("repeat_customer_rate", dimensions=["month"])


def test_a_custom_metric_still_takes_a_date_window(registry: MetricRegistry) -> None:
    rendered = registry.render("repeat_customer_rate", date_from="2018-01-01")
    assert "%(date_from)s" in rendered.sql
    assert rendered.params["date_from"] == "2018-01-01"


def test_the_repeat_customer_metric_never_projects_the_person_key(
    registry: MetricRegistry,
) -> None:
    """It groups on customer_unique_id but must not return it - that is what keeps it inside
    the sensitive-column policy rather than needing approval on every run."""
    rendered = registry.render("repeat_customer_rate")
    assert "GROUP BY" in rendered.sql
    assert "customer_unique_id" in rendered.sql
    # the outermost projection is the rate and a count, not the key
    final_select = rendered.sql.rsplit("SELECT", 1)[1]
    assert "customer_unique_id" not in final_select


# --- the generated catalogue ------------------------------------------------


def test_the_generated_catalogue_is_up_to_date() -> None:
    """A hand-edited catalogue that disagrees with the registry is worse than none: a reviewer
    would be checking the agent's arithmetic against the wrong formula."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, str(root / "scripts" / "generate_metrics_catalog.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_every_metric_appears_in_the_catalogue(registry: MetricRegistry) -> None:
    catalogue = (
        Path(__file__).resolve().parents[2] / "docs" / "metrics-catalog.md"
    ).read_text(encoding="utf-8")
    for definition in registry.all():
        assert f"`{definition.name}`" in catalogue
        assert definition.title in catalogue
