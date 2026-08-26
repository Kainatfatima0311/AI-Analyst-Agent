# Architecture

Companion to [design-document.md](design-document.md). This file covers component boundaries, the
runtime flow of a single question, the data model, and the deployment topology.

---

## 1. Component map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Presentation                                                                 │
│   ui/streamlit_app.py — question box, live timeline, findings, charts,       │
│   evidence drawer, approval controls.  Talks only to the API over HTTP/SSE.  │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ HTTP + SSE
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ Service — api/                                                               │
│   routes/questions.py  POST /v1/questions        start a run                 │
│   routes/runs.py       GET  /v1/runs/{id}        status, findings, charts    │
│                        GET  /v1/runs/{id}/stream SSE progress               │
│                        GET  /v1/runs/{id}/trace  full reconstruction        │
│   routes/approvals.py  POST /v1/runs/{id}/approve | /reject                  │
│   routes/catalog.py    GET  /v1/metrics, GET /v1/schema                      │
│   routes/health.py     GET  /healthz, /readyz                                │
│   Middleware: request id, structlog binding, typed error envelopes, rate     │
│   limiting on question submission.                                          │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ start / resume by thread_id
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ Orchestration — agent/                                                       │
│   graph.py         the LangGraph state graph and its policy edges            │
│   state.py         AnalystState                                              │
│   nodes/           intake, clarify_gate, resolve_metrics, gather_context,     │
│                    plan, compute_metrics, author_sql, execute, interpret,    │
│                    analyse, materiality_check, generate_hypotheses,          │
│                    design_test, evaluate, reconcile, synthesize, visualize,  │
│                    respond                                                   │
│   tool_loop.py     the bounded tool-calling loop used by 4 of those nodes    │
│   distinctness.py  refuses a hypothesis that restates a sibling             │
│   approvals.py     durable pause: expensive query, restricted column, budget │
│   llm.py           the single Anthropic SDK entry point                      │
│   checkpointer.py  PostgresSaver on the app_rw DSN                           │
│   budget.py        query / token / iteration / wall-clock caps               │
└───────┬──────────────────────────────────────────────────┬───────────────────┘
        │ tool calls                                       │ state after every node
┌───────▼──────────────────────────────┐        ┌──────────▼───────────────────┐
│ Capability — tools/                  │        │ agent state (app_rw)         │
│   metric_lookup      → metrics/      │        │  langgraph checkpoints       │
│   metric_query       ─┐→ metrics/    │        │  runs, run_steps             │
│   schema_inspector    │              │        │  tool_calls, sql_audit       │
│   sql_runner         ─┤→ sql_guard/  │        │  approvals, findings         │
│   python_analysis     │              │        └──────────────────────────────┘
│   chart_builder      ─┘              │
└───────┬──────────────────────────────┘
        │ validated SELECT only
┌───────▼──────────────────────────────────────────────────────────────────────┐
│ Data — analytics schema, accessed as analyst_ro                              │
│   orders · order_items · customers · products · sellers · payments ·         │
│   reviews · geolocation · dim_date                                           │
│   read-only role · statement timeout · idle-transaction timeout · row cap    │
└──────────────────────────────────────────────────────────────────────────────┘
```

Boundary rules that hold everywhere:

- The UI never touches the database. Everything it shows comes from the API.
- No node calls Postgres directly for analytical data. Every analytical read goes through
  `sql_runner`, which means it goes through `sql_guard`.
- The agent's own state uses a different role (`app_rw`) than analytical reads (`analyst_ro`), and
  the tool layer is only ever handed the read-only DSN.
- `llm.py` is the only module that imports `anthropic`. Nodes describe *what* they want; the wrapper
  owns model id, thinking, effort, caching, retries and refusal handling.

---

## 2. Runtime flow of one question

```
UI                API                  Graph                  Guard        Postgres
│                  │                    │                      │              │
├─ POST /questions ─>                   │                      │              │
│                  ├─ create run row ───────────────────────────────────────> │ runs
│                  ├─ start thread ────>│                      │              │
│  <─ 202 {run_id} ─┤                    │                      │              │
├─ GET /stream ────>│ (SSE open)         │                      │              │
│                  │                    ├─ intake              │              │
│  <─ event: step ─┤<───────────────────┤                      │              │
│                  │                    ├─ clarify_gate        │              │
│                  │                    │   ambiguous? ──> pause, status=clarifying
│                  │                    ├─ resolve_metrics ────────────────> metrics registry
│                  │                    ├─ gather_context ─> schema_inspector, metric_lookup
│                  │                    ├─ plan                │              │
│                  │                    ├─ compute_metrics ─> metric_query ──>│ (rendered by
│                  │                    │   answered? ──> interpret           │  the registry)
│                  │                    ├─ author_sql          │              │
│                  │                    ├─ sql_runner ────────>│ validate     │
│                  │                    │                      ├─ EXPLAIN ──> │ (analyst_ro)
│                  │                    │            allowed <─┤              │
│                  │                    │                      ├─ SELECT ───> │ (analyst_ro)
│                  │                    │<── rows + query_id ──┤              │
│                  │                    ├─ write sql_audit ────────────────> │ sql_audit
│                  │                    ├─ interpret           │              │
│                  │                    ├─ analyse ─> python_analysis (no new query)
│                  │                    ├─ materiality_check   │              │
│                  │                    ├─ generate_hypotheses (>= 2)         │
│                  │                    ├─ per hypothesis: design_test ─> sql_runner ─> evaluate
│                  │                    ├─ reconcile           │              │
│                  │                    ├─ synthesize          │              │
│                  │                    ├─ visualize           │              │
│  <─ event: done ─┤<── answer ─────────┤                      │              │
├─ GET /runs/{id} ─>│                    │                      │              │
├─ GET /trace ─────>│ full reconstruction from run_steps + tool_calls + sql_audit
```

Two interruptions can occur at any point and are handled identically: an approval request and a
clarification request both persist their payload, set the run status, and stop advancing the graph.
The checkpoint is already written, so the process may exit. When the decision or answer arrives via
the API, the graph resumes from that checkpoint by `thread_id`.

---

## 3. Data model

### 3.1 Analytical data — schema `analytics`, owned by `app_rw`, readable by `analyst_ro`

The Olist Brazilian E-commerce dataset: `orders`, `order_items`, `customers`, `products`,
`sellers`, `payments`, `reviews`, `geolocation`, plus a generated `dim_date` helper for clean
period-over-period joins. Columns holding personal data are enumerated in the column policy rather
than dropped at load time, so the policy itself is reviewable and testable.

### 3.2 Agent state — schema `agent`, owned and used by `app_rw` only

| Table | Purpose | Key columns |
|---|---|---|
| `runs` | One row per question | `run_id`, `thread_id`, `question`, `status`, `requested_by`, timings, token and cost totals |
| `run_steps` | One row per node execution | `run_id`, `step_id`, `node`, `status`, `started_at`, `duration_ms`, `error` |
| `tool_calls` | One row per tool invocation | `run_id`, `step_id`, `tool`, `arguments`, `result_summary`, `error`, `duration_ms` |
| `sql_audit` | One row per query considered — allowed, rejected or escalated | `query_id`, `run_id`, `sql`, `rewritten_sql`, `purpose`, `verdict`, `reasons`, `row_count`, `truncated`, `duration_ms` |
| `approvals` | One row per approval request | `approval_id`, `run_id`, `kind`, `payload`, `status`, `requested_at`, `decided_at`, `decided_by`, `reason` |
| `findings` | Findings and their hypotheses | `finding_id`, `run_id`, `statement`, `material`, `evidence_query_ids`, and per-hypothesis status and reasoning |
| LangGraph checkpoint tables | Durable graph state | managed by `PostgresSaver` |

`sql_audit` records **rejected** queries too. A run in which the guard blocked three attempts is
more informative than one in which those attempts vanished.

---

## 4. Deployment topology

```
docker compose
├── db     postgres:17     volume pgdata
│          init: 01_roles.sql, 02_schema.sql, 03_grants.sql (run once, in order)
│          healthcheck: pg_isready
├── seed   one-shot        loads the Olist dataset into analytics, then exits 0
│          depends_on: db healthy
├── api    uvicorn         analyst_agent.api.main:app, port 8000
│          depends_on: db healthy, seed completed
│          healthcheck: GET /readyz
└── ui     streamlit       port 8501, depends_on: api healthy
```

The image is multi-stage and runs as a non-root user. Nothing depends on a developer's machine
beyond Docker itself: a clean clone plus `docker compose up` yields the whole stack, which is the
acceptance test for Step 13.

---

## 5. Technology decisions

| Decision | Chosen | Alternative considered | Why |
|---|---|---|---|
| Orchestration | LangGraph | A custom plan-act-observe loop | Policy can be enforced by graph edges; durable checkpointing and `interrupt()` come for free, which is exactly what persistent task state and delayed human approval require |
| LLM binding | `anthropic` SDK directly inside nodes | LangChain `ChatAnthropic` | Direct control of prompt caching, adaptive thinking, per-node effort, structured outputs and refusal handling — all of which the abstraction hides |
| SQL validation | `sqlglot` AST inspection | Regex or keyword denylist | A denylist is bypassable by comments, casing, unicode and nesting. Parsing is not. |
| Read-only enforcement | A dedicated Postgres role | Validation only | Defence in depth: even a total validator bypass cannot write |
| Follow-up analysis | An enumerated pandas operation set | Executing model-written Python in a sandbox | Removes a whole class of escape risk; the cost is expressiveness, and it is documented as a limitation |
| Charts | Plotly plus kaleido | matplotlib | Interactive in Streamlit, static PNG for the report, one library for both |
| Metrics layer | YAML definitions in the repo | A prompt listing the formulas | Versioned, reviewable, testable, and diffable; a prompt is none of those |
| Dataset | Olist public e-commerce data | Synthetic generated data | Real messiness (cancellations, delivery delays, category mix shifts) gives the diagnostic questions genuine confounds to disentangle |
| Interface | Streamlit | React SPA | The deliverable is analytical behaviour and traceability, not front-end engineering; Streamlit shows the trace and approvals with far less code |
