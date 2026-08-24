"""Apply pending SQL migrations from db/migrations/.

    python scripts/migrate.py            # apply anything not yet applied
    python scripts/migrate.py --status   # show what is applied and what is pending
    python scripts/migrate.py --check    # exit 1 if anything is pending (for CI)

Deliberately not Alembic. The schema here is a handful of append-only DDL files, and a plain
runner keeps the SQL readable and reviewable as SQL — which matters when the schema itself
carries security-relevant CHECK constraints. Each applied file is recorded with a checksum, so
editing a migration that has already run is detected instead of silently diverging.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "db" / "migrations"

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS agent;
CREATE TABLE IF NOT EXISTS agent.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT NOT NULL
);
"""


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def checksum(text: str) -> str:
    # Newline-normalised so a CRLF checkout does not look like an edited migration.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


def discover() -> list[tuple[str, Path, str]]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    out = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        out.append((path.stem, path, checksum(text)))
    return out


def applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT version, checksum FROM agent.schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report only, change nothing")
    parser.add_argument("--check", action="store_true", help="exit 1 if migrations are pending")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DB_RW_DSN")
    if not dsn:
        print("DB_RW_DSN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    migrations = discover()
    if not migrations:
        print(f"[migrate] no .sql files in {MIGRATIONS_DIR}")
        return 0

    with psycopg.connect(dsn) as conn:
        done = applied(conn)
        conn.commit()

        pending = [m for m in migrations if m[0] not in done]
        drifted = [
            (version, done[version], digest)
            for version, _path, digest in migrations
            if version in done and done[version] != digest
        ]

        for version, _path, digest in migrations:
            state = "applied" if version in done else "PENDING"
            note = ""
            if version in done and done[version] != digest:
                note = f"  <-- CHECKSUM DRIFT: recorded {done[version]}, file is {digest}"
            print(f"  {state:<8} {version}{note}")

        if drifted:
            print(
                f"\n[migrate] {len(drifted)} already-applied migration(s) have been edited. "
                "Add a new migration instead of changing one that has run.",
                file=sys.stderr,
            )
            return 1

        if args.check:
            if pending:
                print(f"\n[migrate] {len(pending)} pending", file=sys.stderr)
                return 1
            print("\n[migrate] up to date")
            return 0

        if args.status:
            print(f"\n[migrate] {len(done)} applied, {len(pending)} pending")
            return 0

        if not pending:
            print("\n[migrate] nothing to do")
            return 0

        for version, path, digest in pending:
            sql = path.read_text(encoding="utf-8")
            print(f"\n[migrate] applying {version}")
            # One transaction per migration: a failure leaves the schema untouched rather
            # than half-applied.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO agent.schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, digest),
                )
            print(f"[migrate] {version} ok")

    print(f"\n[migrate] applied {len(pending)} migration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
