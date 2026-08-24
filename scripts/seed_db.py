"""Seed the database end to end: resolve the dataset, load it, verify the result.

    python scripts/seed_db.py                  # auto source: local, then kaggle, then synthetic
    python scripts/seed_db.py --source synthetic
    python scripts/seed_db.py --skip-download  # reload whatever is already in db/seed/raw/

Reads DB_RW_DSN from the environment, falling back to .env in the repository root so that a
fresh clone works without exporting anything by hand.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db" / "seed"))


def load_dotenv(path: Path) -> None:
    """Minimal .env reader — the service itself uses pydantic-settings for this."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


EXPECTED_TABLES = [
    "analytics.customers",
    "analytics.customer_contact",
    "analytics.sellers",
    "analytics.products",
    "analytics.product_category_name_translation",
    "analytics.geolocation",
    "analytics.orders",
    "analytics.order_items",
    "analytics.payments",
    "analytics.reviews",
    "analytics.dim_date",
]

# Tables that must not be empty. customer_contact is excluded on purpose: the real Olist
# archive ships no direct identifiers, so it is legitimately empty on the kaggle path.
MUST_BE_POPULATED = [t for t in EXPECTED_TABLES if t != "analytics.customer_contact"]


def verify(dsn: str) -> int:
    import psycopg

    failures = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        print("\n[verify] row counts")
        for table in EXPECTED_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — fixed table allowlist
            count = int(cur.fetchone()[0])  # type: ignore[index]
            flag = ""
            if table in MUST_BE_POPULATED and count == 0:
                flag = "  <-- EMPTY, expected rows"
                failures += 1
            print(f"  {table:<48} {count:>9,}{flag}")

        cur.execute(
            """
            SELECT to_char(min(order_purchase_timestamp), 'YYYY-MM'),
                   to_char(max(order_purchase_timestamp), 'YYYY-MM')
            FROM analytics.orders
            """
        )
        first, last = cur.fetchone()  # type: ignore[misc]
        print(f"\n[verify] order period: {first} .. {last}")

        # Referential sanity: the loader drops orphans, so nothing should remain.
        cur.execute(
            """
            SELECT count(*) FROM analytics.order_items oi
            LEFT JOIN analytics.orders o ON o.order_id = oi.order_id
            WHERE o.order_id IS NULL
            """
        )
        orphans = int(cur.fetchone()[0])  # type: ignore[index]
        if orphans:
            print(f"[verify] FAIL: {orphans:,} order_items rows have no parent order")
            failures += 1
        else:
            print("[verify] referential integrity: no orphan order_items")

        cur.execute("SELECT count(*) FROM analytics.v_order_revenue")
        print(f"[verify] view analytics.v_order_revenue: {int(cur.fetchone()[0]):,} rows")  # type: ignore[index]

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["auto", "local", "kaggle", "synthetic"], default="auto")
    parser.add_argument("--skip-download", action="store_true", help="reload db/seed/raw/ as-is")
    parser.add_argument("--no-truncate", action="store_true", help="append instead of replacing")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DB_RW_DSN")
    if not dsn:
        print("DB_RW_DSN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    raw_dir = ROOT / "db" / "seed" / "raw"

    if args.skip_download:
        print("[seed] skipping download; using whatever is in db/seed/raw/")
    else:
        from download import resolve

        used = resolve(args.source, raw_dir)
        print(f"[seed] dataset source: {used}")

    from load import load

    load(dsn=dsn, raw_dir=raw_dir, truncate=not args.no_truncate)

    failures = verify(dsn)
    if failures:
        print(f"\n[seed] FAILED — {failures} verification problem(s)")
        return 1
    print("\n[seed] OK — database seeded and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
