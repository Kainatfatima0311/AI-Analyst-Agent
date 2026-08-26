# Progress Log

Updated after **every** step, inside that step's own commit. Plan: [plan.md](plan.md).

Legend: ⬜ Pending · 🟨 In progress · ✅ Done · ⚠️ Done with known issue

## Status board

| Step | Title | Status | Date | Commit / Tag |
|---:|---|:---:|---|---|
| 0 | Repo skeleton, `plan.md`, `progress.md` | ✅ | 2026-08-24 | `chore(init)` |
| 1 | Design document | ✅ | 2026-08-24 | `docs(design)` / `v0.1-design` |
| 2 | Postgres in Docker, read-only role, seeded dataset | ✅ | 2026-08-24 | `feat(db)` |
| 3 | Config, structured logging, run/trace/audit persistence | ✅ | 2026-08-24 | `feat(core)` |
| 4 | `sql_guard` — SQL safety layer | ✅ | 2026-08-24 | `feat(sql-guard)` / `v0.2-sql-safety` |
| 5 | Metrics layer (approved KPI definitions) | ✅ | 2026-08-25 | `feat(metrics)` |
| 6 | The five tools | ✅ | 2026-08-25 | `feat(tools)` |
| 7 | LangGraph state, checkpointer, LLM wrapper | ✅ | 2026-08-26 | `feat(agent)` / `v0.3-agent-walking-skeleton` |
| 8 | Multi-hypothesis investigation loop | ✅ | 2026-08-26 | `feat(agent)` / `v0.4-investigation` |
| 9 | FastAPI business question API | ✅ | 2026-08-26 | `feat(api)` |
| 10 | Human approval gates and recovery | ✅ | 2026-08-26 | `feat(approvals)` / `v0.5-approvals` |
| 11 | Streamlit analyst interface | ✅ | 2026-08-26 | `feat(ui)` |
| 12 | Evaluation suite (≥30 questions) | ⚠️ | 2026-08-26 | `test(evals)` — built; scores need an API key |
| 13 | Dockerized stack, CI, README | ✅ | 2026-08-26 | `build(ci)` / `v0.7-deployable` |
| 14 | Final technical report | ✅ | 2026-08-26 | `docs(report)` / `v1.0` |

## Key metrics (filled in as they are measured)

| Metric | Target | Current | Measured at |
|---|---|---|---|
| Calculation accuracy (factual questions) | ≥ 90% | — | — |
| SQL safety violations | 0 | 0 | Step 2 — 29/29 read-only assertions pass |
| Diagnostic questions with ≥2 tested hypotheses | 100% | — | — |
| Ambiguous questions correctly deferred to a human | ≥ 90% | — | — |
| Unit + integration test coverage | ≥ 80% | — | — |
| Hostile queries rejected | 100% | 79/79 | Step 4 |
| Material findings reaching an answer untested | 0 | 0 | Step 8 — enforced by a graph edge |

---

## Step log

### Step 0 — Repo skeleton, `plan.md`, `progress.md`

**Status:** ✅ Done · **Date:** 2026-08-24

**Built**
- `.gitignore`, `.env.example`, `pyproject.toml` (runtime + dev dependencies, ruff / mypy /
  pytest configuration), `Makefile`, `README.md` stub.
- `plan.md` — the full fifteen-step plan that all later work executes against.
- `progress.md` — this file, with every step listed.
- Empty directory skeleton so the structure is visible from the first commit.

**Decisions recorded**
- Orchestration: LangGraph (explicit state graph, Postgres checkpointing, `interrupt()` for
  human approval).
- LLM: Anthropic Claude `claude-opus-5` through the official `anthropic` SDK, called directly
  from LangGraph nodes rather than via a LangChain LLM wrapper, so prompt caching, adaptive
  thinking, effort tiers and structured outputs stay under our control.
- Data: the public Olist Brazilian E-commerce dataset seeded into Postgres schema `analytics`.
- Interface: Streamlit, talking only to the FastAPI service.
- Two database roles from the start: `app_rw` for agent state, `analyst_ro` for every
  agent-generated query.

**Verification**
- `pip install -e ".[dev]"` in a fresh `.venv` — ✅ exit 0. Resolved versions: anthropic 1.0.0,
  langgraph 1.2.11, langgraph-checkpoint-postgres 3.1.2, sqlglot 30.17.0, fastapi 0.141.1,
  pandas 3.0.5, streamlit 1.62.0, psycopg 3.3.4, structlog 26.1.0.
- `ruff check .` — ✅ All checks passed.
- Dependency floors in `pyproject.toml` were then raised to match the installed majors
  (`anthropic>=1.0`, `langgraph>=1.2`, `langgraph-checkpoint-postgres>=3.1`, `sqlglot>=30.0`,
  `plotly>=6.0`, `kaleido>=1.0`) so CI resolves the same major versions we develop against.
  `pandas` and `numpy` are additionally capped below the next major.

**Notes / open items**
- `ANTHROPIC_API_KEY` is only required from Step 7 onward; Steps 0–6 are testable without it.

### Step 1 — Design document

**Status:** ✅ Done · **Date:** 2026-08-24 · **Tag:** `v0.1-design`

**Built**
- `docs/design-document.md` — agent goal and success criteria, explicit non-goals, system
  architecture and the rationale for LangGraph plus the Anthropic SDK directly, contracts for all
  five tools, the `AnalystState` schema with its two asserted invariants, the control-flow graph
  with the five policy rules encoded as edges rather than prompt text, per-node effort tiers, the
  four approval points, a failure-handling table, nine security limits, observability, persistence
  and recovery, the evaluation approach, and risks accepted at design time.
- `docs/architecture.md` — component map with its boundary rules, the runtime flow of a single
  question, the data model for both the analytical schema and the agent-state schema, the Docker
  deployment topology, and a technology-decision table with the alternatives considered.
- `docs/security-controls.md` — the four enforcement layers, ten controls each mapped to the threat
  it answers plus how it is tested and what remains residual, a threat-to-control matrix, and an
  explicit out-of-scope list.
- Appendix A of the design document maps all nine common standards to the section or step that
  satisfies each one.

**Verification**
- All nine common standards map to a named section or a numbered step — ✅ (Appendix A).
- Every tool named in §3 has a stated input, output, authority and failure behaviour — ✅ (5 of 5).
- Every security limit in §8 has a corresponding control with a test in `security-controls.md` —
  ✅ (C1–C10).

**Design notes worth flagging**
- `python_analysis` deliberately exposes a **fixed enumerated operation set** rather than executing
  model-written Python. This trades expressiveness for the removal of a whole class of
  sandbox-escape risk, and is recorded as a known limitation rather than hidden.
- The multi-hypothesis requirement is enforced by a **graph edge condition**, not by prompt
  wording: while a finding is marked material and has fewer than two hypotheses in a terminal
  state, the edge to `synthesize` is unavailable.
- Olist contains free-text customer reviews, which makes prompt injection from warehouse data a
  live threat rather than a hypothetical one. Control C6 addresses it, and the adversarial
  evaluation category will exercise it.

### Step 2 — Postgres in Docker, read-only role, seeded dataset

**Status:** ✅ Done · **Date:** 2026-08-24

**Built**
- `docker-compose.yml` with the `db` service (postgres:17), a named volume, a `pg_isready`
  healthcheck, and `db/init` mounted as the init directory.
- `db/init/00_bootstrap.sh` plus `db/init/sql/01_roles.sql`, `02_schema.sql`, `03_grants.sql`.
  The SQL files live in a subdirectory so the shell script controls their order and passes
  credentials in as psql variables instead of hard-coding them.
- Schema `analytics` mirroring the Olist dataset faithfully (original column spellings included,
  so real Kaggle CSVs load untransformed), plus `dim_date`, an `analytics.customer_contact`
  table giving the sensitive-column policy a real surface, and the `v_order_revenue` view that
  pre-aggregates items to order grain. Schema `agent` created empty for Step 3.
- `db/seed/download.py` — three sources tried in order: local CSVs, Kaggle (via
  `KAGGLE_USERNAME`/`KAGGLE_KEY`, no extra dependency), then the synthetic generator.
- `db/seed/generate.py` — a deterministic Olist-shaped generator with **planted ground truth**.
- `db/seed/load.py` — staging-table loader with `ON CONFLICT DO NOTHING` and foreign-key filters.
- `scripts/seed_db.py`, `scripts/smoke.py`, `tests/conftest.py`,
  `tests/integration/test_readonly_role.py`, `db/seed/README.md`.

**Verification**
- `docker compose up -d db` — healthy in 5s; the bootstrap applied all three SQL files with no
  errors.
- `python scripts/seed_db.py --source local` — ✅ 36,198 orders · 50,622 order items · 38,356
  payments · 24,583 reviews · 36,198 customers · 2,400 products · 600 sellers · 730 dim_date
  rows, period 2016-09 .. 2018-08, zero orphan rows, view returns 36,198 rows.
- `python scripts/smoke.py` — ✅ **29 passed, 0 failed.** INSERT/UPDATE/DELETE/TRUNCATE/CREATE/
  DROP/ALTER/CREATE INDEX/GRANT/CREATE ROLE all rejected with SQLSTATE 25006 (read-only
  transaction); `pg_authid` and `COPY FROM '/etc/passwd'` rejected with 42501; the four
  legitimate analytical reads all allowed; the grant surface asserted directly
  (`has_schema_privilege('agent','USAGE')` is false, `usesuper` is false); the statement timeout
  fires on `pg_sleep(5)`.
- `pytest -q` — 29 passed. `ruff check .` — clean.

**Deviations from the plan, and why**
- The plan listed `api`, `ui` and `seed` services in this step's compose file. They are deferred
  to Step 13, where the `Dockerfile` they all depend on is written. Shipping a compose file
  referencing a non-existent build context would have been broken-on-arrival; until then
  seeding runs from the host with `python scripts/seed_db.py`.
- The plan assumed the Olist CSVs would simply be downloaded. Kaggle requires authentication, so
  a **deterministic synthetic generator** was added as a third source. This turned out to be
  more than a fallback: it is what gives the diagnostic evaluation questions real ground truth.
- The design document named "customer name, email, phone, street address" as the sensitive
  columns. The real Olist dataset contains none of those, so `analytics.customer_contact` was
  added to carry exactly those columns (populated by the generator, empty on the Kaggle path),
  and `customer_unique_id` plus precise `geolocation_lat`/`lng` were added to the sensitive set.
  The policy is unchanged in substance and is now bound to columns that actually exist.

**Planted ground truth for later steps**
Shock month `2018-03`: net revenue falls 279,292 â†’ 190,058 (âˆ’32%) while order volume stays on
trend (1,741 → 1,791). Two real causes act together — the premium-category share drops from 0.28
to 0.11, and orders with an `SP` seller run late at 0.34 vs 0.08, pushing cancellations from
1.6% to 11.2%. One decoy: review scores fall in the same month, but downstream of the delays.
One prompt-injection attempt sits in a review comment inside that month. All of it is written to
`db/seed/raw/_manifest.json` so the eval suite reads ground truth instead of hard-coding it.

### Step 3 — Config, structured logging, run/trace/audit persistence

**Status:** ✅ Done · **Date:** 2026-08-24

**Built**
- `src/analyst_agent/config.py` — pydantic-settings. The two DSNs are **separate typed fields**
  rather than one connection string with a role switch, so mis-wiring the tool layer is a
  visible mistake instead of a silent privilege escalation. Per-node effort tiers
  (`low`/`high`/`xhigh`), SQL safety limits, and budget caps all live here.
- `src/analyst_agent/observability/logging.py` — structlog routed *through* stdlib logging, so
  our events and uvicorn's / psycopg's output come out as one consistent JSON stream instead of
  two interleaved formats. `run_id` / `step_id` / `node` / `tool` / `query_id` bind via
  contextvars, so a node binds once and everything beneath it inherits the context. A
  `redact_secrets` processor scrubs registered secrets from every value including nested dicts
  and exception text; `truncate_sql` keeps log lines readable since the full statement is always
  in `sql_audit` anyway.
- `db/migrations/001_agent_state.sql` — schema `agent` with 9 tables: `runs`, `run_steps`,
  `tool_calls`, `sql_audit`, `approvals`, `findings`, `hypotheses`, `charts`,
  `schema_migrations`.
- `scripts/migrate.py` — a plain migration runner with per-file checksums, so editing an
  already-applied migration is detected rather than silently diverging. Deliberately not
  Alembic: the schema carries security-relevant CHECK constraints and those should stay
  reviewable as SQL.
- `src/analyst_agent/db/engine.py` — two pools. The read-only pool additionally pins
  `default_transaction_read_only=on` and the statement timeout at *session* level on top of the
  role settings, and `assert_read_only()` turns a mis-pasted DSN into a startup failure instead
  of an incident found later.
- `src/analyst_agent/db/repository.py` — every self-observation write, plus `get_trace()`, the
  full reconstruction the API and the UI evidence drawer are built on. A `step()` context
  manager records node entry/exit and binds the log context in one place.
- `tests/unit/test_logging.py` (13 tests), `tests/integration/test_repository.py` (14 tests).

**Four invariants moved out of application code and into the schema**
A CHECK constraint cannot be forgotten by a node written six steps from now, so the design
document's promises are enforced by the database:

| Constraint | What it prevents |
|---|---|
| `findings_require_evidence` | reporting a finding with no query behind it |
| `hypotheses_require_a_test` | an untested hypothesis becoming a verdict |
| `sql_audit_executed_implies_allowed` | a tool-layer bug recording an execution for a query the guard rejected |
| `approvals_decision_is_attributed` | a decision with no decider and no timestamp |

All four are asserted as tests that expect `CheckViolation`, not just documented.

**Verification**
- `python scripts/migrate.py` — ✅ applied `001_agent_state`; `--status` reports 1 applied, 0
  pending. 9 tables and 10 check constraints present in schema `agent`.
- `engine.assert_read_only()` — ✅ read-only pool verified as `analyst_ro`, non-superuser.
- `pytest -q` — ✅ **56 passed** (13 logging/config unit, 14 repository integration, 29
  read-only role).
- `ruff check .` — clean. `mypy` — ✅ no issues in 18 source files.

**Two real bugs found and fixed while verifying**
- `ANTHROPIC_API_KEY=` (the empty placeholder that `.env.example` ships) was being read as a
  *configured* empty key, so `require_api_key()` returned `""` and the failure would have
  surfaced much later at the first model call. A `before` validator now treats blank as absent.
- `ALLOWED_SCHEMAS=analytics` in `.env` raised a `SettingsError`, because pydantic-settings
  tries to JSON-parse a tuple field from dotenv *before* validators run. Fixed with
  `Annotated[..., NoDecode]` so the comma-splitting validator actually gets the raw string.

### Step 4 — `sql_guard`, the SQL safety layer

**Status:** ✅ Done · **Date:** 2026-08-24 · **Tag:** `v0.2-sql-safety`

**Built**
- `sql_guard/errors.py` — `GuardVerdict` with `allowed` and `requires_approval` as *separate*
  fields, because they answer different questions: a query can be structurally fine and still
  need a human. Reason codes are stable strings so tests, the audit table and the eval graders
  share one vocabulary.
- `sql_guard/policy.py` — the policy as **data**: 20 denied node types, 30 denied functions,
  forbidden schemas, and the sensitive-column registry. Reviewable on its own, separate from
  the walk that applies it.
- `sql_guard/catalog.py` — a committed `STATIC_CATALOG` snapshot (12 objects, 79 columns)
  alongside `load_catalog()` from `information_schema`. The snapshot is what lets the whole
  hostile-query suite run in CI **with no database**.
- `sql_guard/column_policy.py` — three sensitivity tiers, because "sensitive" is not one thing.
- `sql_guard/validator.py` — the AST walk.
- `sql_guard/explain_gate.py` — `EXPLAIN` (never `ANALYZE`) with a cost ceiling; over it,
  escalate rather than block.
- `sql_guard/__init__.py` — `check()`, the single entry point, running the layers in order.

**The finding that shaped the design**
Probing sqlglot before writing anything showed that

```sql
WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x
```

parses with a **`Select` at the root**. A root-statement-type check — the obvious
implementation, and the one a keyword denylist amounts to — would have cleared a deletion.
Denied node types are therefore matched *anywhere in the tree*, and the root check is only the
first of six gates. Five variants of this attack are in the suite.

**Three sensitivity tiers, each with a reason**

| Tier | Columns | Rule | Why |
|---|---|---|---|
| `direct_identifier` | `customer_contact.{full_name,email,phone,street_address}` | restricted anywhere outside an approved aggregate, **including a WHERE filter** | filtering by an email address is a person-level lookup, not analysis |
| `pseudonymous` | `customers.customer_unique_id` | only *projecting* it in the outermost select is restricted | grouping and joining on it is ordinary work — the approved repeat-customer metric needs exactly that; projecting it is what yields one row per person |
| `precise_location` | `geolocation.{lat,lng}` | aggregate yes, return no; `min`/`max` excluded | `min(lat)` returns a real observed coordinate, which is a disclosure rather than an aggregate |

A restricted column **escalates and is never silently stripped** — the reviewer sees exactly
what was asked for, and the agent is not handed a quietly rewritten result to reason over.

**Verification — the security regression net**
`pytest tests/unit/test_sql_guard.py` â†’ **127 passed**, no database required:

- **79 hostile queries, all rejected.** 8 statement-stacking shapes, 5 DML-at-root, 5
  DML-hidden-in-a-CTE, 8 DDL, 9 privilege/session/non-SELECT commands, 3 SELECT-shaped
  non-reads (`INTO`, `FOR UPDATE`), 16 dangerous-function calls including three buried in
  subqueries, 11 catalog/forbidden-schema reads, 5 out-of-allowlist objects, 3 unbounded
  cartesian products, 4 unparseable or empty, and 6 comment/casing tricks.
- **14 escalations** — restricted columns, wildcard projections, and scalar functions wrapping
  a restricted column.
- **18 legitimate queries, all allowed** — including both planted-cause diagnostic queries,
  window functions, set operations, and the approved-aggregate cases. A guard that blocks real
  work gets switched off, so this half of the corpus matters as much as the hostile half.
- One asserted global property: **no hostile query ever produces runnable SQL** — a rejection
  yields `rewritten_sql is None`, so there is nothing for a buggy caller to execute.

Full suite: `pytest -q` â†’ **190 passed**. `ruff` clean, `mypy` clean (24 files),
`scripts/smoke.py` still 29/29.

**Two real bugs the corpus caught**
1. **`count(*)` was read as a wildcard projection.** The `*` in `count(*)` is an `exp.Star`, so
   every `count(*)` over a table that happens to hold a restricted column escalated —
   `SELECT geolocation_state, count(*) FROM analytics.geolocation GROUP BY 1` among them. A
   guard that escalates ordinary aggregation is a guard that gets disabled. Fixed by skipping
   a `Star` whose parent is a function.
2. **A CTE could shadow a forbidden object — a genuine bypass.** CTE aliases must be excluded
   from the catalog check, but the code skipped any table whose *name* matched a CTE alias.
   So `WITH pg_authid AS (SELECT 1) SELECT * FROM pg_catalog.pg_authid` skipped the allowlist
   entirely and was **allowed**. The same trick worked for `public.secrets`. In SQL a qualified
   name can never resolve to a CTE, so only unqualified names are now treated as CTE
   references. Both cases are regression tests.

**Drift protection**
`tests/integration/test_sql_guard_live.py` asserts `STATIC_CATALOG` still matches the live
schema object-for-object and column-for-column. Without it, a migration could leave the unit
suite validating against a schema that no longer exists — and passing while doing so.

**Note**
Changing the `ro_conn` test fixture to `dict_row` (matching what `db/engine.py` configures on
the real pools) broke five Step 2 tests that indexed rows as tuples. They were updated rather
than the fixture reverted: a test should exercise the row shape the production code assumes.

### Step 5 — Metrics layer (approved KPI definitions)

**Status:** ✅ Done · **Date:** 2026-08-25

**Built**
- `metrics/loader.py` — pydantic models for a definition, with `extra="forbid"` so a typo in a
  field name fails at startup rather than being silently ignored, and a shape validator that
  refuses a half-filled definition.
- `metrics/definitions/*.yaml` — **12 approved metrics**: `revenue`, `gross_revenue`, `orders`,
  `units`, `aov`, `cancellation_rate`, `on_time_delivery_rate`, `avg_delivery_days`,
  `avg_review_score`, `freight_ratio`, `repeat_customer_rate`, `seller_concentration`.
  73 lookup keys across names, titles and aliases.
- `metrics/registry.py` — resolution and rendering.
- `scripts/generate_metrics_catalog.py` and the generated
  [docs/metrics-catalog.md](docs/metrics-catalog.md) (522 lines).

**The design decision that does the real work**
A metric is deliberately **not** "a blob of SQL". It declares an aggregate expression, the tables
it reads, its filter, its date column, and an allow-list of dimensions each carrying a reviewed
SQL expression. The registry assembles the statement from those parts, and values travel as
bound parameters.

The consequence is structural rather than advisory: **for an approved metric, no free text from
the model reaches SQL.** The model picks a *name* — `revenue`, broken down by `product_category`,
filtered to `month = 2018-03` — and every name maps to an expression a human wrote and reviewed.
A hostile filter value stays a value; the test asserting that a `'; DROP TABLE ...` filter comes
back as a bound parameter and returns zero rows is the proof.

Two metrics (`repeat_customer_rate`, `seller_concentration`) genuinely do not fit that mould —
one needs a per-person subquery, the other a window function. They declare `shape: custom` and
carry their own statement, and are held to the same bar a different way: every rendered metric,
custom or not, is asserted to pass `sql_guard`.

**Where the two halves of the project meet**
`repeat_customer_rate` groups on `customer_unique_id` and never projects it. That is exactly what
the `pseudonymous` tier from Step 4 was designed to permit, and it is why the tier exists: a
blanket ban on the person key would have made the approved retention metric need human approval
on every single run. The test asserting the key is absent from the outermost projection pins that
down.

**Verification**
- `pytest tests/unit/test_metrics_registry.py` — **55 passed**, no database. Covers alias
  resolution across 21 phrasings, refusal of 8 unapproved terms (`ltv`, `churn`, `gross margin`,
  `conversion rate`, …), suggestions on a near miss, rejection of duplicate names and of an
  ambiguous alias, parameter binding, injection-in-a-filter-value, undeclared dimensions,
  join deduplication, and custom-shape rules.
- `pytest tests/integration/test_metric_sql.py` — **53 passed**. Every one of the 12 metrics is
  asserted to (a) pass `sql_guard`, (b) execute and return a non-null number over the whole
  dataset, (c) execute over a date window, and (d) work for **every dimension it declares** —
  a wrong join or expression would otherwise only surface mid-run.
- Three tests reach through the metric layer to the planted ground truth: the revenue drop in
  `2018-03`, the category mix shift, and the cancellation spike. A fourth asserts the **decoy**
  moves too — review scores fall in the same month — because the trap has to genuinely be in the
  data for Step 8's refutation logic to be worth testing.
- Full suite: `pytest -q` â†’ **298 passed**. `ruff` clean, `mypy` clean (26 files).

**Catalogue freshness is a test**
`docs/metrics-catalog.md` is generated, and a unit test runs the generator with `--check` and
fails if the committed file is stale. A hand-edited catalogue that disagrees with the registry
would be worse than having none: a reviewer would be checking the agent's arithmetic against the
wrong formula.

**Two small things caught while building**
- Dimensions initially referenced aliases (`c.`, `p.`, `s.`) that the base `from_sql` did not
  provide. Each dimension now declares the `join` it needs, appended only when that dimension is
  used — carrying every possible join would both slow the common case and risk changing the row
  count through a join that fans out.
- Two caveats written with a colon in them parsed as YAML mappings rather than strings. The
  loader's validation caught both at load time, which is the argument for validating definitions
  at startup rather than trusting them.

### Step 6 — The five tools

**Status:** ✅ Done · **Date:** 2026-08-25

**Built**
- `tools/base.py` — one `Tool` base class carrying validation, timing, the audit write and
  logging, so a new tool cannot quietly skip the audit. Plus `anthropic_tool_schema`, which
  makes a pydantic model `strict`-compatible: `additionalProperties: false`, every property in
  `required`, and optional arguments expressed as **nullable** rather than omitted.
- `tools/frames.py` — a bounded LRU frame store with **rehydration from the audit trail**.
- `tools/metric_lookup.py`, `tools/schema_inspector.py`, `tools/sql_runner.py`,
  `tools/python_analysis.py`, `tools/chart_builder.py`, `tools/palette.py`, `tools/registry.py`.

**Two conventions that shaped every tool**
- **A refusal is a result, not an error.** When a tool declines — no approved metric, a query the
  guard rejected, a chart that would mislead — it returns `ok=True` with a `refusal`. Conflating
  that with a crash would lose the distinction in the trace and would teach the model to retry
  rather than change course. `tool_calls` has a separate `refusal` column for exactly this.
- **Nothing returns silently empty.** An empty result set comes back as a success whose summary
  says *check whether the filter is right before concluding there is no data*.

**`python_analysis`: the deliberate limitation**
The obvious implementation is to let the model write pandas and `exec` it. Instead the tool
exposes **eight enumerated operations** — describe, group_by, share_of_total,
period_over_period, rolling, correlation, top_n, linear_fit — each implemented here and validated
against the frame's real columns. Some analyses are therefore not expressible; that is recorded
as a known limitation. What it buys is that **no model-authored code executes anywhere in this
system**. The `correlation` summary carries its own warning that association is not cause, since
the summary is the part the model reliably reads.

**Frames survive a restart**
`python_analysis` and `chart_builder` work on a previous `query_id`, so that result has to live
somewhere between calls. Keeping it purely in memory breaks the recovery Step 10 requires, so a
missing frame is **rebuilt by re-running the statement recorded in `sql_audit`**. Only queries the
audit records as `allowed` and `executed` can be rehydrated — asserted by a test — so this cannot
become a route to run something the guard refused.

**Charts: palette validated, not chosen**
The categorical palette was run through the data-viz validator rather than picked by eye: on the
light surface all checks pass (worst adjacent CVD Î”E 9.1, normal-vision 19.6) with a contrast warn
on three slots that obliges *relief* — every chart is returned alongside its data and rendered
next to the table, which is that relief. Dark passes outright. Three rules are enforced in code:
no dual axis ever (refused, with the alternatives named); hues assigned in fixed slot order and
never cycled, with the tail folding into "Other" **and the fold reported**; and a legend only for
two or more series. Scatter compares every pair at once and the full eight cannot clear the
all-pairs floors, so scatter caps at the three slots that do.

**Verification**
- `pytest tests/unit/test_tool_base.py` — **21 passed**, no database. Schema strictness, the
  refusal-versus-error contract, invalid and unknown arguments, and a check that **every argument
  of every tool is documented** — an undocumented argument is one the model will guess at.
- `pytest tests/integration/test_tools.py` — **26 passed**. All five tools, their refusal paths,
  frame rehydration, the rejected-query rehydration guard, series folding, the scatter cap, and a
  chain test asserting `metric_lookup â†’ sql_runner â†’ python_analysis â†’ chart_builder` is fully
  accounted for in the trace with a duration on every call.
- Full suite: **345 passed**. `ruff` clean, `mypy` clean (35 files). The Step 4 guard suite is
  still 127/127.

**Two real bugs found while verifying**
1. **Every monetary figure reached the model as a string.** Postgres returns `numeric` as a
   `Decimal`, and a `Decimal` has neither `isoformat` nor `item`, so the coercion helper fell
   through to `str()` and `sum(oi.price)` arrived as `"139184.93"`. The model would then either
   compare numbers lexically or burn a turn parsing them — exactly what that helper existed to
   prevent. `Decimal` is now handled first and explicitly, with a test asserting the types.
2. **Foreign keys came back empty for every table.** `information_schema.constraint_column_usage`
   only returns rows for tables the current user *owns*, so as `analyst_ro` every table looked
   unrelated to every other — and the agent needs those join paths to write correct SQL. Rewritten
   against `pg_constraint`, which is readable, with a test asserting `order_items` resolves all
   three of its parents.

Also caught: the Plotly spec contained numpy arrays, which psycopg cannot store as `jsonb`.
Serialising through Plotly's own encoder fixes it and is cheaper than writing a converter.

### Step 7 — LangGraph state, checkpointer, LLM wrapper

**Status:** ✅ Done · **Date:** 2026-08-26 · **Tag:** `v0.3-agent-walking-skeleton`

**Built**
- `agent/state.py` — `AnalystState` as a TypedDict with `operator.add` reducers on the
  append-only lists, plus the predicates the graph routes on (`executed_query_ids`,
  `material_findings`, `tested_hypotheses`, `every_finding_has_evidence`).
- `agent/budget.py` — five simultaneous caps; the first to bind stops the run.
- `agent/llm.py` — the only module in the project that imports `anthropic`.
- `agent/checkpointer.py` — `PostgresSaver` on the `app_rw` pool, its own pool because the
  checkpointer needs autocommit and would otherwise change that under the repository.
- `agent/prompts.py` — the cached stable prefix (~1,240 tokens: role, safety rules, schema card,
  metric catalogue).
- `agent/nodes/schemas.py`, `agent/nodes/linear.py`, `agent/graph.py`.
- `tests/fakes.py` — a scripted model, so the graph is testable with no API key.

**Claude API conventions, applied**
`claude-opus-5` un-suffixed; `thinking={"type": "adaptive"}` and no `budget_tokens` (rejected
outright on this model); the cost lever is `output_config.effort`, tiered per node — `low` for
the clarify gate, `high` for SQL authoring and interpretation, `xhigh` for synthesis. Server-side
fallbacks are **enabled by default** (`betas=["server-side-fallback-2026-07-01"]`,
`fallbacks="default"`) so a safety refusal reroutes by category instead of failing the run, and
`stop_reason == "refusal"` is checked *before* the content is read. Retries use a
most-specific-first chain: a 404, a 400 or an auth error is raised immediately rather than
retried, because it will fail identically next time and retrying hides it behind a timeout.

**Why the nodes take their dependencies by injection**
Every node is a closure over an LLM and a tool registry rather than reaching for a global. That
is not a testing nicety: the **routing is where the policy lives** — "stop and ask", "park on an
escalation", "a spent budget still produces an answer" — and routing has to be asserted
deterministically, not through whatever the model happens to say. A scripted model drives the
whole graph in CI with no key.

**Three conditional edges, already**
`clarify_gate` routes to END when the question cannot be answered as asked; `execute` routes to
END when the guard escalated, so there is no path around an approval; `author_sql` routes
straight to `synthesize` when the budget is spent, which is what turns exhaustion into a partial
answer rather than an exception. Step 8 replaces the single `interpret â†’ synthesize` edge with
the investigation loop, and that edge becomes the one enforcing "a material finding needs two
tested hypotheses" — the shape here is deliberately the shape that edge slots into.

**Verification**
- `pytest tests/unit/test_agent_state.py` — **15 passed**, no database. Budget limits, the
  extension grant surviving a restore, the wall clock deliberately restarting on resume, and each
  routing predicate.
- `pytest tests/integration/test_graph_linear.py` — **9 passed**. End to end through all eight
  nodes; per-node effort tiers asserted rather than assumed; an ambiguous question parking at
  `clarifying` with **zero queries considered**; an unapproved metric recorded as unapproved; an
  escalated query parking at `awaiting_approval` with `synthesize` never reached; a rejected
  query still producing an answer that cites nothing; a spent budget producing a `truncated` run
  with a stated reason.
- **The recovery test discards the graph object and rebuilds it between the two halves** — which
  is what a process restart amounts to. Nothing carries the run forward but the checkpoint, and
  the resumed half continues rather than starting over (`intake` appears exactly once).
- Full suite: **369 passed**. `ruff` clean, `mypy` clean (42 files). Guard suite still 127/127.

**Blocked, and deliberately not faked**
`ANTHROPIC_API_KEY` is not set, so no test has yet driven a **real** model call. Everything above
is asserted against a scripted model. The wrapper is written to the current API and type-checks,
but "it works against the live API" is not yet established, and is not claimed. Add the key to
`.env` and the `llm`-marked tests become runnable.

**Three real problems found while building**
1. **A CTE-style state bug: metric placeholders duplicated.** `clarify_gate` emitted placeholder
   rows into `resolved_metrics`, which uses an append-only reducer — so the real resolution
   *appended* rather than replacing, leaving both in state and both in the model's context. The
   terms now travel as scratch and only the authoritative resolution is recorded.
2. **numpy's type stubs need Python 3.12+ syntax**, which broke `mypy` under the declared floor
   of 3.11. Rather than paper over it, `requires-python` moved to `>=3.12`: we develop and test on
   3.13 and have never run 3.11, so the old floor was a claim we could not support.
3. **mypy cannot bind LangGraph's `add_node` overloads when the node arrives as a `Callable`
   alias** — verified empirically with a minimal repro: the same call type-checks with an inline
   `def` and fails with an aliased one. Since the nodes come from factories by design, this is
   handled with a single documented ignore in one helper rather than eight scattered ones.

### Step 8 — Multi-hypothesis investigation loop

**Status:** OK Done - 2026-08-26 - Tag: `v0.4-investigation`

**Built**
- `agent/nodes/investigate.py` - `materiality_check`, `generate_hypotheses`, `test_hypothesis`,
  `reconcile`.
- `agent/distinctness.py` - two checks for "this is the same hypothesis written twice".
- `agent/nodes/schemas.py` - `HypothesisSet`, `HypothesisEvaluation`, `Reconciliation`.
- `db/migrations/002_inconclusive_needs_no_test.sql`.
- Graph rewired: the `interpret -> synthesize` edge is **gone**. Every path to an answer now
  goes through the materiality gate.

**The gate**
While a material finding has fewer than two hypotheses in a terminal state, the route to
synthesis is *unavailable* - the only edge out of `materiality_check` goes to hypothesis
generation. That is why the requirement lives in the graph rather than the prompt: there is
nothing here for a model to talk its way past. `synthesis_is_blocked()` exposes the same rule as
a predicate so it can be asserted directly, not only through whichever path the graph took.

**Distinctness: what is enforced, and what is not**
Two hypotheses that predict the same thing are one hypothesis, and testing both looks like rigour
while establishing nothing. Two checks, and they are honest about their reach:

- A lexical check on each hypothesis's declared `distinguishing_signal`, applied at generation.
  It catches near-verbatim restatements **and nothing more** - it scores "review scores fell"
  against "customer satisfaction ratings declined" at zero. There is a test asserting that
  limitation, so nobody later mistakes it for a paraphrase detector.
- The check that actually holds: two hypotheses whose tests normalise to the **same SQL** are the
  same hypothesis whatever they are called, because no result could separate them. The second is
  marked inconclusive and never executed.

**Confidence is capped, not requested**
`reconcile` asks the model for a confidence and then lowers it against what the tests established:
more than one explanation still supported caps at `low`; a competing explanation left
inconclusive caps at `medium`. `synthesize` caps again on the same basis, and once more if a
material finding was left unexplained. The node can only lower it.

**The loop guard**
A finding for which the model cannot produce two *distinct* explanations would otherwise be
picked again on every pass, generating duplicates forever. Once a finding has as many hypotheses
as it is allowed and still lacks two tested ones, the investigation moves on and the answer must
say the finding was not fully explained rather than presenting it as if it were.

**Verification**
- `pytest tests/unit/test_distinctness.py` - 11 passed.
- `pytest tests/integration/test_multi_hypothesis.py` - 11 passed: the gate predicate on its own
  (a proposed hypothesis does not open it; one tested is not enough; a *refutation* counts,
  because the point is that the alternative was tested, not that it won), then the loop end to
  end - both explanations tested, the refuted one reaching the answer, an inconclusive
  alternative downgrading confidence to `medium`, two survivors capping it at `low`, a duplicate
  rejected at generation, and two explanations sharing one test query where the second is marked
  inconclusive and never runs.
- Full suite: **393 passed**. ruff clean, mypy clean (44 files).

**A constraint that was slightly too broad**
`hypotheses_require_a_test` demanded a query for any status other than `proposed`. The first real
run of the loop hit it: a hypothesis whose test would duplicate a sibling's is `inconclusive` and
has, correctly, no query of its own. Demanding one forces either a misrepresentation (attach
someone else's query) or leaves the hypothesis at `proposed`, which blocks the synthesis gate
permanently. Migration 002 narrows it - a query is required for a verdict that *claims* something
(`supported`, `refuted`) and not for one that declines to - and adds a constraint that an
`inconclusive` verdict must carry a stated reason.

**A real bug the loop surfaced**
`repo.step` closed the step on the way out even when the node had already closed it, overwriting
the node's summary with NULL. Every node summary written since Step 3 was being silently
discarded - emptying the one human-readable column in the trace, which is most of what makes a
run reviewable. Fixed, with two regression tests.

**Environment note**
Mid-step the `D:` drive was deleted. It held Git and the Python the venv was built from, so every
command failed at once. Recovered by rebuilding the venv from `C:\ProgramData\miniconda3` (same
3.13.5) and reinstalling Git to `K:\software\Git`; 17 dead `D:` PATH entries removed. No repo
history was lost. Six loop tests written just before the shell died had to be rewritten.

### Step 9 — FastAPI business question API

**Status:** OK Done - 2026-08-26

**Built**
- `api/schemas.py` - request and response shapes, kept separate from `AnalystState` so an
  internal refactor is not a breaking API change.
- `api/service.py` - one place that turns stored run state into what a caller sees, so the run
  view and the trace cannot drift into disagreeing about the same run.
- `api/main.py` - the app: `POST /v1/questions`, `GET /v1/runs`, `/v1/runs/{id}`,
  `/v1/runs/{id}/trace`, `/v1/runs/{id}/stream` (SSE), `/v1/runs/{id}/approvals` with
  approve/reject, `/v1/runs/{id}/answer`, `/v1/metrics`, `/v1/schema`, `/healthz`, `/readyz`.

**202, not 200**
A question returns a `run_id` immediately and the graph runs in the background. An investigation
takes minutes and can pause for a human, so holding the connection open would both time out and
make the approval flow impossible.

**Startup refuses to lie about safety**
`lifespan` calls `assert_read_only()`. A mis-pasted DSN becomes a startup failure rather than an
incident found later. It also expires stale approvals and *reports* resumable runs without
auto-resuming them - a run parked on a human decision should wait for that human, not restart
because the service rebooted.

**Deciding twice is a 409**
The first decision is the one that was made. A second silently replacing it would falsify the
audit, so it is refused and the original stands.

**Verification**
- `pytest tests/integration/test_api.py` - **21 passed**: health and readiness, request-id
  echo, validation (empty question, malformed uuid, unknown field), 404 and 422 paths, the
  metric catalogue with its caveats, the schema endpoint flagging restricted columns rather than
  hiding them, an answer resolving its evidence to real queries **with the SQL travelling
  alongside the claim**, findings carrying their hypotheses, the approval round trip including
  the double-decision 409, and an OpenAPI check that every documented route exists.
- Full suite: **414 passed**. ruff clean, mypy clean (47 files).

**Note**
The first draft of `ask()` created a run row, deleted it, and created another - a leftover from
working out where the run id should come from. Replaced with one row created in the request and
driven by `graph.drive()` against that same id, which is why `_drive` became public.

### Step 10 — Human approval gates and recovery

**Status:** OK Done - 2026-08-26 - Tag: `v0.5-approvals`

**The gap this closed**
Step 9 left the run parking at `awaiting_approval` with **no approval row written**. The guard
escalated, the graph stopped, and nobody could see what it was waiting on. The gate existed and
was unusable.

**Built**
- `agent/approvals.py` - requesting an approval and, more importantly, honouring one.
- `sql_runner` now takes an `approval_id`, and `graph.resume_after_decision()` carries the run
  forward once a person has answered.
- `db/migrations/003_approved_verdict.sql`.
- API: approve and reject now resume the run in the background.

**Consent cannot be manufactured**
An escalated statement runs only when four things hold, all checked against the **stored row**
rather than against anything the caller said: the approval exists, it belongs to this run, a
human approved it, and the statement matches the one they were shown. The last one is a
whitespace-normalised fingerprint - deliberately not semantic, because what a reviewer agreed to
was the text in front of them, not a slot in the flow. Tests cover all four: passing the id of a
*pending* approval does not clear it, an invented id is refused, and swapping the statement after
approval is refused with "granted for a different statement".

**Both outcomes carry the run forward**
Approved: the *same* statement runs again, not something re-authored afterwards. Rejected or
timed out: the draft is dropped, the run answers with what it could establish, and the refusal is
recorded in the run's own error list. A timeout is *written down* as a decision with its reason
rather than inferred from the clock at read time.

**Verification**
- `pytest tests/integration/test_approvals.py` - **13 passed**, including the one that matters:
  the graph object is discarded and rebuilt between parking and resuming, so nothing carries the
  run forward but the checkpoint. `intake` still appears exactly once.
- Full suite: **427 passed**. ruff clean, mypy clean (48 files).

**A distinction the audit was missing**
Running under an approval collided with `sql_audit_executed_implies_allowed`. The cause was that
`verdict` had no value for the outcome a reviewer most wants to see: escalated by the guard, then
cleared by a person. Recording it as `allowed` would have erased the escalation from the trail.
Migration 003 adds `approved` as its own verdict, so the audit now distinguishes "the guard
permitted this" from "a person permitted this", and the executed constraint accepts both while
still refusing a rejected or undecided one.

### Step 11 — Streamlit analyst interface

**Status:** OK Done - 2026-08-26

**Built**
- `ui/theme.py` - design tokens and one stylesheet.
- `ui/api_client.py` - the UI's only route into the system.
- `ui/components.py` - one function per rendered thing.
- `ui/streamlit_app.py` - the page.

**The colours are not chosen here**
They come from `tools/palette.py`, the palette already run through the data-viz validator in
Step 6. A chart and the card around it have to read as one system, and the alternative is a UI
whose accents quietly disagree with its own charts. A test asserts the accent is drawn from that
palette, and another asserts no status colour is reused as a series colour - status is reserved,
and "failed" wearing the same hue as "series 4" would be a lie.

**Status is never carried by colour alone**
Every state ships as a chip with an icon *and* a label. A refuted hypothesis reads as "✕ Refuted"
in a screenshot, in print, and to someone who cannot separate the green from the red. There is a
test over every status key that both are present.

**The layout follows one idea**
A conclusion is worth what the evidence you can reach from it is worth. So the answer, its
confidence, what was **ruled out**, and the SQL behind every cited number are on one screen -
and the queries the guard *refused* are on the next tab rather than hidden, because what the
agent tried is usually what a reviewer wants to know. Both approval buttons are equally
prominent: rejection is a real answer, not the discouraged path.

Light and dark are both defined, tokens swapping in one place rather than a filter flipped over
the light theme.

**Verification**
- The API and the UI were both started for real: `/healthz`, `/readyz`, `/v1/metrics` (12),
  `/v1/schema` (12 objects) and `/docs` all answer; Streamlit serves on 8501.
- `pytest tests/integration/test_ui.py` - **14 passed**. A 200 from a Streamlit page proves
  almost nothing, since it renders client-side, so these use `AppTest`, which executes the script
  the way Streamlit does and surfaces any exception. Covered: the landing page and a finished run
  both render clean, the conclusion and its confidence appear, what was ruled out appears beside
  the answer, both hypotheses show with their verdicts, the SQL behind the answer is reachable,
  a **blocked** query is visible with its reason, a parked run shows the exact statement plus
  both buttons, and the design-token rules above.
- Full suite: **441 passed**. ruff clean, mypy clean (51 files).

**Two things that bit**
- `ui/components/` existed as an empty package from the Step 0 skeleton and shadowed the new
  `components.py`, so every `ui.*` call resolved to nothing. Removed the package.
- The first UI test patched the page module, which does nothing: `AppTest` re-executes the script
  in a fresh namespace each run. Patching the *dependency* it imports works, and the caches have
  to be cleared too, or a real client cached by `st.cache_resource` outlives the patch.

### Step 12 — Evaluation suite

**Status:** OK Built and verified; scores not yet measured - 2026-08-26

**Built**
- `evals/schema.py` - what a question *is*, with validation that catches an incoherent one.
- `evals/questions/*.yaml` - **32 questions** in six categories.
- `evals/graders/calculation.py`, `sql_safety.py`, `analytical_quality.py`.
- `evals/runner.py` - runs the suite, writes JSON and Markdown reports.

**Composition**

| Category | N | What it measures |
|---|---|---|
| factual | 8 | calculation accuracy against a hand-written reference query |
| comparison | 7 | multi-step correctness across periods and segments |
| diagnostic | 6 | does it test more than one explanation before concluding |
| ambiguous | 4 | does it stop and ask rather than guessing |
| out_of_scope | 3 | does it say the data cannot answer this |
| adversarial | 4 | does policy hold under pressure |

**Seven of the 32 must not be answered at all.** Four require a clarification, three a refusal,
and four more adversarial ones require a refusal or an escalation. A suite made only of answerable
questions measures fluency rather than judgement: it would score an agent that confidently
invents a churn figure exactly as highly as one that says there is no approved definition. A test
asserts that proportion holds, so the suite cannot quietly drift into being easy.

**The three graders do different jobs**
- *Calculation* compares against a number computed by executing the question's own reference
  query. "It was right" becomes a measurement rather than an opinion about whether the prose
  sounded correct.
- *Safety* produces a verdict, not a score: **one violation fails the whole suite.** A guard that
  holds for thirty-one questions and gives way on the thirty-second has not held, and averaging
  that away would hide the only number here that matters.
- *Quality* is split. The mechanical half needs no model and carries the weight - did it stop to
  ask, did it test enough explanations, does every cited query actually exist. The judged half is
  an LLM against a fixed rubric for what only reading can settle, reported separately and never
  allowed to overturn the mechanical result. A model marking its own homework is worth something,
  but not that much.

**`--validate` runs without an API key, and immediately earned its place**
It executes every reference query and puts each through the SQL guard. On the first run it caught
a bug in one of *my* reference queries - `corr(row_number() OVER (...), aov)` nests a window
function inside an aggregate, which Postgres rejects. Without that mode the question would have
silently graded every future run against nothing.

**Verification**
- `python -m evals.runner --validate` - all 15 reference queries execute and pass the guard.
- `pytest tests/unit/test_evals.py` - **29 passed**. The graders are measurement equipment, so
  they are fed runs whose correct score is known: a leaked email address, a query that ran
  without clearance, a non-SELECT that executed, a query with no verdict recorded, a confident
  answer to an ambiguous question, a diagnostic run with only one explanation, a *proposed*
  explanation being counted as tested, and an answer citing a query that never ran. Each is
  asserted to be caught.
- The runner was executed for real and produced a report.
- Full suite: **470 passed**. ruff clean, mypy clean (51 files).

**Not yet measured, and not claimed**
`ANTHROPIC_API_KEY` is still unset, so no question has been run against a real model. The three
out-of-scope questions were executed end to end and correctly recorded as errored with
`ANTHROPIC_API_KEY is not set` - which proves the harness, the graders and the report all work,
and proves nothing about the agent's accuracy. **There is no baseline report yet.** The key
metrics table in this file stays empty until there is one, rather than being filled with numbers
from a scripted model.

### Step 13 — Dockerized stack, CI, README

**Status:** ✅ Done · **Date:** 2026-08-26 · **Tag:** `v0.7-deployable`

**Built**
- `Dockerfile` — multi-stage, non-root (uid 10001), dependency metadata copied before source so a
  code change does not invalidate the install layer. One image serves both API and UI: they share
  every dependency and differ only in the command.
- `docker-compose.yml` — `db`, a one-shot `seed`, `api`, `ui`. Ordering is *enforced*, not hoped
  for: `api` waits on `seed` completing successfully, and `seed` waits on `db` being healthy.
  Without that the first request can hit an empty schema and the failure looks like an agent bug.
- `.github/workflows/ci.yml` — three jobs.
- `README.md` — rewritten as the front door rather than a stub.

**CI is shaped around what can run where**
The `static` job needs no database at all, because the guard suite runs against a committed
catalogue snapshot — so lint, types and **79 hostile queries** are the fast feedback. The
`integration` job applies the real `db/init` SQL against a service container, which doubles as a
test that those scripts work outside Docker, then seeds synthetically (never Kaggle: CI must not
depend on credentials or a network download), asserts control C1 *before* anything else, and runs
`evals --validate`. The `docker` job is the acceptance test: build, `compose up --wait`, and probe
the API's own readiness.

The guard suite is a named step so a failure reads as what it is. If a hostile query gets through,
nothing else about that build matters.

**Verification — the stack actually started**
- `docker compose build` → image `analyst-agent:latest`, 1.08 GB, running as `uid=10001(analyst)`.
- `docker compose up --wait api` → `db` healthy, `seed` exited 0, `api` **healthy**.
- Probed from outside: `/healthz`, `/readyz` (`read_only_verified: true`), `/v1/metrics` (12),
  `/v1/schema` (12 objects), `/docs` 200.
- Inside the database: 36,198 orders loaded, 3 migrations applied.
- `docker compose exec api python scripts/smoke.py` → **29/29**. Control C1 verified from inside
  the container, not just on the host.
- `ui` came up healthy and serves.
- Full suite on the host: **470 passed**. ruff clean, mypy clean (51 files).

**Two things worth recording**
- The first `compose up` failed on `port is already allocated` — an unrelated project's container
  already had 8000. Every published port is parameterised, so the fix was
  `$env:API_PORT="8010"` rather than editing the compose file, and the README now says so.
  Someone else's container is not a reason to edit shared configuration.
- A half-created container from that failed attempt stayed attached to a network `down -v` had
  already removed, which surfaced as `failed to resolve host 'db'` — a DNS error that looked like
  a compose misconfiguration and was not. `--force-recreate` cleared it.

### Step 14 — Final technical report

**Status:** ✅ Done · **Date:** 2026-08-26 · **Tag:** `v1.0`

`docs/final-technical-report.md`: where the project stands, the architecture, the seven decisions
that shaped it *with what each one cost*, the security posture, what went wrong and what it
taught, the evaluation instrument and what it has not yet told us, known limitations, and ten
things production would still require in the order they would matter.

**It leads with what has not been verified.** No agent behaviour has been run against a real
model — every one is verified against a scripted model, because `ANTHROPIC_API_KEY` was never
available. The report states that in its first section rather than its last, and separates the two
kinds of claim throughout: a routing bug is a bug in this repository, while a weak hypothesis is a
property of the model and the prompt and would show up as an eval score. That score does not
exist.

**Also fixed in this step: an encoding fault of my own making.**
`Set-Content -Encoding utf8` on Windows PowerShell 5.1 writes a BOM and round-trips through
cp1252, which double-encoded `README.md`, `progress.md`, `api/main.py`, `tests/integration/test_ui.py`
and the `Makefile` — a middle dot (`U+00B7`, bytes `C2 B7`) came back out as two characters
(`C3 82 C2 B7`). Found by scanning every text file for those marker bytes rather than by noticing
it in a diff.

`progress.md` needed three passes and then a rebuild. A whole-file reverse failed on the first
unmappable byte; a line-by-line pass got most of it; and some lines had been through the
round-trip *twice*, so one reverse was not enough. Worse, every status-board update this session
was a PowerShell `-replace` with the emoji typed literally in the pattern — once the file was
double-encoded those patterns no longer matched, and `-replace` reports nothing when it matches
nothing, so steps 9 through 13 silently stayed marked pending while their entries were being
appended below. The board was rebuilt from an explicit list rather than patched again, because
patching is precisely what failed.

All files are now clean UTF-8 with no BOM. The lesson is narrow and worth keeping: on Windows
PowerShell 5.1, do not use `Set-Content`/`Add-Content` to write a file containing non-ASCII text,
and do not trust a `-replace` that reports no error.
