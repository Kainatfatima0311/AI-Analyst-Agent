"""Tool 2: let the agent discover what data exists before it writes SQL.

This is the *only* route to catalog metadata. ``sql_runner`` rejects ``information_schema`` and
``pg_catalog`` outright (control C3), so schema discovery goes through a curated shape rather
than raw catalog rows.

Restricted columns are listed - hiding them would make the agent write SQL that gets escalated
for reasons it cannot see - but they are flagged, annotated with the terms on which they may be
used, and **never** accompanied by sample values.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from analyst_agent.config import get_settings
from analyst_agent.db.engine import ro_conn
from analyst_agent.observability.logging import get_logger
from analyst_agent.sql_guard.policy import SENSITIVE_BY_KEY, SensitiveColumn
from analyst_agent.tools.base import Tool, ToolResult

log = get_logger(__name__)

# Sample values are offered only for columns with few enough distinct values to be a genuine
# category. Above this a sample is noise, and possibly a disclosure.
MAX_DISTINCT_FOR_SAMPLES = 40
MAX_SAMPLES = 12


class SchemaInspectorInput(BaseModel):
    model_config = {"extra": "forbid"}

    tables: list[str] | None = Field(
        default=None,
        description=(
            "Table or view names to describe. Pass null for the whole allowed schema. Names may "
            "be bare ('orders') or qualified ('analytics.orders')."
        ),
    )
    include_samples: bool | None = Field(
        default=None,
        description=(
            "Pass true to include sample distinct values for low-cardinality columns, which "
            "helps you filter on real literals. Restricted columns never return values."
        ),
    )


class SchemaInspectorTool(Tool[SchemaInspectorInput]):
    name = "schema_inspector"
    description = """
Describe the tables, views and columns you are allowed to query.

Call this before writing SQL against a table you have not used yet, so that you filter on real
column names and real literal values rather than guessed ones.

Returns for each object: its columns with types and nullability, an estimated row count, its
foreign keys, and - with include_samples - sample values for low-cardinality columns.

Columns marked restricted:true are subject to the sensitive-column policy. Their sample values
are never returned. Read the 'usage' note on each before referencing it: some may be used only
inside an approved aggregate, others only outside the final projection.

This is the only way to see catalog metadata. sql_runner refuses information_schema and
pg_catalog.
"""
    input_model = SchemaInspectorInput

    def run(
        self, payload: SchemaInspectorInput, run_id: uuid.UUID, step_id: uuid.UUID | None
    ) -> ToolResult:
        settings = get_settings()
        schemas = list(settings.allowed_schemas)
        wanted = self._normalise(payload.tables)

        with ro_conn() as conn, conn.cursor() as cur:
            objects = self._objects(cur, schemas, wanted)
            if not objects:
                return ToolResult.refuse(
                    f"no such table in {', '.join(schemas)}: "
                    f"{', '.join(sorted(wanted)) if wanted else '(none requested)'}",
                    allowed_schemas=schemas,
                    available=self._object_names(cur, schemas),
                )
            described = [
                self._describe(cur, schema, name, kind, bool(payload.include_samples))
                for schema, name, kind in objects
            ]

        restricted = sum(1 for obj in described for col in obj["columns"] if col.get("restricted"))
        return ToolResult.succeed(
            f"described {len(described)} object(s) in {', '.join(schemas)}"
            + (f", {restricted} restricted column(s)" if restricted else ""),
            allowed_schemas=schemas,
            objects=described,
        )

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _normalise(tables: list[str] | None) -> set[str]:
        """Accept 'orders' and 'analytics.orders' alike; compare on the bare name."""
        if not tables:
            return set()
        return {t.strip().split(".")[-1].lower() for t in tables if t.strip()}

    @staticmethod
    def _object_names(cur: Any, schemas: list[str]) -> list[str]:
        cur.execute(
            "SELECT table_schema || '.' || table_name AS qualified "
            "FROM information_schema.tables "
            "WHERE table_schema = ANY(%s) AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1",
            (schemas,),
        )
        return [row["qualified"] for row in cur.fetchall()]

    @staticmethod
    def _objects(cur: Any, schemas: list[str], wanted: set[str]) -> list[tuple[str, str, str]]:
        cur.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = ANY(%s) AND table_type IN ('BASE TABLE', 'VIEW') "
            "ORDER BY table_schema, table_name",
            (schemas,),
        )
        return [
            (r["table_schema"], r["table_name"], r["table_type"])
            for r in cur.fetchall()
            if not wanted or r["table_name"].lower() in wanted
        ]

    def _describe(
        self, cur: Any, schema: str, name: str, kind: str, samples: bool
    ) -> dict[str, Any]:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, name),
        )
        columns: list[dict[str, Any]] = []
        for row in cur.fetchall():
            column = row["column_name"]
            sensitive = SENSITIVE_BY_KEY.get((schema, name, column))
            entry: dict[str, Any] = {
                "name": column,
                "type": row["data_type"],
                "nullable": row["is_nullable"] == "YES",
            }
            if sensitive is not None:
                entry["restricted"] = True
                entry["usage"] = self._usage_note(sensitive)
            elif samples:
                values = self._samples(cur, schema, name, column)
                if values:
                    entry["sample_values"] = values
            columns.append(entry)

        return {
            "name": f"{schema}.{name}",
            "kind": "view" if kind == "VIEW" else "table",
            "estimated_rows": self._estimated_rows(cur, schema, name),
            "columns": columns,
            "foreign_keys": self._foreign_keys(cur, schema, name),
        }

    @staticmethod
    def _usage_note(sensitive: SensitiveColumn) -> str:
        aggregates = ", ".join(sorted(sensitive.approved_aggregates))
        if sensitive.projection_only:
            return (
                f"{sensitive.note}. May be grouped, joined and filtered on, but returning it in "
                f"the final result needs approval. Approved aggregates: {aggregates}."
            )
        return (
            f"{sensitive.note}. Any reference outside an approved aggregate needs approval, "
            f"including a WHERE filter. Approved aggregates: {aggregates}."
        )

    @staticmethod
    def _estimated_rows(cur: Any, schema: str, name: str) -> int | None:
        """Planner estimate, not count(*): this is metadata, not a scan of the table."""
        cur.execute(
            "SELECT reltuples::bigint AS estimate FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, name),
        )
        row = cur.fetchone()
        if row is None or row["estimate"] is None or row["estimate"] < 0:
            return None
        return int(row["estimate"])

    @staticmethod
    def _foreign_keys(cur: Any, schema: str, name: str) -> list[dict[str, Any]]:
        """Foreign keys, read from pg_catalog rather than information_schema.

        ``information_schema.constraint_column_usage`` only returns rows for tables the current
        user *owns*, so as analyst_ro it came back empty and every table looked unrelated to
        every other. pg_constraint is readable, and the agent needs the join paths to write
        correct SQL.
        """
        cur.execute(
            """
            SELECT src_col.attname AS column,
                   tgt_ns.nspname || '.' || tgt.relname AS references_table,
                   tgt_col.attname AS references_column
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
            JOIN pg_class tgt ON tgt.oid = con.confrelid
            JOIN pg_namespace tgt_ns ON tgt_ns.oid = tgt.relnamespace
            JOIN unnest(con.conkey, con.confkey) AS cols(src_attnum, tgt_attnum) ON true
            JOIN pg_attribute src_col
              ON src_col.attrelid = con.conrelid AND src_col.attnum = cols.src_attnum
            JOIN pg_attribute tgt_col
              ON tgt_col.attrelid = con.confrelid AND tgt_col.attnum = cols.tgt_attnum
            WHERE con.contype = 'f' AND src_ns.nspname = %s AND src.relname = %s
            ORDER BY 1
            """,
            (schema, name),
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _samples(cur: Any, schema: str, name: str, column: str) -> list[str] | None:
        """Distinct values, but only for columns that are genuinely categorical.

        The identifiers are quoted from catalog output rather than from model input, and the
        column has already been checked against the sensitive registry by the caller.
        """
        try:
            sampling_sql = (
                f'SELECT DISTINCT "{column}"::text AS value FROM "{schema}"."{name}" '  # noqa: S608
                f'WHERE "{column}" IS NOT NULL LIMIT {MAX_DISTINCT_FOR_SAMPLES + 1}'
            )
            cur.execute(sampling_sql)
            values = [row["value"] for row in cur.fetchall()]
        except Exception as exc:
            # A type that will not cast to text is simply not sampled; this is a convenience,
            # not a correctness requirement.
            log.debug("sampling skipped", table=f"{schema}.{name}", column=column, error=str(exc))
            return None
        if len(values) > MAX_DISTINCT_FOR_SAMPLES:
            return None
        return sorted(values)[:MAX_SAMPLES]
