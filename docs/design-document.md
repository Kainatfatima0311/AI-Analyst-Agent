# Design Document — AI Data Analyst & Business Intelligence Agent

**Status:** approved for implementation · **Version:** 1.0 · **Date:** 2026-08-24

Written **before implementation**, per the project's common standards. Changes made during
implementation are recorded in [../progress.md](../progress.md) and reflected in the final
technical report.

---

## 1. Agent goal

Given a business question in natural language, the agent must:

1. Decide whether the question is answerable from the available data, and **stop and ask** when it
   is ambiguous or unanswerable.
2. Resolve the business terms in the question to **approved metric definitions**.
3. Identify the tables and columns required, and author SQL for them.
4. Execute that SQL **only after** it has passed validation, against a **read-only** role.
5. Inspect the results, and where a finding is material, generate **at least two competing
   explanations** and test each with its own query.
6. Run follow-up Python analysis and produce charts or tables.
7. Present a conclusion in which **every number is traceable** to a specific query, row count and
   metric definition version.

### Success criteria

| Criterion | Target |
|---|---|
| Calculation accuracy on factual questions | ≥ 90% within tolerance of a hand-written reference query |
| SQL safety violations (any non-SELECT execution, any policy bypass) | 0 — a single breach fails the evaluation suite |
| Diagnostic questions answered with ≥ 2 tested hypotheses | 100% |
| Ambiguous questions correctly deferred to a human instead of guessed | ≥ 90% |
| Every reported number linked to a query id | 100% |

### Explicit non-goals

Writing to the warehouse; scheduled or push reporting; dashboard authoring; multi-tenant row-level
security; forecasting or causal inference beyond descriptive hypothesis testing; generating new
metric definitions without human approval.

---

## 2. System architecture

```
Browser UI  ──HTTP/SSE──> FastAPI ──> LangGraph agent ──> Postgres checkpointer (app_rw)
(served at /app/)
                              |             |
                              |             +- metric_lookup      approved KPI registry
                              |             +- schema_inspector    allow-listed metadata
                              |             +- sql_runner         -+
                              |             +- python_analysis     | every statement is
                              |             +- chart_builder       | validated by sql_guard
                              |                                    | before it can execute
                              +- runs / run_steps / tool_calls /   |
                                 sql_audit / approvals / findings  |
                                                                   v
                                                    analytics schema, analyst_ro role
                                                    (read-only, timeout, row cap)
```

Two database roles exist from the first commit and are never merged:

- **`app_rw`** — owns the agent's own tables (run state, traces, audit, LangGraph checkpoints). The
  agent's SQL tool never receives this connection string.
- **`analyst_ro`** — the only role any generated query ever runs under. `SELECT` on schema
  `analytics` only, `default_transaction_read_only = on`, a statement timeout, and an idle
  transaction timeout.

### Why LangGraph, and why the Anthropic SDK directly

LangGraph provides the three things this project actually needs: an explicit state graph whose
**edge conditions can enforce policy** (a material finding cannot reach synthesis with fewer than
two tested hypotheses), a Postgres checkpointer giving durable task state and recovery, and
`interrupt()` for human approval that survives a process restart.

The LLM calls go through one thin wrapper (`agent/llm.py`) built on the official `anthropic` Python
SDK, not a LangChain LLM abstraction. That keeps direct control over prompt caching of the large
stable prefix (schema card, metric registry, safety rules), adaptive thinking, per-node effort
tiers, structured outputs, and refusal handling.

---

## 3. Tool inventory and contracts

Six tools, each with a pydantic input model, a `strict: true` Anthropic tool schema, an executor,
and a mandatory audit write. A tool that refuses returns a structured refusal — never a silent
empty result.

**Which tools the model chooses, and which the graph calls.** `sql_runner` is invoked by a node
after `author_sql` has produced exactly one statement through structured output — deliberately
not by the model in a loop, because one statement per turn through the guard and the audit is what
makes the safety story deterministic. The other four (`schema_inspector`, `metric_query`,
`python_analysis`, `chart_builder`) are offered to the model in bounded tool loops, because
whether looking at the schema, computing a metric, deriving a further view or drawing a chart
*helps* depends on what the data turned out to look like, and that cannot be scheduled from
outside the run. Each loop caps its turns and offers only the tools its node names.

### 3.1 `metric_lookup`

| | |
|---|---|
| **Purpose** | Resolve a business term ("revenue", "AOV", "churn") to an approved definition. |
| **Input** | `term: str`, `dimensions: list[str] = []` |
| **Output** | `MetricDefinition` (name, version, grain, SQL template, dimensions, caveats) or `NotApproved(term, closest_matches)` |
| **Authority** | Read-only over the registry. |
| **Failure** | An unknown term produces `NotApproved`. The agent must then either ask the user or proceed with an explicitly flagged ad-hoc definition. It may **not** silently invent a formula. |

### 3.2 `schema_inspector`

| | |
|---|---|
| **Purpose** | Let the agent discover what data exists before writing SQL. |
| **Input** | `tables: list[str] or null`, `include_samples: bool = False` |
| **Output** | Tables, columns, types, nullability, row-count estimates, foreign keys, and sample distinct values for low-cardinality columns only. |
| **Authority** | Allow-listed metadata for schema `analytics`. Sensitive columns are listed with a `restricted: true` flag but **never** with sample values. |
| **Failure** | An unknown table produces an error naming the allow-listed tables. |

### 3.3 `sql_runner`

| | |
|---|---|
| **Purpose** | Execute an analytical query. |
| **Input** | `sql: str`, `purpose: str`, `row_limit: int or null` |
| **Output** | `QueryResult(query_id, columns, rows, row_count, truncated, duration_ms, guard_verdict)` |
| **Authority** | `analyst_ro` only, and only after `sql_guard.validate()` returns allowed. |
| **Failure** | Guard rejection returns `GuardRejection(reasons)` with no execution. Guard escalation raises an approval request and the run pauses. A timeout returns `QueryTimeout` with a narrowing suggestion. Truncation is reported, never hidden. |

`purpose` is a required argument: it is written to `sql_audit` so a reviewer can see *why* each
query ran, not just what it was.

### 3.4 `python_analysis`

| | |
|---|---|
| **Purpose** | Follow-up analysis over a **previously executed** query's result frame. |
| **Input** | `query_id: str`, `operation: enum`, `params: dict` |
| **Output** | A derived frame plus a plain-language summary of what was computed. |
| **Authority** | Operates only on frames already produced and audited. No network, no filesystem, an import allowlist, and wall-clock plus memory caps. Operations are a **fixed enumerated set** — groupby, pivot, rolling, correlation, share-of-total, period-over-period, describe, simple linear fit — not arbitrary generated code. |
| **Failure** | Unknown `query_id`, incompatible columns, or an empty frame produce structured errors. A resource cap produces `AnalysisAborted`. |

Choosing an enumerated operation set over executing model-written Python is deliberate: it removes
an entire class of sandbox-escape risk at the cost of some flexibility. The tradeoff is recorded in
the final report.

### 3.5 `metric_query`

| | |
|---|---|
| **Purpose** | Compute an approved metric without writing SQL for it. |
| **Input** | `metric: str`, `dimensions: list[str] or null`, `date_from`, `date_to`, `filters: dict`, `purpose: str` |
| **Output** | The same shape as `sql_runner`, plus the `definition_version` the figure came from and the metric's caveats. |
| **Authority** | The registry assembles the statement from reviewed parts; every value is a bound parameter. The result still passes through `sql_guard` and still lands in `sql_audit` — a narrower door, not a bypass. |
| **Failure** | An undeclared dimension, or a breakdown of a custom-shaped metric, is refused with the dimensions that *are* available. |

This is what makes the metrics layer a guarantee rather than a lookup table. `metric_lookup`
tells the model what a metric means; without this tool the model then wrote its own SQL for it,
so the claim "no free text from the model reaches SQL for an approved metric" was true of the
registry and not of the agent. Here the model supplies only **names**.

### 3.6 `chart_builder`

| | |
|---|---|
| **Purpose** | Turn a result frame into a chart for the answer. |
| **Input** | `query_id: str`, `chart_type: enum`, `x`, `y`, `series`, `title` |
| **Output** | A Plotly spec plus a PNG, and the `query_id` the chart is built from. |
| **Authority** | Read-only over stored frames. |
| **Failure** | Unsuitable column types or too many series produce a refusal with a suggested alternative chart type. |

Every chart carries the `query_id` of its source frame, so a chart in the UI is one click away from
the SQL behind it.

---

## 4. Agent state

`AnalystState` is a typed structure persisted by the LangGraph Postgres checkpointer after every
node.

```
run_id, thread_id, question, asked_at, requested_by
clarifications:   [{question, answer|null, asked_at}]
resolved_metrics: [{term, metric_name, version, approved}]
plan:             [{step_id, intent, status}]
queries:          [{query_id, sql, purpose, guard_verdict, row_count, truncated, duration_ms}]
frames:           {query_id -> frame handle}
findings:         [{finding_id, statement, evidence_query_ids[], material}]
hypotheses:       [{hypothesis_id, finding_id, statement, test_design, test_query_ids[],
                    status: proposed|testing|supported|refuted|inconclusive, reasoning}]
charts:           [{chart_id, query_id, spec_ref, png_ref}]
approvals:        [{approval_id, kind, payload, status, requested_at, decided_at, decided_by}]
budget:           {queries_used, tokens_in, tokens_out, iterations, started_at, wall_clock_s}
errors:           [{node, kind, message, recoverable, occurred_at, attempt}]
answer:           {conclusion, confidence, caveats, evidence_map} | null
status:           received|clarifying|investigating|awaiting_approval|completed|failed|truncated
```

Two invariants matter for review, and both are asserted in code rather than merely requested in a
prompt: nothing in `findings` may be reported with an empty `evidence_query_ids`, and no entry in
`hypotheses` may leave `proposed` without at least one `test_query_ids` entry.

---

## 5. Control flow

```
intake
  +-> clarify_gate --ambiguous--> ask_human (run pauses, status=clarifying)
        +-> resolve_metrics --unapproved metric--> ask_human | flag_ad_hoc
              +-> gather_context  [tool loop: schema_inspector, metric_lookup]
                    +-> plan
                          +-> compute_metrics  [tool loop: metric_query]
                                |  answered by an approved metric -> interpret
                                +  something else needed
                                      +-> author_sql -> sql_guard -> execute
                                            |  reject   -> answer with what is established
                                            +  escalate -> request_approval (run pauses)
                                                              |
                                                          interpret
                                                              |
                                          analyse  [tool loop: python_analysis]
                                                              |
                                                     materiality_check
   +--- not material ---------------------------------------------+
   |                                                     material |
   |                                 generate_hypotheses (>= 2, effort=xhigh)
   |                                                              |
   |             per hypothesis:  design_test -> author_sql -> sql_guard -> execute
   |                              -> evaluate (supported | refuted | inconclusive)
   |                                                              |
   |                                                        reconcile
   |                                                              |
   |                              another material finding? --yes--> materiality_check
   |                                                              | no
   +--------------> synthesize -> visualize  [tool loop: chart_builder] -> respond
```

Three of these nodes run a **bounded tool loop** rather than a single structured call, marked
above. `author_sql` deliberately does not: it produces exactly one statement per turn through
structured output, so exactly one statement per turn reaches the guard and the audit.

Budget exhaustion at `author_sql` does not go straight to a truncated answer — it parks the run
and asks (approval point 3). Only a refused or timed-out extension truncates.

### Rules encoded as graph edges, not prompt text

1. When `materiality_check` marks a finding material, the edge to `synthesize` becomes
   **unavailable** until at least two hypotheses for that finding have reached a terminal status.
2. `design_test` rejects a hypothesis whose test would not distinguish it from a sibling
   hypothesis. A test that cannot fail is not a test.
3. `reconcile` must record which explanations were **refuted and why**, not only which one won.
   Refuted hypotheses are required content in the synthesis prompt.
4. If competing hypotheses remain `inconclusive`, confidence is downgraded and the answer says so.
   The agent is not permitted to pick one arbitrarily.
5. Budget exhaustion asks for an extension (approval point 3) and, if refused, routes to
   `respond` with `status=truncated` and a partial answer — never to an unsupported conclusion.

### Per-node model settings

| Node | Effort | Rationale |
|---|---|---|
| `clarify_gate`, `materiality_check` | `low` | Cheap classification with structured output. |
| `plan`, `author_sql`, `repair_sql`, `interpret` | `high` | Correctness-sensitive. |
| `generate_hypotheses`, `reconcile`, `synthesize` | `xhigh` | The reasoning the project is actually judged on. |

Every node uses adaptive thinking and shares a cached system prefix (schema card, metric registry,
safety rules). Cache effectiveness is asserted in the evaluation report from the reported
cache-read token counts.

---

## 6. Approval points

Human approval is required — the agent never holds unlimited permission — at exactly four points.
Each request persists to the `approvals` table with its full payload and reason, so the decision is
auditable and survives a restart.

| # | Trigger | What the reviewer sees | Default on timeout |
|---|---|---|---|
| 1 | The EXPLAIN cost gate reports an estimated cost above the configured ceiling | The exact SQL, the plan, the estimated cost, the ceiling | Reject |
| 2 | A query touches a restricted or sensitive column | The SQL, which columns, and whether the use is projection or an approved aggregate | Reject |
| 3 | Budget exhausted mid-investigation and the agent requests an extension | What has been established, what remains untested, the cost so far | Reject, then partial answer |
| 4 | Exporting or publishing a report outside the session | The full answer with its evidence map | Reject |

Rejection is a first-class path, not an error. The run continues, reports what it could establish
without the rejected action, and states plainly what it could not.

---

## 7. Failure handling

| Failure | Detection | Response |
|---|---|---|
| Model refusal | `stop_reason` checked before reading content on every call | Server-side fallback; if still refused, surface to the user with the category |
| Invalid SQL syntax | sqlglot parse error | `repair_sql`, at most 2 attempts, then ask the user |
| Guard rejection | Verdict is not allowed | `repair_sql` with the rejection reasons in context; a second rejection of the same kind aborts that plan step |
| Query timeout | Postgres statement timeout | Reported to the model with a narrowing suggestion; one retry with a tighter filter |
| Empty result set | `row_count == 0` | Treated as a **finding**, not an error — the agent must check whether the filter is wrong before concluding "no data" |
| Truncated result | Row cap reached | Flagged in the answer; the agent must aggregate rather than reason over a truncated sample |
| Rate limit | Typed SDK exception | Exponential backoff, bounded retries |
| Transient database error | Typed exception | Retry with backoff, then fail the step while keeping the run resumable |
| Process crash or restart | Checkpoint present and run not terminal | Resume from the last checkpoint by `thread_id` |
| Budget exhausted | Counters in `budget.py` | `status=truncated`, partial answer with an explicit note |
| Contradictory evidence | `reconcile` finds no discriminating result | `inconclusive`, confidence downgraded, both explanations reported |

Retries are bounded and counted per node in `state.errors` with an attempt number, so a retry storm
is visible in the trace rather than hidden inside it.

---

## 8. Security limits

Layered, so that no single failure is sufficient. Threat mapping in
[security-controls.md](security-controls.md).

1. **Physical read-only.** All generated SQL runs as `analyst_ro`: `SELECT` on schema `analytics`
   only, `default_transaction_read_only = on`, no access to `public`. Even a total validator bypass
   cannot write.
2. **AST validation, not regex.** sqlglot parses each statement; exactly one statement is
   permitted, and its root must be a `SELECT`, optionally wrapped in `WITH`. Every DDL, DML and DCL
   node, plus `COPY`, `CALL`, `DO`, `SET`/`RESET` and dangerous functions (`pg_read_file`,
   `pg_ls_dir`, large-object functions, `dblink`, `pg_sleep`) is rejected. Regex validation is
   bypassable and is not used.
3. **Object allowlist.** Only tables in schema `analytics`. `pg_catalog` and `information_schema`
   are reachable through `schema_inspector` alone, never through `sql_runner`.
4. **Sensitive column policy.** Customer name, email, phone, street address, precise geolocation
   and payment identifiers cannot be projected. Approved aggregates over them are allowed. A
   sensitive projection is never silently stripped — it raises, and becomes approval point 2.
5. **Resource limits.** Statement timeout, idle-transaction timeout, an injected or clamped
   `LIMIT`, and an EXPLAIN cost gate before execution. Cross joins with no join condition are
   rejected.
6. **Prompt-injection containment.** Data returned from the warehouse is treated as data, never as
   instructions, and tool results are never interpolated into the system prompt. A row whose text
   contains an instruction still cannot cause a write, because writing requires both a role the
   connection does not have and a statement the validator rejects.
7. **Budget caps.** Queries per run, hypotheses per finding, iterations, tokens and wall clock are
   all capped, so a runaway loop is bounded rather than merely expensive.
8. **Secrets.** Credentials come only from the environment, `.env` is git-ignored, and the two DSNs
   are kept separate in configuration so the tool layer can reach only the read-only one.
9. **Full audit.** Every query — allowed, rejected or escalated — is written to `sql_audit` with its
   verdict, reasons, purpose, timing and row count.

---

## 9. Observability

Structured JSON logging via structlog, with `run_id`, `step_id`, `node`, `tool` and `query_id`
bound onto every line, so a single run is one filterable stream. Persisted alongside it:
`run_steps` (node entry and exit, duration, status), `tool_calls` (arguments, result summary,
error), `sql_audit` (SQL, verdict, reasons, purpose, rows, duration), `approvals` (request,
decision, decider, timing), and per-call token and cost accounting.

`GET /v1/runs/{id}/trace` returns the whole reconstruction, and the UI's "Show the evidence" drawer
is built directly on it. The operative test: for any finished run, a reviewer who was not present
can explain why it succeeded or failed from the trace alone.

---

## 10. Persistence and recovery

The LangGraph Postgres checkpointer writes state after every node, keyed by `thread_id`. A run
therefore survives an API restart mid-investigation, an approval that arrives an hour later, and a
crash inside a node — it resumes from the last completed node rather than from the beginning.
Terminal states are `completed`, `failed` and `truncated`; anything else is resumable. Resumption is
idempotent because completed queries are keyed by `query_id` and reused rather than re-executed.

---

## 11. Evaluation approach

At least 30 business questions across six categories, deliberately including cases where the
**correct behaviour is to stop or ask** rather than answer. Three grader families: calculation
accuracy against hand-written reference SQL; SQL safety, where any single violation fails the whole
suite; and analytical quality judged against a fixed rubric covering hypothesis count and
distinctness, evidence-to-claim linkage, honesty about refutation, and calibrated confidence. Full
composition in [../plan.md](../plan.md), Step 12.

---

## 12. Risks accepted at design time

| Risk | Mitigation | Residual |
|---|---|---|
| The model authors correct-looking but semantically wrong SQL | Approved metric registry; recorded `purpose`; evidence map exposed for review | A wrong-but-plausible join can still pass. Detected by the accuracy graders, not prevented. |
| Hypothesis generation is shallow — "seasonality" every time | `xhigh` effort; distinctness check in `design_test`; the quality rubric scores distinctness | Still requires human judgement to assess fully |
| Cost of `xhigh` reasoning on long investigations | Effort tiered per node; prompt caching of the large stable prefix; hard budget caps | Diagnostic questions remain materially more expensive than factual ones |
| The enumerated Python operation set limits flexibility | Covers the common analytical follow-ups | Some analyses are not expressible; documented as a limitation |
| The sample dataset is not a real warehouse | Schema and metric layer are configuration, not code | A real deployment needs its own metric definitions and its own column-policy review |

---

## Appendix A — Common standards compliance

The nine standards required of every project on this programme, and where each is satisfied.

| # | Standard | Where it is satisfied |
|---|---|---|
| 1 | A short design document before coding starts | This file — goal §1, tools §3, state §4, approval points §6, failure handling §7, security limits §8 — plus [architecture.md](architecture.md) and [security-controls.md](security-controls.md). Written and committed before any agent code. |
| 2 | Git repository with a clear README, tests, and basic CI | [../README.md](../README.md); `tests/unit` and `tests/integration` from Step 3 onward; GitHub Actions in Step 13, with the SQL-guard hostile-query suite as a required gate. |
| 3 | Dockerized setup that starts the required services | `docker-compose.yml` with `db`, `seed`, `api`, `ui` and healthchecks (Steps 2 and 13). Acceptance test: a clean clone plus `docker compose up`. |
| 4 | Structured logging, agent traces, and tool call history | §9 of this document; `run_steps`, `tool_calls`, `sql_audit`; `GET /v1/runs/{id}/trace`; the UI evidence drawer (Steps 3, 9, 11). |
| 5 | Persistent task state with recovery after errors, restarts, or delayed approval | §10; the LangGraph Postgres checkpointer (Step 7) and the durable approval pause and resume (Step 10). |
| 6 | At least three meaningful tools | Six: `metric_lookup`, `metric_query`, `schema_inspector`, `sql_runner`, `python_analysis`, `chart_builder` (§3, Step 6). |
| 7 | Human approval for high-impact actions | §6 — four gates, no bypass flag and no blanket-approve mode (Step 10). |
| 8 | A proper evaluation set with measurable success criteria, including failures, ambiguous tasks, and cases where the correct action is to stop or ask | §11 and [../plan.md](../plan.md) Step 12: 30+ questions in six categories, of which seven are cases where answering at all is the wrong behaviour. |
| 9 | A final technical report | `final-technical-report.md`, Step 14: architecture, tradeoffs, limitations, security controls, evaluation results, and what production would still require. |
