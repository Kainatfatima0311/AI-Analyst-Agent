"""What objects and columns exist, so the guard can allow-list them.

The guard needs a catalog for three separate jobs: rejecting an object that is not in an
allowed schema, expanding ``SELECT *`` well enough to know whether it would return a sensitive
column, and resolving an unqualified column reference back to the table it came from.

Two sources, deliberately:

* ``load_catalog()`` reads ``information_schema`` at runtime, so the guard tracks the database
  it is actually pointed at.
* ``STATIC_CATALOG`` is a committed snapshot, used by the unit tests so that the 60-plus
  hostile-query suite runs with no database at all — the security regression net must be
  runnable on any machine and in CI without a service container.

Drift between the two is a test failure, not a silent divergence: see
``tests/integration/test_catalog_matches_database.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from analyst_agent.config import get_settings


@dataclass(frozen=True)
class Catalog:
    """Objects and their columns, keyed by schema."""

    schemas: frozenset[str]
    objects: dict[str, frozenset[str]]
    """schema -> object names (tables and views)."""
    columns: dict[tuple[str, str], frozenset[str]]
    """(schema, object) -> column names."""

    def has_object(self, schema: str, name: str) -> bool:
        return name in self.objects.get(schema, frozenset())

    def columns_of(self, schema: str, name: str) -> frozenset[str]:
        return self.columns.get((schema, name), frozenset())

    def find_object(self, name: str) -> tuple[str, str] | None:
        """Resolve an unqualified object name against the allowed schemas.

        Returns None when the name is unknown or ambiguous across schemas; the caller rejects
        in both cases, because guessing which table was meant is exactly the wrong instinct
        for a security check.
        """
        matches = [schema for schema in sorted(self.schemas) if self.has_object(schema, name)]
        return (matches[0], name) if len(matches) == 1 else None

    def tables_with_column(self, schema: str, column: str) -> tuple[str, ...]:
        """Every object in a schema that has this column, for unqualified reference resolution."""
        return tuple(
            sorted(
                obj
                for obj in self.objects.get(schema, frozenset())
                if column in self.columns_of(schema, obj)
            )
        )


_ANALYTICS_COLUMNS: dict[str, frozenset[str]] = {
    "customer_contact": frozenset({
        "customer_id", "full_name", "email", "phone", "street_address"
    }),
    "customers": frozenset({
        "customer_id", "customer_unique_id", "customer_zip_code_prefix",
        "customer_city", "customer_state"
    }),
    "dim_date": frozenset({
        "date_key", "year", "quarter", "month", "month_name", "year_month", "day",
        "day_of_week", "iso_week", "is_weekend"
    }),
    "geolocation": frozenset({
        "geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng",
        "geolocation_city", "geolocation_state"
    }),
    "order_items": frozenset({
        "order_id", "order_item_id", "product_id", "seller_id",
        "shipping_limit_date", "price", "freight_value"
    }),
    "orders": frozenset({
        "order_id", "customer_id", "order_status", "order_purchase_timestamp",
        "order_approved_at", "order_delivered_carrier_date",
        "order_delivered_customer_date", "order_estimated_delivery_date"
    }),
    "payments": frozenset({
        "order_id", "payment_sequential", "payment_type", "payment_installments",
        "payment_value"
    }),
    "product_category_name_translation": frozenset({
        "product_category_name", "product_category_name_english"
    }),
    "products": frozenset({
        "product_id", "product_category_name", "product_name_lenght",
        "product_description_lenght", "product_photos_qty", "product_weight_g",
        "product_length_cm", "product_height_cm", "product_width_cm"
    }),
    "reviews": frozenset({
        "review_id", "order_id", "review_score", "review_comment_title",
        "review_comment_message", "review_creation_date", "review_answer_timestamp"
    }),
    "sellers": frozenset({
        "seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"
    }),
    "v_order_revenue": frozenset({
        "order_id", "customer_id", "order_status", "order_purchase_timestamp",
        "purchase_date", "year_month", "order_delivered_customer_date",
        "order_estimated_delivery_date", "item_count", "item_revenue",
        "freight_revenue", "gross_revenue"
    }),}

STATIC_CATALOG = Catalog(
    schemas=frozenset({"analytics"}),
    objects={"analytics": frozenset(_ANALYTICS_COLUMNS)},
    columns={("analytics", obj): cols for obj, cols in _ANALYTICS_COLUMNS.items()},
)


def build_catalog(rows: list[tuple[str, str, str]]) -> Catalog:
    """Build a Catalog from (schema, object, column) rows."""
    objects: dict[str, set[str]] = {}
    columns: dict[tuple[str, str], set[str]] = {}
    for schema, obj, column in rows:
        objects.setdefault(schema, set()).add(obj)
        columns.setdefault((schema, obj), set()).add(column)
    return Catalog(
        schemas=frozenset(objects),
        objects={schema: frozenset(names) for schema, names in objects.items()},
        columns={key: frozenset(cols) for key, cols in columns.items()},
    )


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """Read the catalog from the database for the configured allowed schemas.

    Cached: the analytical schema does not change while the service is running, and a fresh
    catalog query per validated statement would be a needless round trip on the hot path.
    Call ``load_catalog.cache_clear()`` after a migration.
    """
    from analyst_agent.db.engine import rw_conn

    schemas = list(get_settings().allowed_schemas)
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_schema, c.table_name, c.column_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = ANY(%s) AND t.table_type IN ('BASE TABLE', 'VIEW')
            """,
            (schemas,),
        )
        rows = [(r["table_schema"], r["table_name"], r["column_name"]) for r in cur.fetchall()]
    if not rows:
        raise RuntimeError(
            f"no objects found in schemas {schemas}. Has the database been seeded? "
            "Run `python scripts/seed_db.py`."
        )
    return build_catalog(rows)
