"""The static policy: what may never appear in a query, and which columns are restricted.

Everything here is data, not logic, so the policy is reviewable on its own. The walk that
applies it lives in ``validator.py``.

Two findings from probing sqlglot drove these lists rather than intuition. First,
``WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`` parses with a **Select** at the root
— so checking the root statement type is not sufficient, and denied node types must be matched
anywhere in the tree. Second, the dangerous Postgres functions all parse as ``exp.Anonymous``,
so they are matched by name rather than by node class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlglot import exp

# --- statement shape --------------------------------------------------------

# The only permitted root node types. A WITH-wrapped SELECT also lands on Select.
ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
)

# Node types that may not appear ANYWHERE in the tree, mapped to the reason code used when one
# is found. Matching by node type rather than by keyword is what makes comment tricks, casing
# and nesting irrelevant.
DENIED_NODES: dict[type[exp.Expression], tuple[str, str]] = {
    exp.Insert: ("dml_insert", "INSERT is not permitted"),
    exp.Update: ("dml_update", "UPDATE is not permitted"),
    exp.Delete: ("dml_delete", "DELETE is not permitted"),
    exp.Merge: ("dml_merge", "MERGE is not permitted"),
    exp.Create: ("ddl_create", "CREATE is not permitted"),
    exp.Drop: ("ddl_drop", "DROP is not permitted"),
    exp.Alter: ("ddl_alter", "ALTER is not permitted"),
    exp.TruncateTable: ("ddl_truncate", "TRUNCATE is not permitted"),
    exp.Comment: ("ddl_comment", "COMMENT ON is not permitted"),
    exp.Grant: ("dcl_grant", "GRANT is not permitted"),
    exp.Copy: ("copy", "COPY is not permitted - it can read and write server files"),
    exp.Set: ("session_set", "SET / RESET is not permitted - session settings are fixed"),
    exp.Into: ("select_into", "SELECT ... INTO creates a table and is not permitted"),
    exp.Lock: ("row_lock", "FOR UPDATE / FOR SHARE takes row locks and is not permitted"),
    exp.Transaction: ("tx_control", "transaction control is not permitted"),
    exp.Commit: ("tx_control", "transaction control is not permitted"),
    exp.Rollback: ("tx_control", "transaction control is not permitted"),
    exp.Use: ("use", "USE is not permitted"),
    exp.Analyze: ("maintenance", "maintenance statements are not permitted"),
    # sqlglot parses anything it does not recognise as a statement (CALL, DO, VACUUM, REINDEX)
    # into Command. Treating that as denied is the fail-closed default: syntax we cannot reason
    # about does not reach the database.
    exp.Command: ("unrecognised_statement", "statement could not be parsed as a SELECT"),
}

# --- functions --------------------------------------------------------------

# Functions that read or write outside the query, reach another database, hold the session
# open, or mutate sequence state. Matched by name, case-insensitively.
DENIED_FUNCTIONS: dict[str, tuple[str, str]] = {
    # server file access
    "pg_read_file": ("fn_file_read", "reads files from the database server"),
    "pg_read_binary_file": ("fn_file_read", "reads files from the database server"),
    "pg_ls_dir": ("fn_file_read", "lists directories on the database server"),
    "pg_stat_file": ("fn_file_read", "stats files on the database server"),
    "pg_ls_logdir": ("fn_file_read", "lists server log files"),
    "pg_ls_waldir": ("fn_file_read", "lists server WAL files"),
    # large objects - a file read and write channel
    "lo_import": ("fn_large_object", "imports a server file as a large object"),
    "lo_export": ("fn_large_object", "writes a large object to a server file"),
    "lo_get": ("fn_large_object", "reads large object data"),
    "lo_put": ("fn_large_object", "writes large object data"),
    "lo_unlink": ("fn_large_object", "deletes a large object"),
    # cross-database
    "dblink": ("fn_cross_database", "connects to another database"),
    "dblink_exec": ("fn_cross_database", "executes a statement in another database"),
    "dblink_connect": ("fn_cross_database", "connects to another database"),
    "postgres_fdw_handler": ("fn_cross_database", "foreign data wrapper access"),
    # denial of service and session control
    "pg_sleep": ("fn_sleep", "holds the session open"),
    "pg_sleep_for": ("fn_sleep", "holds the session open"),
    "pg_sleep_until": ("fn_sleep", "holds the session open"),
    "pg_terminate_backend": ("fn_session_control", "kills other sessions"),
    "pg_cancel_backend": ("fn_session_control", "cancels other sessions"),
    "pg_reload_conf": ("fn_session_control", "reloads server configuration"),
    "pg_rotate_logfile": ("fn_session_control", "rotates server logs"),
    # writes disguised as reads
    "setval": ("fn_sequence_write", "writes sequence state"),
    "nextval": ("fn_sequence_write", "advances a sequence, which is a write"),
    "set_config": ("fn_session_control", "changes session configuration"),
    # dynamic execution and metadata exfiltration
    "query_to_xml": ("fn_dynamic_sql", "executes a query supplied as a string"),
    "query_to_xmlschema": ("fn_dynamic_sql", "executes a query supplied as a string"),
    "database_to_xml": ("fn_dynamic_sql", "serialises the whole database"),
    "pg_logical_emit_message": ("fn_replication", "writes to the replication stream"),
    "pg_create_physical_replication_slot": ("fn_replication", "creates a replication slot"),
}

# --- objects ----------------------------------------------------------------

# Schemas sql_runner may never touch, whatever the allowlist says. schema_inspector is the only
# route to catalog metadata, and it returns a curated shape rather than raw catalog rows.
FORBIDDEN_SCHEMAS: frozenset[str] = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "pg_temp", "agent"}
)

# Names that are recognisably catalog objects even when referenced unqualified.
FORBIDDEN_TABLE_PREFIXES: tuple[str, ...] = ("pg_", "sql_")

# --- sensitive columns (control C4) ----------------------------------------

Tier = Literal["direct_identifier", "pseudonymous", "precise_location"]


@dataclass(frozen=True)
class SensitiveColumn:
    """One restricted column and the terms on which it may be used.

    The tiers exist because "sensitive" is not one thing:

    * ``direct_identifier`` - a name, email, phone or street address identifies a person on its
      own. Any reference outside an approved aggregate is restricted, **including a WHERE
      filter**, because filtering by an email address is a person-level lookup.
    * ``pseudonymous`` - a surrogate key such as ``customer_unique_id`` identifies a person only
      by joining. Grouping and joining on it is ordinary analysis, and the approved
      repeat-customer metric needs exactly that, so only *projecting* it in the outermost select
      list is restricted - that is what produces one row per person.
    * ``precise_location`` - an exact coordinate. Aggregating it is fine; returning it is not.
    """

    schema: str
    table: str
    column: str
    tier: Tier
    approved_aggregates: frozenset[str]
    note: str

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}.{self.column}"

    @property
    def projection_only(self) -> bool:
        """Whether the restriction applies to the outermost projection alone."""
        return self.tier in ("pseudonymous", "precise_location")


_COUNT_ONLY = frozenset({"count"})
# min and max are deliberately excluded for coordinates: they return a real observed point,
# which is a disclosure rather than an aggregate.
_GEO_AGGREGATES = frozenset({"count", "avg", "stddev", "stddev_samp", "stddev_pop", "variance"})

SENSITIVE_COLUMNS: tuple[SensitiveColumn, ...] = (
    SensitiveColumn(
        "analytics", "customer_contact", "full_name", "direct_identifier", _COUNT_ONLY,
        "identifies a person directly",
    ),
    SensitiveColumn(
        "analytics", "customer_contact", "email", "direct_identifier", _COUNT_ONLY,
        "identifies and allows contacting a person directly",
    ),
    SensitiveColumn(
        "analytics", "customer_contact", "phone", "direct_identifier", _COUNT_ONLY,
        "identifies and allows contacting a person directly",
    ),
    SensitiveColumn(
        "analytics", "customer_contact", "street_address", "direct_identifier", _COUNT_ONLY,
        "locates a person at their home",
    ),
    SensitiveColumn(
        "analytics", "customers", "customer_unique_id", "pseudonymous", _COUNT_ONLY,
        "person-level surrogate key; grouping and joining are fine, returning one row per "
        "person is not",
    ),
    SensitiveColumn(
        "analytics", "geolocation", "geolocation_lat", "precise_location", _GEO_AGGREGATES,
        "exact coordinate; aggregate to city or state instead",
    ),
    SensitiveColumn(
        "analytics", "geolocation", "geolocation_lng", "precise_location", _GEO_AGGREGATES,
        "exact coordinate; aggregate to city or state instead",
    ),
)

SENSITIVE_BY_KEY: dict[tuple[str, str, str], SensitiveColumn] = {
    (c.schema, c.table, c.column): c for c in SENSITIVE_COLUMNS
}
SENSITIVE_COLUMN_NAMES: frozenset[str] = frozenset(c.column for c in SENSITIVE_COLUMNS)


def sensitive_columns_of(schema: str, table: str) -> tuple[SensitiveColumn, ...]:
    return tuple(c for c in SENSITIVE_COLUMNS if c.schema == schema and c.table == table)
