"""What a saved report contains, and why it is a copy rather than a link.

A report is built once, from the trace, and stored. The alternative — keep a `run_id` and render
live on every read — is less code and was rejected: a saved report would then change whenever
anything behind it changed, and somebody re-opening a report from March would read different
figures under the same name with nothing to tell them so.

The snapshot therefore carries everything a reader needs to check the answer without touching the
live system: the question, the answer and its confidence, the findings and the explanations tested
against them, the charts, the SQL behind every cited number, and the **metric definition versions
used**. That last one is the reason the metrics layer exists — a figure without its definition
version is a number whose meaning cannot be recovered.

The `run_id` is stored alongside it, so the live trace is always one hop away for anyone who
wants to see what has changed since.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from analyst_agent.agent import confidence as conf

# metric_query appends "[revenue@v1]" to the purpose it records, so a version can be recovered
# from the audit trail even for a query nobody tagged in the answer.
VERSION_IN_PURPOSE = re.compile(r"\[([a-z0-9_]+@v\d+)\]")


def build_snapshot(trace: dict[str, Any], answer: dict[str, Any] | None) -> dict[str, Any]:
    """Freeze a run into a report body.

    Everything is plain JSON-safe types: this goes into a `jsonb` column and comes back out of it
    months later, possibly into a PDF, so a `datetime` or a `UUID` left in place would fail at the
    least convenient moment.
    """
    run = trace.get("run") or {}
    queries = {str(q["query_id"]): q for q in trace.get("queries") or []}
    hypotheses = trace.get("hypotheses") or []
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for hypothesis in hypotheses:
        by_finding.setdefault(str(hypothesis.get("finding_id")), []).append(hypothesis)

    evidence = _evidence(answer, queries)
    score = conf.from_trace(trace, answer)

    return {
        "question": run.get("question", ""),
        "run_id": str(run.get("run_id", "")),
        "status": run.get("status"),
        "asked_at": _iso(run.get("created_at")),
        "finished_at": _iso(run.get("finished_at")),
        "saved_at": datetime.now(UTC).isoformat(),
        "duration_ms": run.get("duration_ms"),
        "answer": {
            "conclusion": (answer or {}).get("conclusion", ""),
            "stated_confidence": (answer or {}).get("confidence"),
            "caveats": list((answer or {}).get("caveats") or []),
            "refuted": list((answer or {}).get("refuted") or []),
        },
        "confidence": score.as_dict(),
        "findings": [
            {
                "statement": finding.get("statement", ""),
                "material": bool(finding.get("material")),
                "hypotheses": [
                    {
                        "statement": h.get("statement", ""),
                        "status": h.get("status"),
                        "reasoning": h.get("reasoning"),
                    }
                    for h in by_finding.get(str(finding.get("finding_id")), [])
                ],
            }
            for finding in trace.get("findings") or []
        ],
        "evidence": evidence,
        "queries_considered": [
            {
                "query_id": str(query["query_id"]),
                "purpose": query.get("purpose", ""),
                "verdict": query.get("verdict"),
                "executed": bool(query.get("executed")),
                "row_count": query.get("row_count"),
                "truncated": bool(query.get("truncated")),
                "reasons": list(query.get("reasons") or []),
                "sql": query.get("rewritten_sql") or query.get("sql_text") or "",
            }
            # Refused statements included on purpose: a report that showed only what ran would
            # hide the half a reviewer usually asks about.
            for query in trace.get("queries") or []
        ],
        "metrics_used": _metrics_used(trace),
        "charts": [
            {
                "chart_id": str(chart["chart_id"]),
                "query_id": str(chart.get("query_id", "")),
                "title": chart.get("title") or "",
                "chart_type": chart.get("chart_type"),
                "spec": chart.get("spec") or {},
            }
            for chart in trace.get("charts") or []
        ],
        "usage": {
            "queries": run.get("queries_used"),
            "tokens_in": run.get("tokens_in"),
            "tokens_out": run.get("tokens_out"),
        },
    }


def _evidence(answer: dict[str, Any] | None, queries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The cited queries, with their SQL.

    A cited id with no matching query is dropped rather than rendered as an empty row: the
    constraint on `findings` already refuses unevidenced findings, and a dangling citation in a
    report would be a link that goes nowhere.
    """
    out = []
    for item in (answer or {}).get("evidence") or []:
        query_id = str(item.get("query_id") if isinstance(item, dict) else item)
        query = queries.get(query_id)
        if query is None:
            continue
        out.append(
            {
                "query_id": query_id,
                "purpose": query.get("purpose", ""),
                "row_count": query.get("row_count"),
                "truncated": bool(query.get("truncated")),
                "sql": query.get("rewritten_sql") or query.get("sql_text") or "",
                "definition_version": _version_of(query.get("purpose", "")),
            }
        )
    return out


def _metrics_used(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Which approved definitions this answer rests on.

    Read from two places and merged: the `metric_query` tool calls, which name a metric outright,
    and the version tag `metric_query` writes into the audit purpose. Either alone would miss
    cases — a tool call whose result was never cited, or a metric computed before the tool
    existed.
    """
    seen: dict[str, dict[str, Any]] = {}

    for call in trace.get("tool_calls") or []:
        if call.get("tool") != "metric_query":
            continue
        arguments = call.get("arguments") or {}
        name = arguments.get("metric")
        if not name:
            continue
        entry = seen.setdefault(str(name), {"metric": str(name), "version": None, "uses": 0})
        entry["uses"] += 1

    for query in trace.get("queries") or []:
        version = _version_of(query.get("purpose", ""))
        if not version:
            continue
        name = version.split("@", 1)[0]
        entry = seen.setdefault(name, {"metric": name, "version": None, "uses": 0})
        entry["version"] = version

    return sorted(seen.values(), key=lambda entry: entry["metric"])


def _version_of(purpose: str) -> str | None:
    match = VERSION_IN_PURPOSE.search(purpose or "")
    return match.group(1) if match else None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def default_name(question: str) -> str:
    """A name that will still mean something in a list of forty.

    The question, trimmed at a word boundary rather than mid-word: a report called
    "Why did revenue drop in Marc" reads as a bug.
    """
    text = " ".join((question or "Untitled analysis").split())
    if len(text) <= 70:
        return text
    cut = text[:70].rsplit(" ", 1)[0]
    return f"{cut}…"


def safe_filename(name: str, suffix: str) -> str:
    """A filename a browser and every filesystem will accept."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "report").strip()).strip("-.")
    stem = (stem or "report")[:60]
    return f"{stem}.{suffix}"


def new_report_id() -> uuid.UUID:
    return uuid.uuid4()
