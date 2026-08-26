"""Run the evaluation suite and write a report.

    python -m evals.runner --validate     # check the suite itself; no model needed
    python -m evals.runner --all          # run the agent against every question
    python -m evals.runner --category diagnostic
    python -m evals.runner --id dia001

``--validate`` exists because a suite can be wrong. Every reference query is executed and put
through the SQL guard, and every question's expectations are checked for coherence. A reference
query that no longer runs would otherwise quietly grade every future run against nothing, and a
question that demands both an answer and a refusal would score whatever the agent happened to do.
It needs no API key, so the suite stays verifiable even when the agent cannot run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyst_agent.config import get_settings
from analyst_agent.db import repository as repo
from analyst_agent.db.engine import ro_conn
from analyst_agent.observability.logging import configure_logging, get_logger, set_redaction_secrets
from analyst_agent.sql_guard import STATIC_CATALOG, validate
from evals.graders import analytical_quality, calculation, sql_safety
from evals.schema import CATEGORY_INTENT, Question, load_questions

log = get_logger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclass
class QuestionOutcome:
    id: str
    category: str
    question: str
    status: str = "not_run"
    duration_ms: int = 0
    run_id: str | None = None
    calculation: dict[str, Any] = field(default_factory=dict)
    safety_violations: list[str] = field(default_factory=list)
    quality_checks: dict[str, bool] = field(default_factory=dict)
    quality_failures: list[str] = field(default_factory=list)
    judged: dict[str, Any] | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        """Everything has to hold. A wrong number with clean safety is still a wrong answer."""
        if self.error or self.status == "not_run":
            return False
        if self.safety_violations or self.quality_failures:
            return False
        return not (self.calculation.get("graded") and not self.calculation.get("passed"))


# --- validating the suite itself --------------------------------------------


def validate_suite(questions: list[Question]) -> tuple[bool, list[str]]:
    """Check every reference query runs and would pass the guard.

    Both halves matter. A query that does not run grades nothing; a query the guard would reject
    is not a fair reference, because it asks the agent to produce something it is not allowed to.
    """
    problems: list[str] = []
    settings = get_settings()

    with ro_conn() as conn:
        for question in questions:
            if not question.ground_truth_sql:
                continue

            verdict = validate(
                question.ground_truth_sql, catalog=STATIC_CATALOG, settings=settings
            )
            if verdict.verdict == "rejected":
                problems.append(
                    f"{question.id}: the reference query would be rejected by the guard "
                    f"({', '.join(verdict.codes)})"
                )
                continue

            try:
                value = calculation.reference_value(question, conn)
            except Exception as exc:
                problems.append(f"{question.id}: the reference query failed: {exc}")
                continue

            if value is None:
                problems.append(f"{question.id}: the reference query returned no value")
            else:
                log.info("reference computed", question=question.id, value=value)

    return not problems, problems


# --- running one question ---------------------------------------------------


def run_one(question: Question, judge_llm: Any | None) -> QuestionOutcome:
    from analyst_agent.agent.graph import build_graph, start_run

    outcome = QuestionOutcome(id=question.id, category=question.category, question=question.question)
    started = time.monotonic()

    try:
        state = start_run(question.question, requested_by="evals", graph=build_graph())
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    outcome.run_id = state["run_id"]
    outcome.status = state.get("status", "unknown")

    run_id = uuid.UUID(state["run_id"])
    trace = repo.get_trace(run_id)
    run_view = _run_view(state, trace)

    expected = None
    if question.ground_truth_sql:
        with ro_conn() as conn:
            expected = calculation.reference_value(question, conn)
    calc = calculation.grade(question, run_view, expected)
    outcome.calculation = asdict(calc)

    safety = sql_safety.grade(question, run_view, trace)
    outcome.safety_violations = [str(v) for v in safety.violations]

    quality = analytical_quality.grade_mechanically(question, run_view, trace)
    outcome.quality_checks = quality.checks
    outcome.quality_failures = quality.failures

    if judge_llm is not None:
        verdict, error = analytical_quality.judge(question, run_view, trace, judge_llm)
        outcome.judged = verdict.model_dump() if verdict else {"error": error}

    return outcome


def _run_view(state: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """Shape the graph's final state the way the graders expect it.

    Built from the trace rather than the state so the graders see what was *recorded*, which is
    what a reviewer would see, rather than whatever the last node happened to return.
    """
    hypotheses_by_finding: dict[Any, list[dict[str, Any]]] = {}
    for hypothesis in trace["hypotheses"]:
        hypotheses_by_finding.setdefault(hypothesis["finding_id"], []).append(
            {
                "statement": hypothesis["statement"],
                "status": hypothesis["status"],
                "reasoning": hypothesis.get("reasoning"),
            }
        )

    answer = trace["run"].get("answer")
    if answer:
        answer = {**answer, "evidence": answer.get("evidence", [])}

    return {
        "status": trace["run"]["status"],
        "answer": answer,
        "clarifications": state.get("clarifications", []),
        "findings": [
            {
                "statement": finding["statement"],
                "material": finding["material"],
                "hypotheses": hypotheses_by_finding.get(finding["finding_id"], []),
            }
            for finding in trace["findings"]
        ],
    }


# --- reporting ---------------------------------------------------------------


def build_report(outcomes: list[QuestionOutcome], questions: list[Question]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        bucket = by_category.setdefault(
            outcome.category,
            {"intent": CATEGORY_INTENT[outcome.category], "total": 0, "passed": 0, "failed_ids": []},
        )
        bucket["total"] += 1
        if outcome.passed:
            bucket["passed"] += 1
        else:
            bucket["failed_ids"].append(outcome.id)

    for bucket in by_category.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 3) if bucket["total"] else 0.0

    graded = [o for o in outcomes if o.calculation.get("graded")]
    correct = [o for o in graded if o.calculation.get("passed")]
    violations = [v for o in outcomes for v in o.safety_violations]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "questions": len(questions),
        "run": len([o for o in outcomes if o.status != "not_run"]),
        "passed": sum(1 for o in outcomes if o.passed),
        "pass_rate": round(sum(1 for o in outcomes if o.passed) / len(outcomes), 3) if outcomes else 0.0,
        "calculation": {
            "graded": len(graded),
            "correct": len(correct),
            "accuracy": round(len(correct) / len(graded), 3) if graded else None,
        },
        "safety": {
            "violations": len(violations),
            "detail": violations,
            # One violation fails the suite. Averaging it away would hide the thing that matters.
            "passed": not violations,
        },
        "by_category": by_category,
        "median_duration_ms": _median([o.duration_ms for o in outcomes if o.duration_ms]),
        # `passed` is a property, and asdict() only serialises fields — so it is added
        # explicitly rather than being silently absent from the report.
        "outcomes": [{**asdict(o), "passed": o.passed} for o in outcomes],
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Generated {report['generated_at']}",
        "",
        f"**{report['passed']} of {report['questions']} passed** "
        f"({report['pass_rate']:.0%}).",
        "",
    ]

    safety = report["safety"]
    if safety["passed"]:
        lines += ["**SQL safety: 0 violations.** The required result is zero, not a low number.", ""]
    else:
        lines += [
            f"**SQL SAFETY FAILED — {safety['violations']} violation(s).** "
            "One is enough to fail the suite.",
            "",
        ]
        lines += [f"- {v}" for v in safety["detail"]] + [""]

    calc = report["calculation"]
    if calc["graded"]:
        lines += [
            f"Calculation accuracy: **{calc['correct']}/{calc['graded']}** "
            f"({calc['accuracy']:.0%}) within tolerance.",
            "",
        ]

    lines += ["## By category", "", "| Category | Passed | Rate | What it measures |", "|---|---|---|---|"]
    for name, bucket in sorted(report["by_category"].items()):
        lines.append(
            f"| {name} | {bucket['passed']}/{bucket['total']} | {bucket['pass_rate']:.0%} | "
            f"{bucket['intent']} |"
        )

    failures = [o for o in report["outcomes"] if not o["passed"]]
    if failures:
        lines += ["", "## Failures", "", "Each one needs a written diagnosis: fixed, or recorded as a known limitation.", ""]
        for outcome in failures:
            lines.append(f"### {outcome['id']} — {outcome['question']}")
            lines.append("")
            if outcome["error"]:
                lines.append(f"- Errored: `{outcome['error']}`")
            for violation in outcome["safety_violations"]:
                lines.append(f"- **Safety**: {violation}")
            for failure in outcome["quality_failures"]:
                lines.append(f"- Quality: {failure}")
            calc_result = outcome["calculation"]
            if calc_result.get("graded") and not calc_result.get("passed"):
                lines.append(f"- Calculation: {calc_result.get('note')}")
            lines.append("")

    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "")[:15]
    json_path = directory / f"{stamp}.json"
    md_path = directory / f"{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# --- cli ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run every question")
    parser.add_argument("--category", help="run one category")
    parser.add_argument("--id", dest="question_id", help="run one question")
    parser.add_argument("--validate", action="store_true", help="check the suite; no model needed")
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM quality rubric")
    parser.add_argument("--report", type=Path, default=REPORTS_DIR)
    args = parser.parse_args()

    settings = get_settings()
    set_redaction_secrets(settings.secret_values)
    configure_logging(settings.log_level, "console")

    questions = load_questions()
    print(f"[evals] {len(questions)} questions loaded")

    if args.validate or not (args.all or args.category or args.question_id):
        ok, problems = validate_suite(questions)
        for problem in problems:
            print(f"  FAIL  {problem}", file=sys.stderr)
        if ok:
            print("[evals] the suite is sound: every reference query runs and passes the guard")
            return 0
        print(f"[evals] {len(problems)} problem(s) with the suite itself", file=sys.stderr)
        return 1

    selected = questions
    if args.category:
        selected = [q for q in questions if q.category == args.category]
    if args.question_id:
        selected = [q for q in questions if q.id == args.question_id]
    if not selected:
        print("[evals] nothing selected", file=sys.stderr)
        return 1

    judge_llm = None
    if not args.no_judge:
        try:
            from analyst_agent.agent.llm import get_llm

            llm = get_llm()
            llm.client  # noqa: B018 - fails here if no key, rather than mid-suite
            judge_llm = llm
        except Exception as exc:
            print(f"[evals] quality judge disabled: {exc}")

    outcomes = []
    for index, question in enumerate(selected, start=1):
        print(f"[evals] {index}/{len(selected)}  {question.id}  {question.question[:60]}")
        outcome = run_one(question, judge_llm)
        outcomes.append(outcome)
        print(f"          {'pass' if outcome.passed else 'FAIL'}  ({outcome.duration_ms} ms)")

    report = build_report(outcomes, selected)
    json_path, md_path = write_report(report, args.report)

    print()
    print(render_markdown(report))
    print(f"[evals] written to {md_path.name} and {json_path.name}")
    return 0 if report["safety"]["passed"] and report["pass_rate"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
