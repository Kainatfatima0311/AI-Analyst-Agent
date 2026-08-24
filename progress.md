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
| 5 | Metrics layer (approved KPI definitions) | 🟨 | — | — |
| 6 | The five tools | ⬜ | — | — |
| 7 | LangGraph state, checkpointer, LLM wrapper | ⬜ | — | — |
| 8 | Multi-hypothesis investigation loop | ⬜ | — | — |
| 9 | FastAPI business question API | ⬜ | — | — |
| 10 | Human approval gates and recovery | ⬜ | — | — |
| 11 | Streamlit analyst interface | ⬜ | — | — |
| 12 | Evaluation suite (≥30 questions) | ⬜ | — | — |
| 13 | Dockerized stack, CI, README | ⬜ | — | — |
| 14 | Final technical report | ⬜ | — | — |

## Key metrics (filled in as they are measured)

| Metric | Target | Current | Measured at |
|---|---|---|---|
| Calculation accuracy (factual questions) | ≥ 90% | — | — |
| SQL safety violations | 0 | 0 | Step 2 — 29/29 read-only assertions pass |
| Diagnostic questions with ≥2 tested hypotheses | 100% | — | — |
| Ambiguous questions correctly deferred to a human | ≥ 90% | — | — |
| Unit + integration test coverage | ≥ 80% | — | — |
| Hostile queries rejected | 100% | 79/79 | Step 4 |

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
Shock month `2018-03`: net revenue falls 279,292 → 190,058 (−32%) while order volume stays on
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
`pytest tests/unit/test_sql_guard.py` → **127 passed**, no database required:

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

Full suite: `pytest -q` → **190 passed**. `ruff` clean, `mypy` clean (24 files),
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
