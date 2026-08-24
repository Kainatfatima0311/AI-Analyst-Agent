"""Control C4: which sensitive columns a query may touch, and how.

A sensitive projection is never silently stripped. It **escalates** — the reviewer sees exactly
what was asked for and decides. Silently rewriting the query would hide the request and give
the agent a misleading result to reason about.

The two hard parts are both about resolution rather than policy. An unqualified column has to
be traced back to a table before the policy can apply, and ``SELECT *`` has to be expanded far
enough to know whether it would return a restricted column. Where resolution is ambiguous the
answer is fail-safe: if any candidate table is sensitive, the reference is treated as sensitive.
"""

from __future__ import annotations

from sqlglot import exp

from analyst_agent.sql_guard.catalog import Catalog
from analyst_agent.sql_guard.errors import Reason
from analyst_agent.sql_guard.policy import (
    SENSITIVE_BY_KEY,
    SensitiveColumn,
    sensitive_columns_of,
)

# alias or bare name (lowercased) -> (schema, object)
AliasMap = dict[str, tuple[str, str]]


def func_name(node: exp.Expr) -> str:
    """The callable's name, lowercased, for both typed and anonymous functions.

    Typed as ``Expr`` rather than ``Expression`` because in sqlglot ``Func`` descends from
    ``Condition``, not from ``Expression`` - the two are siblings under ``Expr``.
    """
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return ""


def _enclosing_func(node: exp.Expr) -> exp.Func | None:
    """The nearest function wrapping this node, if any.

    Nearest rather than any: ``count(distinct lower(email))`` resolves to ``lower``, not
    ``count``, so wrapping a restricted column in a scalar function does not inherit the
    aggregate's permission.
    """
    parent: exp.Expr | None = node.parent
    while parent is not None:
        if isinstance(parent, exp.Func):
            return parent
        parent = parent.parent
    return None


def _outermost_selects(root: exp.Expression) -> list[exp.Select]:
    """The SELECTs whose projection is what the caller actually receives.

    For a plain query that is the root; for a set operation it is every branch, since each one
    contributes columns to the result.
    """
    if isinstance(root, exp.Select):
        return [root]
    if isinstance(root, exp.SetOperation):
        out: list[exp.Select] = []
        for side in (root.this, root.expression):
            out.extend(_outermost_selects(side))
        return out
    if isinstance(root, exp.Subquery):
        return _outermost_selects(root.this)
    return []


def _projection_node_ids(root: exp.Expression) -> set[int]:
    ids: set[int] = set()
    for select in _outermost_selects(root):
        for projected in select.expressions:
            ids.add(id(projected))
            for node in projected.walk():
                ids.add(id(node))
    return ids


def _resolve_column(
    node: exp.Column, aliases: AliasMap, catalog: Catalog
) -> tuple[tuple[str, str], ...]:
    """Which (schema, table) pairs this column reference could belong to.

    Empty when it cannot be resolved at all — for example a reference into a CTE, whose own
    body was already validated on its own terms.
    """
    qualifier = (node.table or "").lower()
    if qualifier:
        target = aliases.get(qualifier)
        return (target,) if target else ()

    column = node.name.lower()
    candidates = [
        (schema, table)
        for schema, table in set(aliases.values())
        if column in {c.lower() for c in catalog.columns_of(schema, table)}
    ]
    return tuple(candidates)


def _sensitive_for(
    schema: str, table: str, column: str
) -> SensitiveColumn | None:
    return SENSITIVE_BY_KEY.get((schema, table, column.lower()))


def check_columns(
    root: exp.Expression, aliases: AliasMap, catalog: Catalog
) -> tuple[list[Reason], set[str]]:
    """Apply the sensitive-column policy.

    Returns the reasons for escalation (empty when the query is clear) and every restricted
    column the query touches, including ones it touched acceptably — the audit records what was
    involved, not only what was refused.
    """
    reasons: list[Reason] = []
    touched: set[str] = set()
    projection_ids = _projection_node_ids(root)
    flagged: set[str] = set()

    def flag(sensitive: SensitiveColumn, how: str) -> None:
        if sensitive.qualified in flagged:
            return
        flagged.add(sensitive.qualified)
        reasons.append(
            Reason(
                code="sensitive_column",
                message=f"{sensitive.qualified} is restricted and would be {how}",
                detail=(
                    f"tier={sensitive.tier}; {sensitive.note}; approved aggregates: "
                    f"{', '.join(sorted(sensitive.approved_aggregates))}"
                ),
            )
        )

    # --- explicit column references -------------------------------------
    for node in root.find_all(exp.Column):
        if isinstance(node.this, exp.Star):
            continue  # handled with the other stars below
        for schema, table in _resolve_column(node, aliases, catalog):
            sensitive = _sensitive_for(schema, table, node.name)
            if sensitive is None:
                continue
            touched.add(sensitive.qualified)

            enclosing = _enclosing_func(node)
            if enclosing is not None and func_name(enclosing) in sensitive.approved_aggregates:
                continue  # an approved aggregate is exactly what the policy permits

            if sensitive.projection_only and id(node) not in projection_ids:
                continue  # grouping, joining and filtering on a surrogate key is ordinary work

            flag(sensitive, "returned" if id(node) in projection_ids else "used")

    # --- SELECT * and t.* ------------------------------------------------
    # A Star inside a function call is not a wildcard projection: count(*) names no column
    # and exposes nothing. Treating it as one flagged every `count(*)` over a table that
    # happens to hold a restricted column, which would have escalated ordinary aggregation.
    stars: list[exp.Expression] = [
        star for star in root.find_all(exp.Star) if not isinstance(star.parent, exp.Func)
    ]
    stars.extend(c for c in root.find_all(exp.Column) if isinstance(c.this, exp.Star))
    for star in stars:
        in_projection = id(star) in projection_ids
        qualifier = (star.table or "").lower() if isinstance(star, exp.Column) else ""

        scope: tuple[tuple[str, str], ...]
        if qualifier:
            target = aliases.get(qualifier)
            scope = (target,) if target else ()
        else:
            scope = tuple(set(aliases.values()))

        for schema, table in scope:
            for sensitive in sensitive_columns_of(schema, table):
                touched.add(sensitive.qualified)
                if sensitive.projection_only and not in_projection:
                    continue
                flag(sensitive, "returned by a wildcard projection")

    return reasons, touched


def build_alias_map(resolved: list[tuple[str, str, str | None]]) -> AliasMap:
    """Map every alias and bare table name to its (schema, object).

    Both forms are registered because a query may refer to the same table by either, and the
    column resolver has to accept whichever the model wrote.
    """
    aliases: AliasMap = {}
    for schema, table, alias in resolved:
        aliases[table.lower()] = (schema, table)
        if alias:
            aliases[alias.lower()] = (schema, table)
    return aliases
