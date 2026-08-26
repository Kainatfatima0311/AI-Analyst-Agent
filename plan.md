# Build Plan — AI Data Analyst & Business Intelligence Agent

This is the authoritative, step-by-step plan. Work is executed against it in order.
After every step, [progress.md](progress.md) is updated in the **same commit** as the step's code.

---

## 1. Goal

An agent that takes a business question, identifies the data it needs, writes **safe** SQL,
inspects the results, **tests more than one plausible explanation**, runs follow-up Python
analysis, and presents a supported conclusion with charts or tables — where every claim is
traceable back to the exact queries and rows that produced it.

It must not behave like a chat box over a database. Three hard rules drive the whole design:

1. **Read-only database access, and every generated query is validated before execution.**
2. **Multi-hypothesis investigation** — any material finding must have ≥2 competing
   explanations tested before a conclusion is allowed.
3. **Traceability** — every conclusion carries the run id, the SQL, the row counts, and the
   version of the metric definition used.

## 2. Stack

| Concern | Choice | Why |
|---|---|---|
| API | FastAPI | Async, typed, auto docs |
| Orchestration | LangGraph | Explicit state graph, Postgres checkpointing, `interrupt()` for approvals |
| LLM | Anthropic Claude (`claude-opus-5`) via the official `anthropic` SDK | Direct control over thinking, effort, prompt caching, structured outputs |
| Database | PostgreSQL 17 (Docker) | Two roles: `app_rw` for agent state, `analyst_ro` for all generated SQL |
| SQL safety | `sqlglot` AST validation | Parsing, not regex — regex validation is bypassable |
| Analysis | pandas | Follow-up analysis over a prior query's result frame |
| Charts | Plotly (+ kaleido for PNG) | Interactive in the UI, static for reports |
| UI | Streamlit | Chat, live trace, charts, approval controls in one Python app |
| Metrics layer | YAML KPI registry | Approved definitions; the agent may not invent a formula |

LangGraph supplies the graph, the state, and the checkpointer. The LLM calls go through a
single thin wrapper (`agent/llm.py`) using the Anthropic SDK directly — no LangChain LLM
abstraction, so prompt caching, adaptive thinking, effort tiers, and structured outputs stay
under our control.

### Claude API conventions used throughout

- Model id: `claude-opus-5` (env-configurable). Never date-suffixed.
- `thinking={"type": "adaptive"}` — never `budget_tokens` (rejected with 400 on Opus 5).
- Cost/latency lever is `output_config={"effort": ...}`: `low` for cheap classifier nodes,
  `high` for SQL authoring and interpretation, `xhigh` for hypothesis generation and synthesis.
- The schema card + metric registry + safety rules form a large **stable prefix** →
  `cache_control: {"type": "ephemeral"}` on the system block, volatile question text after it.
  Cache effectiveness verified via `usage.cache_read_input_tokens`.
- `.stream()` + `.get_final_message()` for large outputs; `max_tokens` 16000 non-streaming,
  64000 streaming.
- Structured outputs via `output_config={"format": {...}}` for SQL plans, hypothesis lists and
  grader verdicts. Tools declared with `strict: true`. No assistant prefill (400 on Opus 5).
- `stop_reason == "refusal"` is handled explicitly; server-side fallbacks enabled.

> **This is the plan as written before implementation, and it is left that way on purpose.**
> Two things in it were superseded during the work — Streamlit as the interface, and "five tools"
> where there are now six — and both divergences are recorded in §9 and in
> [progress.md](progress.md) rather than edited out here. A plan rewritten to look prescient is
> not a record of anything.

## 3. Architecture

```
Streamlit UI ──HTTP──> FastAPI ──> LangGraph agent (Postgres checkpointer)
                          │              │
                          │              ├─ tool: metric_lookup     (approved KPI registry)
                          │              ├─ tool: schema_inspector   (allow-listed schema)
                          │              ├─ tool: sql_runner        ─┐
                          │              ├─ tool: python_analysis    │  every query passes
                          │              └─ tool: chart_builder      │  sql_guard first
                          │                                          │
                          └─ runs / traces / audit tables <──────────┘
                                                     read-only Postgres role
```

## 4. Repository layout

```
AI-Analyst-Agent/
├── plan.md                      # this file
├── progress.md                  # updated after every step
├── README.md  .env.example  .gitignore  pyproject.toml  Makefile
├── docker-compose.yml  Dockerfile  .dockerignore
├── docs/
│   ├── design-document.md          # Step 1 — written before agent code
│   ├── architecture.md  security-controls.md  metrics-catalog.md
│   └── final-technical-report.md   # Step 14
├── src/analyst_agent/
│   ├── config.py                   # pydantic-settings
│   ├── api/        main.py  routes/  schemas.py  dependencies.py
│   ├── agent/      graph.py  state.py  llm.py  checkpointer.py  budget.py
│   │               nodes/  prompts/
│   ├── tools/      metric_lookup.py  schema_inspector.py  sql_runner.py
│   │               python_analysis.py  chart_builder.py  registry.py
│   ├── sql_guard/  validator.py  policy.py  column_policy.py  explain_gate.py  errors.py
│   ├── metrics/    registry.py  loader.py  definitions/*.yaml
│   ├── db/         engine.py  session.py  models.py  repository.py
│   ├── observability/  logging.py  trace.py  audit.py
│   └── ui/         streamlit_app.py  components/
├── db/
│   ├── init/       01_roles.sql  02_schema.sql  03_grants.sql
│   └── seed/       download.py  load.py  README.md
├── evals/
│   ├── questions/  q001..q030+.yaml
│   ├── runner.py  graders/  reports/
├── tests/          unit/  integration/  conftest.py  fixtures/
├── scripts/        bootstrap.ps1  seed_db.py  smoke.py
└── .github/workflows/ci.yml
```

## 5. Git workflow

One commit per step, conventional-commit messages, a tag at each milestone.
`progress.md` is updated inside the step's own commit — never as a separate commit.

**Pushes are performed manually by the repository owner.** Each step lists its commands.

```powershell
# once (Step 0)
git init
git branch -M main
git remote add origin <REPO_URL>

# after each step
git add -A
git commit -m "<type>(<scope>): <summary>"
git push -u origin main      # run manually
```

---

## 6. Steps

Every step has the same shape: **build → verify → update `progress.md` → commit.**

### Step 0 — Repo skeleton, `plan.md`, `progress.md`

`.gitignore`, `README.md` stub, `pyproject.toml` (runtime + dev dependencies, ruff/mypy/pytest
config), `.env.example`, `Makefile`, this `plan.md`, and `progress.md` with every step listed as
pending.

**Verify:** `pip install -e ".[dev]"` succeeds in a fresh virtualenv; `ruff check .` is clean.

```powershell
git init; git branch -M main; git remote add origin <REPO_URL>
git add -A; git commit -m "chore(init): repo skeleton, plan.md, progress.md, tooling config"
```

### Step 1 — Design document (before any agent code)

`docs/design-document.md`: agent goal · tool inventory and contracts · state schema · approval
points · failure handling and retry/abort policy · security limits (read-only role, AST
validation, sensitive-column policy, budget caps) · explicit out-of-scope list.
`docs/architecture.md` holds the diagram and the data flow.

**Verify:** each of the nine common standards maps to a named section here or to a later step.

```powershell
git add -A; git commit -m "docs(design): agent design document and architecture overview"
git tag v0.1-design
```

### Step 2 — Postgres in Docker, read-only role, seeded dataset

`docker-compose.yml` with `db`, a one-shot `seed`, `api`, `ui`.
`db/init/01_roles.sql` creates `analyst_ro`: `GRANT CONNECT`, `USAGE` + `SELECT` on the
`analytics` schema **only**, `REVOKE ALL ON SCHEMA public FROM PUBLIC`,
`ALTER ROLE analyst_ro SET default_transaction_read_only = on`, plus `statement_timeout` and
`idle_in_transaction_session_timeout`. A separate `app_rw` role owns the agent's own tables
(runs, traces, audit, checkpoints) — the SQL tool never receives that connection.

`db/seed/download.py` + `load.py` load the Olist Brazilian E-commerce dataset (orders,
order_items, customers, products, sellers, payments, reviews, geolocation) into schema
`analytics`, plus a date dimension helper.

**Verify:** `scripts/smoke.py` asserts (a) expected row counts per table, (b) `analyst_ro` can
`SELECT`, (c) `analyst_ro` `INSERT`/`UPDATE`/`CREATE`/`DROP` all raise, (d) the statement
timeout fires on `pg_sleep`.

```powershell
git add -A; git commit -m "feat(db): dockerized postgres, read-only analyst role, olist seed pipeline"
```

### Step 3 — Config, structured logging, run/trace/audit persistence

`config.py` (pydantic-settings, two DSNs), `db/engine.py` (separate pools; the read-only pool
pinned to `analyst_ro` with `options=-c default_transaction_read_only=on`),
`observability/logging.py` (structlog JSON with `run_id` / `step_id` / `tool` bound on every
line), `db/models.py` + migration for `runs`, `run_steps`, `tool_calls`, `sql_audit`,
`approvals`, `findings`.

**Verify:** a fabricated run writes and reads back a complete trace.

```powershell
git add -A; git commit -m "feat(core): settings, structured logging, run/trace/audit persistence"
```

### Step 4 — `sql_guard`: the SQL safety layer

Built early because it is the highest-risk component. `validator.py` parses with **sqlglot** and
enforces, on the AST rather than by regex:

- exactly one statement; the root must be `Select` or a `With`-wrapped `Select`;
- reject every DDL/DML/DCL node, `COPY`, `CALL`, `DO`, `SET`/`RESET`, and dangerous functions
  (`pg_read_file`, `pg_ls_dir`, `lo_*`, `dblink`, `pg_sleep`);
- table/schema allowlist derived from the seeded schema — anything outside `analytics` is
  rejected; `pg_catalog` / `information_schema` are reachable only through `schema_inspector`,
  never through `sql_runner`;
- `column_policy.py` blocks sensitive columns (customer name, email, phone, street address,
  precise geolocation, payment identifiers) from projection while still permitting approved
  aggregates over them. A sensitive projection is **not** silently dropped — it raises and
  becomes an approval request;
- clamp or inject `LIMIT` (default 5 000); reject unbounded cross joins;
- `explain_gate.py` runs `EXPLAIN` (never `ANALYZE`) first and rejects or escalates plans above
  a cost threshold.

Every decision returns a structured `GuardVerdict(allowed, reasons[], rewritten_sql,
requires_approval)` that is written to `sql_audit`.

**Verify:** `tests/unit/test_sql_guard.py` — a table-driven suite of **≥60 hostile queries**
(stacked statements, CTE-wrapped `DELETE`, `UPDATE … RETURNING`, comment tricks, unicode
escapes, `information_schema` exfiltration, sensitive-column projection, function-based file
reads) all rejected, and ~15 legitimate analytical queries all allowed with correct rewrites.
This suite is the project's security regression net and must stay green.

```powershell
git add -A; git commit -m "feat(sql-guard): AST-based SQL validation, column policy, explain cost gate"
git tag v0.2-sql-safety
```

### Step 5 — Metrics layer (approved KPI definitions)

`metrics/definitions/*.yaml`, one file per KPI: `name`, `description`, `owner`, `version`,
`grain`, parameterized `sql_template`, `dimensions[]`, `filters`, `caveats`, `sensitive`.
Seed roughly twelve KPIs for this dataset: revenue, orders, AOV, units, on-time delivery rate,
average delivery days, review score, repeat-customer rate, category mix, seller concentration,
cancellation rate, freight ratio.

`registry.py` loads and validates them at startup and renders SQL from a template with bound
parameters. **The agent may not invent a metric formula** — if a question names a metric with no
registry entry, the graph must ask, or explicitly flag the answer as using an unapproved ad-hoc
definition.

**Verify:** unit tests for schema validation, alias resolution, parameter binding and rejection
of unknown metrics; an integration test executes every KPI template against the seeded database
and asserts a non-null numeric result. `docs/metrics-catalog.md` is generated from the YAML.

```powershell
git add -A; git commit -m "feat(metrics): approved KPI registry, definitions, generated catalog"
```

### Step 6 — The five tools

Each tool is a pydantic input model, a `strict: true` Anthropic tool schema, an executor, and an
audit write. All are registered in `tools/registry.py`.

1. **`metric_lookup`** — resolve a business term to an approved definition, or report that no
   approved definition exists.
2. **`schema_inspector`** — allow-listed tables, columns, types, row-count estimates and sample
   distinct values for low-cardinality columns. Never returns sensitive column values.
3. **`sql_runner`** — `sql_guard.validate()` → read-only pool → timeout → row cap → returns rows
   plus `query_id`, timing and a truncation flag. Refuses anything the guard did not approve.
4. **`python_analysis`** — a constrained pandas step over a *prior* `query_id`'s result frame: no
   network, no filesystem, an import allowlist, wall-clock and memory caps. Supports groupby,
   pivot, rolling windows, correlation, share-of-total, period-over-period and simple regression.
5. **`chart_builder`** — a Plotly figure from a result frame, returning both spec and PNG.

**Verify:** per-tool unit tests including every refusal path, plus an integration test doing
metric → sql → python → chart end to end.

```powershell
git add -A; git commit -m "feat(tools): metric lookup, schema inspector, safe sql runner, python analysis, charts"
```

### Step 7 — LangGraph state, checkpointer, LLM wrapper

`agent/state.py` — a typed `AnalystState`: question, clarifications, resolved metrics, plan
steps, executed queries (`query_id` → sql → summary), findings, hypotheses (each
`proposed` / `testing` / `supported` / `refuted`), charts, approvals, budget counters, errors.

`agent/checkpointer.py` — `PostgresSaver` on the `app_rw` DSN, so a run survives a process
restart and an arbitrarily long approval wait.

`agent/llm.py` — the single Anthropic entry point: model from config, adaptive thinking,
per-node effort, cached system prefix, structured-output helper, refusal and fallback handling,
retries via a typed-exception chain (`NotFoundError` → `RateLimitError` → `APIStatusError` →
`APIConnectionError`), and token accounting into `budget.py`.

Graph v1 (linear, no investigation loop yet):
`intake → clarify_gate → resolve_metrics → plan → author_sql → guard → execute → interpret → synthesize`.

**Verify:** an integration test answers "What was total revenue by month in 2017?" end to end;
killing the process mid-run and resuming by `thread_id` continues from the checkpoint.

```powershell
git add -A; git commit -m "feat(agent): langgraph state, postgres checkpointer, anthropic llm wrapper, linear graph"
git tag v0.3-agent-walking-skeleton
```

### Step 8 — Multi-hypothesis investigation loop

The differentiating step. The graph is extended to:

`interpret → materiality_check → generate_hypotheses (≥2, xhigh effort) → [per hypothesis: design_test → author_sql → guard → execute → evaluate] → reconcile → follow_up? → synthesize`

These rules are encoded in the **graph**, not merely in the prompt:

- a finding flagged material cannot reach `synthesize` with fewer than two tested hypotheses —
  the edge condition blocks it;
- each hypothesis carries its own falsifying test and its own `query_id`; a hypothesis with no
  distinguishing test is rejected at design time;
- `reconcile` must state which explanations were **refuted** and why, not only which one won;
- confidence is downgraded when competing hypotheses remain indistinguishable, and the answer
  says so;
- loop bounds come from `budget.py` (max iterations, max queries, max tokens, wall clock). On
  exhaustion the agent returns partial findings with an explicit "investigation truncated" note
  rather than an unsupported conclusion.

**Verify:** an integration test on a question with a known confound in the seeded data ("why did
revenue drop in month X?", where both category-mix shift and delivery delays are present) must
produce ≥2 tested hypotheses with distinct SQL; a second assertion proves a material finding
cannot short-circuit to `synthesize`.

```powershell
git add -A; git commit -m "feat(agent): multi-hypothesis generation, falsification tests, reconciliation"
git tag v0.4-investigation
```

### Step 9 — FastAPI: business question API

Routes: `POST /v1/questions` (start a run, return `run_id`) · `GET /v1/runs/{id}` (status,
findings, charts) · `GET /v1/runs/{id}/stream` (SSE progress) · `GET /v1/runs/{id}/trace` (tool
calls, SQL, guard verdicts) · `POST /v1/runs/{id}/approve|reject` · `GET /v1/metrics` (KPI
catalog) · `GET /v1/schema` · `GET /healthz`, `GET /readyz`.
Request-id middleware, structlog correlation, typed error envelopes, and a rate limit on
question submission.

**Verify:** integration tests for the happy path, validation errors, unknown run, and the
approval round-trip; `/docs` renders.

```powershell
git add -A; git commit -m "feat(api): question submission, run status, trace, approval, SSE endpoints"
```

### Step 10 — Human approval gates and recovery

`interrupt()`-based gates at four points: a query the `explain_gate` flags as expensive; a query
touching a restricted or sensitive column; budget exhaustion requesting an extension; and
exporting or publishing a report. Approval requests persist to `approvals` with the exact SQL and
the reason; the run resumes from its checkpoint when the decision arrives — **including after an
API restart**. A timeout auto-rejects with a recorded reason. No blanket permissions anywhere.

**Verify:** pause a run, restart the process, approve, and see it complete; the reject path
produces a documented partial answer; the timeout path is recorded.

```powershell
git add -A; git commit -m "feat(approvals): human-in-the-loop gates with durable pause and resume"
git tag v0.5-approvals
```

### Step 11 — Streamlit analyst interface

`ui/streamlit_app.py`: question box with metric autocomplete from `/v1/metrics`, a live step
timeline driven by SSE, findings panel, chart and table rendering, a **"Show the evidence"**
drawer mapping each claim to its SQL, guard verdict, row count and metric definition version, a
hypothesis panel showing supported versus refuted, a pending-approval banner with approve/reject
buttons, and run history. The UI talks only to the API — it never touches the database.

**Verify:** `docker compose up`, then walk three questions (a simple metric, a comparison, and a
"why" question), capturing screenshots for the final report.

```powershell
git add -A; git commit -m "feat(ui): streamlit analyst interface with evidence drawer and approval controls"
```

### Step 12 — Evaluation suite (≥30 business questions)

`evals/questions/*.yaml`, each with `id`, `question`, `category`, `expected_behavior`,
`ground_truth_sql` (executed directly to compute the reference number), `tolerance`,
`must_ask_clarification`, `must_refuse`, `rubric_notes`.

| Category | Count | Purpose |
|---|---|---|
| Single-metric factual | 8 | calculation accuracy against `ground_truth_sql` |
| Comparison / trend / segmentation | 7 | multi-step correctness |
| Diagnostic "why" questions | 6 | ≥2 tested hypotheses required |
| Ambiguous / underspecified | 4 | the correct action is to **ask**, not guess |
| Out-of-scope / unanswerable | 3 | the correct action is to **stop and say so** |
| Adversarial / policy | 4 | prompt-injected DDL, sensitive-column requests, unapproved metric definitions → refuse or escalate |

Graders: `calculation.py` (numeric match within tolerance), `sql_safety.py` (zero non-`SELECT`
executions, zero policy bypasses, a guard verdict recorded for every query — any breach fails the
whole suite), `analytical_quality.py` (LLM-as-judge against a fixed rubric via structured
outputs: hypothesis count and distinctness, evidence-to-claim linkage, honesty about refutation,
calibrated confidence).

`runner.py` executes every question and writes `evals/reports/<timestamp>.{json,md}` with
per-category pass rates plus cost and latency.

**Verify:** the suite runs headless; a baseline report is published and its numbers recorded in
`progress.md`. Every failure gets a written diagnosis — fixed, or documented as a known
limitation.

```powershell
git add -A; git commit -m "test(evals): 30+ question suite, accuracy/safety/quality graders, baseline report"
git tag v0.6-evals
```

### Step 13 — Dockerized stack, CI, README

Multi-stage non-root `Dockerfile`; `docker-compose.yml` finalised with `db`, `seed`, `api`, `ui`,
healthchecks and `depends_on: service_healthy`. `.github/workflows/ci.yml`: ruff → mypy → unit
tests → integration tests against a service-container Postgres → the `sql_guard` hostile-query
suite as a required gate → image build. README: architecture diagram, one-command start
(`docker compose up`), environment variables, API examples, how to run tests and evals, security
notes, screenshots.

**Verify:** a clean clone plus `docker compose up` produces a working stack with **no
host-machine dependency beyond Docker**; CI is green.

```powershell
git add -A; git commit -m "build(ci): dockerized stack, github actions pipeline, README"
git tag v0.7-deployable
```

### Step 14 — Final technical report

`docs/final-technical-report.md`: architecture and data flow · design tradeoffs (LangGraph vs a
custom loop, direct SDK vs LangChain, sqlglot AST vs regex validation, sample data vs synthetic)
· security controls and the threat each one answers · evaluation results with per-category
analysis and failure walkthroughs · known limitations · **what would still be required before a
real production rollout** (row-level security, result caching, multi-tenant isolation, cost
governance, PII review, an on-call runbook, drift monitoring for metric definitions).

A final `progress.md` pass marks every step complete and links the tags and reports.

**Verify:** the report is cross-checked against the actual eval numbers, not aspirational ones.

```powershell
git add -A; git commit -m "docs(report): final technical report and completed progress log"
git tag v1.0
```

---

## 7. End-to-end verification

```powershell
# 1. stack
docker compose up -d db
python scripts/seed_db.py
python scripts/smoke.py

docker compose up -d              # api + ui

# 2. safety net — must be green at all times
pytest tests/unit/test_sql_guard.py -v

# 3. full test suite
pytest -v --cov=src/analyst_agent

# 4. the agent, live
curl -X POST localhost:8000/v1/questions -H "content-type: application/json" `
     -d '{"question":"Why did revenue drop in November 2017?"}'
curl localhost:8000/v1/runs/<id>/trace     # every claim traceable to SQL

# 5. evaluation
python -m evals.runner --all --report evals/reports/
```

**Definition of done, per step:** it builds, its tests pass, `progress.md` is updated, one commit
is made.
**Definition of done, overall:** `docker compose up` on a clean clone yields a working stack, CI
is green, the eval report exists with real measured numbers, and the final report is written.

## 8. Open items settled during execution

- `ANTHROPIC_API_KEY` must be present in `.env` before Step 7. Everything through Step 6 is
  testable without it.
- Olist download source: a small committed subset serves as the CI fixture so integration tests
  never depend on a network download.
- The exact `explain_gate` cost threshold and the default `LIMIT` are tuned in Step 12 against
  real eval runs.

---

## 9. Beyond the original plan

The fourteen steps above were the plan as written before implementation, and they were executed as
written. Five further steps followed, each recorded in [progress.md](progress.md) with what it
found:

| Step | What it added | Why it was not in the original plan |
|---:|---|---|
| 15 | **Design-conformance pass** — four gaps closed | It is a *check* on the plan rather than a step of it: the implementation was read back against `docs/design-document.md` and four places had quietly settled for less than the document promised |
| 16 | **Groq backend**, result rows reaching synthesis, a hand-written frontend replacing Streamlit | No Anthropic credit was available; the first live run then exposed a real defect; and the supplied UI design could not be reproduced on a widget toolkit |
| 17 | **Phase 2** — dashboards, saved reports, PDF/Excel/PNG exports, a decomposable confidence score | Requested after v1.0: turn an analyst tool into a product |
| 18 | **Phase 3** — organisations, teams, encrypted data sources, share links, monitoring alerts, an audit trail | Requested after v1.0: serve more than one company |
| 19 | **The answer as an eight-section BI report** | Requested after v1.0: the output should read as an analyst's report rather than a chat reply |

Two things about that list are worth saying plainly.

**The plan held.** Nothing in steps 0–14 was abandoned or reversed. Streamlit was replaced, which
was a *decision* recorded in this plan rather than a requirement of the brief — the brief asked for
an "analyst interface" and named no technology.

**Every phase after v1.0 found a real bug in the phase before it**, and each is written up rather
than quietly fixed: a saved report landing in the wrong organisation, an alert query with no run to
trace it to, monthly figures reconstructed from prose because the values never reached synthesis,
and an audit trail that could not reproduce a parameterised query. Those are the entries in
`progress.md` worth reading, because they are where the design was wrong rather than merely
incomplete.
