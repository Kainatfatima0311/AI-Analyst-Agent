"""Load the raw CSVs in ``db/seed/raw/`` into schema ``analytics``.

Loading strategy, and why it is not a plain ``COPY`` straight into the target table:

* Each file is copied into a **staging table** created with ``LIKE <target>``, which inherits the
  column types and NOT NULL constraints but no primary or foreign keys.
* Rows then move into the target with ``ON CONFLICT DO NOTHING`` and, where a foreign key
  exists, a filter that drops rows whose parent is absent.

The real Olist archive contains duplicate review rows and a handful of orphan references. A
direct ``COPY`` aborts the whole load on the first one; this way the load succeeds, and the
number of rows dropped is reported instead of being silently swallowed.

Run through ``scripts/seed_db.py`` rather than directly — that wrapper resolves the source
first and verifies the result afterwards.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

RAW_DIR = Path(__file__).resolve().parent / "raw"


@dataclass(frozen=True)
class TableSpec:
    """How one CSV file maps onto one analytics table."""

    csv_name: str
    table: str
    conflict: str | None = None
    """Conflict target for ON CONFLICT DO NOTHING, or None for a table without a unique key."""
    required: bool = True
    fk_filters: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    """(local_column, parent_table, parent_column) triples; rows with no parent are dropped."""


# Dependency order matters: parents before children.
TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("olist_customers_dataset.csv", "analytics.customers", conflict="(customer_id)"),
    TableSpec("olist_sellers_dataset.csv", "analytics.sellers", conflict="(seller_id)"),
    TableSpec("olist_products_dataset.csv", "analytics.products", conflict="(product_id)"),
    TableSpec(
        "product_category_name_translation.csv",
        "analytics.product_category_name_translation",
        conflict="(product_category_name)",
    ),
    TableSpec("olist_geolocation_dataset.csv", "analytics.geolocation"),
    TableSpec(
        "customer_contact.csv",
        "analytics.customer_contact",
        conflict="(customer_id)",
        required=False,
        fk_filters=(("customer_id", "analytics.customers", "customer_id"),),
    ),
    TableSpec(
        "olist_orders_dataset.csv",
        "analytics.orders",
        conflict="(order_id)",
        fk_filters=(("customer_id", "analytics.customers", "customer_id"),),
    ),
    TableSpec(
        "olist_order_items_dataset.csv",
        "analytics.order_items",
        conflict="(order_id, order_item_id)",
        fk_filters=(
            ("order_id", "analytics.orders", "order_id"),
            ("product_id", "analytics.products", "product_id"),
            ("seller_id", "analytics.sellers", "seller_id"),
        ),
    ),
    TableSpec(
        "olist_order_payments_dataset.csv",
        "analytics.payments",
        conflict="(order_id, payment_sequential)",
        fk_filters=(("order_id", "analytics.orders", "order_id"),),
    ),
    TableSpec(
        "olist_order_reviews_dataset.csv",
        "analytics.reviews",
        conflict="(review_id, order_id)",
        fk_filters=(("order_id", "analytics.orders", "order_id"),),
    ),
)

# Truncate order is the reverse of the load order.
TRUNCATE_ORDER = (
    "analytics.reviews",
    "analytics.payments",
    "analytics.order_items",
    "analytics.orders",
    "analytics.customer_contact",
    "analytics.geolocation",
    "analytics.product_category_name_translation",
    "analytics.products",
    "analytics.sellers",
    "analytics.customers",
    "analytics.dim_date",
)


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return next(csv.reader(fh))


def table_columns(cur: psycopg.Cursor, table: str) -> set[str]:
    schema, name = table.split(".", 1)
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, name),
    )
    return {row[0] for row in cur.fetchall()}


def load_one(cur: psycopg.Cursor, spec: TableSpec, raw_dir: Path) -> dict[str, int]:
    path = raw_dir / spec.csv_name
    if not path.is_file():
        if spec.required:
            raise FileNotFoundError(f"required CSV missing: {path}")
        print(f"  {spec.table:<48} skipped (optional file {spec.csv_name} absent)")
        return {"read": 0, "inserted": 0, "dropped": 0}

    header = read_header(path)
    known = table_columns(cur, spec.table)
    unknown = [column for column in header if column not in known]
    if unknown:
        raise ValueError(f"{spec.csv_name} has columns not in {spec.table}: {', '.join(unknown)}")

    column_list = ", ".join(f'"{column}"' for column in header)
    staging = f"stg_{spec.table.split('.')[1]}"

    cur.execute(f"CREATE TEMP TABLE {staging} (LIKE {spec.table}) ON COMMIT DROP")
    copy_sql = (
        f"COPY {staging} ({column_list}) FROM STDIN "
        "WITH (FORMAT csv, HEADER true, NULL '')"
    )
    with cur.copy(copy_sql) as copy, path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            copy.write(chunk)

    cur.execute(f"SELECT count(*) FROM {staging}")
    read_rows = int(cur.fetchone()[0])  # type: ignore[index]

    where = " AND ".join(
        f"EXISTS (SELECT 1 FROM {parent} p WHERE p.{parent_col} = s.{local})"
        for local, parent, parent_col in spec.fk_filters
    )
    conflict = f"ON CONFLICT {spec.conflict} DO NOTHING" if spec.conflict else ""
    cur.execute(
        f"INSERT INTO {spec.table} ({column_list}) "
        f"SELECT {column_list} FROM {staging} s "
        f"{'WHERE ' + where if where else ''} "
        f"{conflict}"
    )
    inserted = cur.rowcount
    dropped = read_rows - inserted

    note = f"  ({dropped:,} dropped: duplicate or orphan)" if dropped else ""
    print(f"  {spec.table:<48} {inserted:>9,} rows{note}")
    return {"read": read_rows, "inserted": inserted, "dropped": dropped}


def build_dim_date(cur: psycopg.Cursor) -> int:
    """Fill dim_date with a continuous calendar spanning the loaded orders."""
    cur.execute(
        """
        INSERT INTO analytics.dim_date (
            date_key, year, quarter, month, month_name, year_month,
            day, day_of_week, iso_week, is_weekend
        )
        SELECT
            d::date,
            EXTRACT(YEAR    FROM d)::smallint,
            EXTRACT(QUARTER FROM d)::smallint,
            EXTRACT(MONTH   FROM d)::smallint,
            to_char(d, 'Month'),
            to_char(d, 'YYYY-MM'),
            EXTRACT(DAY   FROM d)::smallint,
            EXTRACT(ISODOW FROM d)::smallint,
            EXTRACT(WEEK  FROM d)::smallint,
            EXTRACT(ISODOW FROM d) >= 6
        FROM generate_series(
            (SELECT date_trunc('month', min(order_purchase_timestamp)) FROM analytics.orders),
            (SELECT date_trunc('month', max(order_purchase_timestamp)) + interval '1 month'
                    - interval '1 day' FROM analytics.orders),
            interval '1 day'
        ) AS d
        ON CONFLICT (date_key) DO NOTHING
        """
    )
    return cur.rowcount


def load(dsn: str | None = None, raw_dir: Path = RAW_DIR, truncate: bool = True) -> dict[str, dict[str, int]]:
    dsn = dsn or os.environ.get("DB_RW_DSN")
    if not dsn:
        raise SystemExit("DB_RW_DSN is not set (copy .env.example to .env and fill it in)")

    stats: dict[str, dict[str, int]] = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("SET search_path = analytics, public")
        with conn.cursor() as cur:
            if truncate:
                print("[load] truncating analytics tables")
                cur.execute(f"TRUNCATE {', '.join(TRUNCATE_ORDER)}")

            print("[load] copying CSVs")
            for spec in TABLE_SPECS:
                stats[spec.table] = load_one(cur, spec, raw_dir)

            rows = build_dim_date(cur)
            print(f"  {'analytics.dim_date':<48} {rows:>9,} rows")
            stats["analytics.dim_date"] = {"read": rows, "inserted": rows, "dropped": 0}

            print("[load] ANALYZE")
            cur.execute("ANALYZE")
        conn.commit()

    total_dropped = sum(s["dropped"] for s in stats.values())
    if total_dropped:
        print(f"[load] {total_dropped:,} rows dropped as duplicates or orphans (reported, not hidden)")
    return stats


if __name__ == "__main__":
    load()
