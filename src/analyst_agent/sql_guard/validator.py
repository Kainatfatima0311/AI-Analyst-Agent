"""Control C2: parse every statement and decide, on the AST, whether it may run.

Regex and keyword denylists are bypassable by comments, casing, unicode escapes, nesting and
statement stacking, so none of that is used here. The statement is parsed, and the decision is
made on node types and resolved object names.

The check that matters most is not the root statement type. Probing sqlglot showed that

    WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x

parses with a **Select** at the root, so a root-type check alone would clear a deletion. The
denied node types are therefore matched anywhere in the tree, and the root check is only the
first of several gates.

``validate`` needs no database: it takes a catalog. That is what lets the hostile-query suite
run in CI with no service container.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from analyst_agent.config import Settings, get_settings
from analyst_agent.sql_guard.catalog import STATIC_CATALOG, Catalog
from analyst_agent.sql_guard.column_policy import build_alias_map, check_columns, func_name
from analyst_agent.sql_guard.errors import GuardVerdict, _Accumulator
from analyst_agent.sql_guard.policy import (
    ALLOWED_ROOTS,
    DENIED_FUNCTIONS,
    DENIED_NODES,
    FORBIDDEN_SCHEMAS,
    FORBIDDEN_TABLE_PREFIXES,
)

DIALECT = "postgres"


def _cte_names(root: exp.Expression) -> set[str]:
    """Names introduced by WITH clauses and subquery aliases.

    These look like tables in the AST but are not objects, so they must not be checked against
    the catalog. They equally must not become a way to smuggle a real name past it, which is
    why the bodies of the CTEs are still walked like everything else.
    """
    names = {str(cte.alias).lower() for cte in root.find_all(exp.CTE) if cte.alias}
    for subquery in root.find_all(exp.Subquery):
        if subquery.alias:
            names.add(str(subquery.alias).lower())
    return names


def _check_statement_shape(sql: str, acc: _Accumulator) -> exp.Expression | None:
    """Parse, and insist on exactly one statement whose root is a query."""
    if not sql or not sql.strip():
        acc.reject("empty_statement", "no SQL was provided")
        return None

    try:
        parsed = [p for p in sqlglot.parse(sql, read=DIALECT) if p is not None]
    except (ParseError, TokenError) as exc:
        acc.reject("parse_error", "the statement could not be parsed", str(exc).split("\n")[0])
        return None

    if not parsed:
        acc.reject("empty_statement", "the statement parsed to nothing")
        return None

    if len(parsed) > 1:
        kinds = ", ".join(type(p).__name__ for p in parsed)
        acc.reject(
            "multiple_statements",
            f"{len(parsed)} statements were provided; exactly one is allowed",
            kinds,
        )
        return None

    root = parsed[0]
    if not isinstance(root, ALLOWED_ROOTS):
        acc.reject("not_a_select", f"the statement is a {type(root).__name__}, not a SELECT")
        return None
    return root


def _check_denied_nodes(root: exp.Expression, acc: _Accumulator) -> None:
    """Reject denied node types anywhere in the tree, root included."""
    seen: set[str] = set()
    for node in root.walk():
        for denied, (code, message) in DENIED_NODES.items():
            if isinstance(node, denied) and code not in seen:
                seen.add(code)
                acc.reject(code, message, f"found {type(node).__name__} in the statement")


def _check_denied_functions(root: exp.Expression, acc: _Accumulator) -> None:
    seen: set[str] = set()
    for node in root.find_all(exp.Func):
        name = func_name(node)
        entry = DENIED_FUNCTIONS.get(name)
        if entry and name not in seen:
            seen.add(name)
            code, why = entry
            acc.reject(code, f"function {name}() is not permitted", why)


def _resolve_objects(
    root: exp.Expression, catalog: Catalog, settings: Settings, acc: _Accumulator
) -> list[tuple[str, str, str | None]]:
    """Resolve and allow-list every real table reference.

    Returns (schema, object, alias) triples for the references that resolved, so the column
    policy can map aliases back to tables.
    """
    allowed = set(settings.allowed_schemas)
    cte_names = _cte_names(root)
    resolved: list[tuple[str, str, str | None]] = []

    for table in root.find_all(exp.Table):
        name = str(table.name)
        if not name:
            continue

        alias = str(table.alias) if table.alias else None
        schema = str(table.db) if table.db else ""
        catalog_name = str(table.catalog) if table.catalog else ""

        # A CTE reference is always unqualified. Skipping every table whose *name* matched a
        # CTE alias was a real bypass: naming a CTE `pg_authid` made the schema-qualified
        # `pg_catalog.pg_authid` skip the allowlist entirely. In SQL a qualified name can
        # never resolve to a CTE, so only unqualified names are treated as CTE references.
        if not schema and not catalog_name and name.lower() in cte_names:
            continue

        if catalog_name:
            acc.reject(
                "cross_database_reference",
                f"{catalog_name}.{schema}.{name} refers to another database",
            )
            continue

        if schema and schema.lower() in FORBIDDEN_SCHEMAS:
            acc.reject(
                "forbidden_schema",
                f"schema {schema} is not readable through sql_runner",
                "catalog metadata is available through the schema_inspector tool only",
            )
            continue

        if not schema:
            if name.lower().startswith(FORBIDDEN_TABLE_PREFIXES):
                acc.reject(
                    "forbidden_schema",
                    f"{name} is a catalog object and is not readable through sql_runner",
                )
                continue
            found = catalog.find_object(name)
            if found is None:
                acc.reject(
                    "unknown_object",
                    f"{name} is not a known object in {', '.join(sorted(allowed))}",
                    "unqualified names resolve against the allowed schemas; an unknown or "
                    "ambiguous name is rejected rather than guessed",
                )
                continue
            schema = found[0]

        if schema.lower() not in allowed:
            acc.reject(
                "schema_not_allowed",
                f"schema {schema} is outside the allowed schemas ({', '.join(sorted(allowed))})",
            )
            continue

        if not catalog.has_object(schema, name):
            acc.reject("unknown_object", f"{schema}.{name} does not exist")
            continue

        acc.objects.add(f"{schema}.{name}")
        resolved.append((schema, name, alias))

    if not resolved and not acc.rejections:
        acc.note(
            "no_table_reference",
            "the statement reads no table",
            "constant-only queries are allowed but rarely useful",
        )
    return resolved


def _check_cross_joins(root: exp.Expression, acc: _Accumulator) -> None:
    """Reject a cartesian product that nothing constrains.

    Only when there is no WHERE clause either: with a filter present the AST cannot tell us
    whether the product is bounded, and the EXPLAIN cost gate is the right place for that case.
    """
    for select in root.find_all(exp.Select):
        joins = list(select.args.get("joins") or [])
        if not joins or select.args.get("where") is not None:
            continue
        for join in joins:
            unconstrained = join.args.get("on") is None and join.args.get("using") is None
            if unconstrained and isinstance(join.this, exp.Table):
                acc.reject(
                    "unbounded_cross_join",
                    "a cross join with no join condition and no WHERE clause is not permitted",
                    f"joined table: {join.this.sql(dialect=DIALECT)}",
                )
                return


def _apply_row_limit(
    root: exp.Expression, settings: Settings, requested: int | None, acc: _Accumulator
) -> tuple[str, int]:
    """Ensure the statement carries a sane LIMIT, and record what was done.

    An absent limit is injected and an oversized one clamped. Both are recorded as notes: the
    agent needs to know a result may be partial, so that it aggregates rather than reasoning
    over a truncated sample.
    """
    ceiling = settings.sql_max_row_limit
    target = min(requested or settings.sql_default_row_limit, ceiling)

    existing = root.args.get("limit")
    current: int | None = None
    if isinstance(existing, exp.Limit) and isinstance(existing.expression, exp.Literal):
        try:
            current = int(existing.expression.name)
        except ValueError:
            current = None

    if current is None:
        acc.note("limit_injected", f"no LIMIT was present; LIMIT {target} was added")
        effective = target
    elif current > ceiling:
        acc.note(
            "limit_clamped", f"LIMIT {current} exceeds the ceiling and was reduced to {ceiling}"
        )
        effective = ceiling
    else:
        effective = current

    if not isinstance(root, exp.Query):
        # Unreachable given ALLOWED_ROOTS, but the row cap is a safety control: if a future
        # root type slips through, fail closed rather than emit an unlimited statement.
        acc.reject("limit_not_applicable", f"cannot apply a row limit to {type(root).__name__}")
        return sql_text_of(root), effective
    return root.limit(effective, copy=True).sql(dialect=DIALECT), effective


def sql_text_of(root: exp.Expr) -> str:
    return root.sql(dialect=DIALECT)


def validate(
    sql: str,
    *,
    catalog: Catalog | None = None,
    settings: Settings | None = None,
    row_limit: int | None = None,
) -> GuardVerdict:
    """Statically validate one statement. No database access, no side effects.

    The EXPLAIN cost gate lives in ``explain_gate.py`` because it needs a connection;
    ``sql_guard.check`` runs both in the right order.
    """
    settings = settings or get_settings()
    catalog = catalog if catalog is not None else STATIC_CATALOG
    acc = _Accumulator()

    root = _check_statement_shape(sql, acc)
    if root is None:
        return acc.build(None, None)

    _check_denied_nodes(root, acc)
    _check_denied_functions(root, acc)
    resolved = _resolve_objects(root, catalog, settings, acc)
    _check_cross_joins(root, acc)

    aliases = build_alias_map(resolved)
    column_reasons, touched = check_columns(root, aliases, catalog)
    acc.sensitive.update(touched)
    acc.escalations.extend(column_reasons)

    if acc.rejections:
        return acc.build(None, None)

    rewritten, effective_limit = _apply_row_limit(root, settings, row_limit, acc)
    return acc.build(rewritten, effective_limit)
