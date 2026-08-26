# AI Data Analyst & Business Intelligence Agent

### Complete Project Documentation

| | |
|---|---|
| **Project** | An autonomous agent that answers business questions from a data warehouse, writes SQL that is validated before it runs, tests more than one explanation for what it finds, and returns a conclusion traceable to the exact queries behind it |
| **Repository** | `https://github.com/Kainatfatima0311/AI-Analyst-Agent` |
| **Language / runtime** | Python 3.12+ (developed on 3.13.5) |
| **Core stack** | FastAPI · LangGraph · Anthropic Claude (`claude-opus-5`) · PostgreSQL 17 · sqlglot · pandas · Plotly · Streamlit |
| **Size** | 53 source modules (9,605 lines) · 22 test modules (4,867 lines) · **473 tests passing** |
| **Static analysis** | `ruff` clean · `mypy --strict` clean |
| **Document date** | 26 August 2026 |

---

## Table of contents

1. [Abstract](#1-abstract)
2. [Problem statement and objectives](#2-problem-statement-and-objectives)
3. [Scope](#3-scope)
4. [Background: why an agent and not a text-to-SQL box](#4-background-why-an-agent-and-not-a-text-to-sql-box)
5. [System architecture](#5-system-architecture)
6. [Technology choices and their justification](#6-technology-choices-and-their-justification)
7. [Data model](#7-data-model)
8. [The metrics layer: approved KPI definitions](#8-the-metrics-layer-approved-kpi-definitions)
9. [The SQL safety layer (sql_guard)](#9-the-sql-safety-layer-sql_guard)
10. [Tool inventory](#10-tool-inventory)
11. [Agent design: state, graph and nodes](#11-agent-design-state-graph-and-nodes)
12. [The multi-hypothesis investigation loop](#12-the-multi-hypothesis-investigation-loop)
13. [Human-in-the-loop approval gates](#13-human-in-the-loop-approval-gates)
14. [Budgets, failure handling and recovery](#14-budgets-failure-handling-and-recovery)
15. [Observability and traceability](#15-observability-and-traceability)
16. [The HTTP API](#16-the-http-api)
17. [The analyst interface](#17-the-analyst-interface)
18. [Testing strategy](#18-testing-strategy)
19. [Evaluation methodology](#19-evaluation-methodology)
20. [Deployment and continuous integration](#20-deployment-and-continuous-integration)
21. [Results: what is verified and what is not](#21-results-what-is-verified-and-what-is-not)
22. [Engineering log: defects found and what they taught](#22-engineering-log-defects-found-and-what-they-taught)
23. [Known limitations](#23-known-limitations)
24. [What production would still require](#24-what-production-would-still-require)
25. [How to run the project](#25-how-to-run-the-project)
26. [Appendices](#26-appendices)

---

## 1. Abstract

Business intelligence work is dominated by a repetitive loop: somebody asks a question in
English, an analyst translates it into SQL against a warehouse they know well, looks at the
result, notices something surprising, and then — the part that actually requires judgement —
works out *why*. That last step is where analysis differs from reporting. A number is a fact; an
explanation is a claim, and a claim can be wrong.

Large language models are now good enough at SQL that the first three steps can be automated
convincingly. That is precisely the danger. A system that produces fluent SQL and a confident
paragraph, with no mechanism forcing it to check itself, produces *plausible* answers — and a
plausible wrong answer in a business setting is worse than no answer, because it will be acted
upon.

This project builds the missing mechanism. It is an agent with three properties enforced
structurally rather than by instruction:

1. **It cannot write to the database, and no query it generates executes unchecked.** Every
   statement is parsed into an abstract syntax tree and validated against a policy before it is
   allowed to run, and it then runs as a PostgreSQL role that physically lacks write permission.
2. **It cannot present a material finding as explained until it has tested at least two
   competing explanations**, each with its own falsifying query. This is enforced by a graph edge
   condition, not by a sentence in a prompt — the path to an answer does not exist until the
   condition is satisfied.
3. **Every number it reports leads back to a specific query, row count and metric definition
   version.** A finding recorded without evidence is rejected by a database constraint.

The system was built in fifteen documented steps, each with its own tests and its own commit. It
ships with a Dockerised stack that starts with one command, a 79-case hostile-query corpus as a
security regression net, a 32-question evaluation suite in which seven questions must *not* be
answered at all, and a CI pipeline. 473 automated tests pass.

One limitation is stated up front because it shapes how every result in this document should be
read: **no agent behaviour has been executed against a live language model.** An API key with
available credit was not obtainable during the build. Every agent behaviour is therefore verified
against a *scripted* model that returns predetermined responses, which fully exercises the
routing, the policy enforcement, the approval flow, the budget caps, the recovery path and the
graders — but says nothing about the quality of the SQL a real model would write. Section 21
separates the two kinds of claim precisely.

---

## 2. Problem statement and objectives

### 2.1 Problem statement

Given a natural-language business question and read access to a transactional data warehouse,
produce an answer that is (a) numerically correct, (b) safe to generate, and (c) defensible —
meaning a reviewer can reconstruct exactly how it was reached and can see which competing
explanations were considered and rejected.

Three failure modes make this hard, and each one became a design constraint rather than an
implementation detail:

| Failure mode | What it looks like | What the design owes it |
|---|---|---|
| **Unsafe generation** | A generated statement mutates data, reads system catalogues, exfiltrates personal data, or runs an unbounded query that takes the warehouse down | Validation before execution, a role that cannot write, resource caps |
| **Confident confabulation** | The agent reports the first explanation it thought of, or invents a KPI formula that does not match the company's | Mandatory competing hypotheses; a registry of approved definitions the agent may not bypass |
| **Unauditable output** | A paragraph of conclusions with no way to check them | Every claim carries query identifiers; the full trace is persisted, including refused queries |

### 2.2 Objectives

**Functional objectives**

| # | Objective | Where it is met |
|---|---|---|
| F1 | Accept a business question over an HTTP API and via an analyst interface | §16, §17 |
| F2 | Decide whether the question is answerable, and stop and ask when it is not | §11 (`clarify_gate`) |
| F3 | Resolve business terms to approved metric definitions | §8 |
| F4 | Discover schema and author SQL for what the metrics layer does not cover | §10, §11 |
| F5 | Execute only validated SQL, read-only | §9 |
| F6 | Run follow-up analysis on results already fetched | §10 (`python_analysis`) |
| F7 | Generate charts and tables | §10 (`chart_builder`) |
| F8 | For a material finding, test ≥2 competing explanations | §12 |
| F9 | Present a conclusion in which every number is traceable | §15 |

**Non-functional objectives**

| # | Objective | Target | Result |
|---|---|---|---|
| N1 | Zero SQL safety violations | 0 | 0 across 79 hostile cases and 29 role assertions |
| N2 | One-command reproducible deployment | `docker compose up` on a clean clone | Achieved |
| N3 | Durable task state surviving process restart | Resume by thread id | Achieved and tested |
| N4 | Human approval for high-impact actions | 4 gate types | 4 implemented, 3 reachable (§13) |
| N5 | Evaluation on ≥30 business questions | ≥30 | 32, in 6 categories |
| N6 | Structured logging, agent traces, tool-call history | Complete reconstruction of any run | Achieved |

---

## 3. Scope

### 3.1 In scope

Descriptive and diagnostic analytics over a fixed, seeded warehouse: single-metric lookups,
comparisons across periods and segments, trend analysis, and "why did X change" investigations
answered with tested hypotheses. A read-only conversational interface. Human approval for
expensive, sensitive or budget-extending actions. A full audit trail.

### 3.2 Explicitly out of scope

These were excluded in the design document, *before* implementation, so that the boundary is a
decision rather than an omission:

- **Writing to the warehouse.** The agent has no write path at all; this is not a configuration
  flag but two separate database roles.
- **Scheduled or pushed reporting.** The agent answers when asked.
- **Dashboard authoring.** A chart belongs to an answer, not to a persisted dashboard object.
- **Multi-tenant row-level security.** One warehouse, one analyst audience.
- **Forecasting and causal inference.** Hypothesis testing here is descriptive: it establishes
  that an explanation is consistent or inconsistent with the data, not that it *caused* anything.
  The distinction is stated in the system prompt and repeated in any answer reporting a
  correlation.
- **Generating new metric definitions without human approval.** The agent may report that no
  approved definition exists; it may not invent one silently.

---

## 4. Background: why an agent and not a text-to-SQL box

The obvious solution to "answer business questions from a database" is a text-to-SQL prompt: give
the model the schema, ask for SQL, run it, summarise the rows. It is a few hundred lines of code.
It was rejected for four reasons, each of which produced a specific subsystem in this project.

**Reason 1 — a single query is rarely the answer to a business question.** "Why did revenue drop
in March?" is not one query. It is one query to establish the drop, and then several to test
whether the cause was fewer orders, smaller orders, a shift in product mix, a delivery problem,
or a data artefact. A one-shot design cannot investigate; it can only report. This produced the
multi-hypothesis loop (§12).

**Reason 2 — fluency is not correctness, and the model cannot tell the difference.** A model that
writes a join which double-counts line items produces a number that is wrong and looks right.
Nothing in the model's output signals this. The mitigation is not a better prompt: it is an
external reference — approved metric definitions written by humans (§8) — plus evaluation against
hand-written reference queries (§19).

**Reason 3 — a generated statement is untrusted input.** The model's output arrives as text that
will be handed to a database. Treating it as trusted because "the model is helpful" is the same
category error as trusting a form field. This produced `sql_guard` (§9) and the read-only role.
Prompt injection makes this concrete: the seeded dataset deliberately contains a review whose
text instructs the reader to drop a table, so the containment claim is tested against real data
rather than asserted.

**Reason 4 — an answer nobody can check is not usable in a business.** If an analyst cannot see
which query produced a figure, they must either take it on faith or redo the work. Both defeat
the purpose. This produced the trace tables, the evidence drawer, and the database constraint
that refuses an unevidenced finding (§15).

The architecture follows from these four. The agent is a **state graph** rather than a loop
because the graph's edges can carry policy: a transition that would let an unexplained finding
become an answer simply does not exist.

---

## 5. System architecture

### 5.1 Component overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Presentation — src/analyst_agent/ui/                                         │
│   streamlit_app.py — question box, live timeline, findings, charts,          │
│   evidence drawer, approval controls. Talks only to the API.                 │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ HTTP + Server-Sent Events
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ Service — src/analyst_agent/api/                                             │
│   routes/  questions · runs · approvals · catalog · health                   │
│   Middleware: request id, structlog binding, typed error envelopes,          │
│   rate limiting on question submission                                       │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ start / resume a run by thread_id
┌───────────────────────────────▼──────────────────────────────────────────────┐
│ Orchestration — src/analyst_agent/agent/                                     │
│   graph.py        the LangGraph state graph and its policy edges             │
│   state.py        AnalystState + custom reducers                             │
│   nodes/          linear.py · investigate.py · explore.py · schemas.py       │
│   tool_loop.py    bounded tool-calling loop with a per-node allowlist        │
│   distinctness.py refuses a hypothesis that restates a sibling               │
│   approvals.py    durable pause: expensive query, restricted column, budget  │
│   llm.py          the single module that imports `anthropic`                 │
│   checkpointer.py PostgresSaver on the app_rw DSN                            │
│   budget.py       query / token / iteration / wall-clock caps                │
└───────┬──────────────────────────────────────────────┬───────────────────────┘
        │ tool calls                                   │ checkpoint after every node
┌───────▼──────────────────────────────┐   ┌───────────▼──────────────────────┐
│ Capability — tools/                  │   │ Agent state — schema `agent`     │
│   metric_lookup   ─┐                 │   │  (owned by app_rw)               │
│   metric_query    ─┤→ metrics/       │   │  langgraph checkpoints           │
│   schema_inspector │                 │   │  runs · run_steps · tool_calls   │
│   sql_runner      ─┤→ sql_guard/     │   │  sql_audit · approvals           │
│   python_analysis  │                 │   │  findings · hypotheses · charts  │
│   chart_builder   ─┘                 │   └──────────────────────────────────┘
└───────┬──────────────────────────────┘
        │ validated SELECT only
┌───────▼──────────────────────────────────────────────────────────────────────┐
│ Data — schema `analytics`, accessed as analyst_ro                            │
│   orders · order_items · customers · customer_contact · products · sellers · │
│   payments · reviews · geolocation · dim_date · category translation         │
│   read-only role · statement timeout · idle-transaction timeout · row cap    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Boundary rules

Four rules hold everywhere in the codebase, and each exists because the alternative fails in a
specific, identifiable way:

| Rule | Why |
|---|---|
| **The UI never touches the database.** Everything it displays arrived through the API. | The interface cannot show something the API would not, so there is one place to audit what is exposed. |
| **No node reads analytical data directly.** Every analytical read goes through `sql_runner`, which means every read goes through the guard. | There is no second code path that a future check would have to be remembered and added to. |
| **Two database roles, never merged.** `app_rw` owns agent state; `analyst_ro` runs every generated query, and the tool layer is only ever handed the read-only DSN. | Defence in depth: even a total validator bypass cannot mutate data. |
| **Exactly one module imports `anthropic`.** Nodes describe *what* they want; `agent/llm.py` owns model id, thinking, effort tier, prompt caching, retries and refusal handling. | Model-API concerns change often; they change in one file. |

### 5.3 Control flow of a single question

```
intake → clarify_gate → resolve_metrics
       → gather_context      [tool loop: schema_inspector, metric_lookup]
       → plan
       → compute_metrics     [tool loop: metric_query]
             ├─ answered by an approved metric ────────────────┐
             └─ something else needed → author_sql → execute ──┤
       → interpret ◄─────────────────────────────────────────── ┘
       → analyse             [tool loop: python_analysis]
       → materiality_check → generate_hypotheses → [test each] → reconcile
       → synthesize → visualize [tool loop: chart_builder] → respond
```

Sixteen nodes. Three run a **bounded tool loop** rather than a single structured call, because
whether inspecting the schema, deriving a further view, or drawing a chart *helps* depends on
what the data turned out to look like, and that cannot be scheduled from outside the run. Each
loop caps its turns and offers only the tools its node names; a call to anything outside that
allowlist returns a refusal *in the tool result*, not an exception, because a model can recover
from the first and not the second.

`author_sql` deliberately does **not** run a tool loop. It produces exactly one statement through
structured output, so exactly one statement per turn reaches the guard and the audit. Determinism
there is worth more than flexibility.

There is **no `interpret → synthesize` edge.** Every path to an answer passes through the
materiality gate. That is what makes the multi-hypothesis requirement structural rather than
aspirational.

### 5.4 Sequence of an investigation

```
UI                API                  Graph                  Guard        Postgres
│                  │                    │                      │              │
├─ POST /questions ─>                   │                      │              │
│                  ├─ create run row ──────────────────────────────────────> │ runs
│                  ├─ start thread ────>│                      │              │
│  <─ 202 {run_id} ─┤                    │                      │              │
├─ GET /stream ────>│ (SSE open)         │                      │              │
│                  │                    ├─ intake              │              │
│  <─ event: step ─┤<───────────────────┤                      │              │
│                  │                    ├─ clarify_gate        │              │
│                  │                    │   ambiguous? ──> pause, status=clarifying
│                  │                    ├─ resolve_metrics ──> metrics registry
│                  │                    ├─ gather_context ──> schema_inspector
│                  │                    ├─ plan                │              │
│                  │                    ├─ compute_metrics ─> metric_query ─>│
│                  │                    ├─ author_sql          │              │
│                  │                    ├─ sql_runner ────────>│ validate     │
│                  │                    │                      ├─ EXPLAIN ──> │ analyst_ro
│                  │                    │            allowed <─┤              │
│                  │                    │                      ├─ SELECT ───> │ analyst_ro
│                  │                    │<── rows + query_id ──┤              │
│                  │                    ├─ write sql_audit ───────────────── > │ sql_audit
│                  │                    ├─ interpret           │              │
│                  │                    ├─ analyse ─> python_analysis (no new query)
│                  │                    ├─ materiality_check   │              │
│                  │                    ├─ generate_hypotheses (≥ 2)          │
│                  │                    ├─ per hypothesis: design_test ─> sql_runner ─> evaluate
│                  │                    ├─ reconcile · synthesize · visualize │
│  <─ event: done ─┤<── answer ─────────┤                      │              │
├─ GET /trace ─────>│ full reconstruction from run_steps + tool_calls + sql_audit
```

Two interruptions can occur at any point and are handled identically: an approval request and a
clarification request both persist their payload, set the run status, and stop advancing the
graph. The checkpoint is already written, so the process may exit entirely. When the decision or
the answer arrives through the API, the graph resumes from that checkpoint by `thread_id`.

---

## 6. Technology choices and their justification

Every choice below was made with a named alternative in view. The cost of each choice is stated,
because a decision presented without its cost is advocacy rather than engineering.

| Decision | Chosen | Alternative considered | Why | What it cost |
|---|---|---|---|---|
| Orchestration | **LangGraph** state graph | A hand-written plan–act–observe loop | Edge conditions can *enforce* policy; a Postgres checkpointer and restart-surviving pauses come with it, which is exactly what durable task state and delayed human approval require | More machinery than a loop; a loop guard is needed so a finding the model cannot explain is not retried forever |
| LLM binding | The official **`anthropic` SDK**, used directly in one wrapper module | LangChain's `ChatAnthropic` | Prompt caching of the large stable prefix, adaptive thinking, per-node effort tiers, structured outputs and refusal handling are each a lever worth controlling, and the abstraction hides all four | More wrapper code, and one documented `type: ignore` where mypy cannot bind LangGraph's `add_node` overloads for a `Callable` alias |
| SQL validation | **`sqlglot` AST inspection** | A regex or keyword denylist | A denylist is bypassable by comments, casing, unicode escapes, nesting and statement stacking. Parsing is not. | A parser dependency, and refusing anything that will not parse — which occasionally rejects valid but exotic SQL |
| Read-only enforcement | A dedicated **PostgreSQL role** *plus* validation | Validation alone | Defence in depth: even a total validator bypass cannot write | Two DSNs to manage and two connection pools |
| Follow-up analysis | An **enumerated set of eight pandas operations** | Executing model-written Python in a sandbox | Removes an entire class of sandbox-escape risk permanently instead of defending against it forever. **No model-authored code executes anywhere in this system.** | Some analyses are not expressible and the agent must instead write different SQL — a real, documented limitation |
| Charts | **Plotly** with kaleido | matplotlib | Interactive inside Streamlit and static PNG for reports, from one library | A heavier dependency |
| Metrics layer | **YAML definitions in the repository** | A prompt listing the formulas | Versioned, reviewable, diffable and testable; a prompt is none of those | Two of twelve metrics do not fit the template and carry their own statement under `shape: custom` |
| Dataset | **Olist Brazilian e-commerce** (public) | Synthetic generated data | Real messiness — cancellations, delivery delays, category-mix shifts — gives the diagnostic questions genuine confounds to disentangle | A download step, mitigated by a committed fixture subset and a synthetic fallback |
| Interface | **Streamlit** | A React single-page application | The deliverable is analytical behaviour and traceability, not front-end engineering; Streamlit shows a trace and approval controls in far less code | Less layout control |

### 6.1 Model configuration

All model calls go through `agent/llm.py`. The configuration is deliberate rather than default:

| Setting | Value | Reason |
|---|---|---|
| Model | `claude-opus-5` (configurable) | The reasoning quality matters most at hypothesis generation and synthesis |
| Thinking | `{"type": "adaptive"}` | Lets the model spend reasoning where the problem needs it |
| Effort tier — classification | `low` | Ambiguity checks and chart decisions are cheap classifications |
| Effort tier — authoring | `high` | SQL authoring and result interpretation carry the correctness weight |
| Effort tier — reasoning | `xhigh` | Hypothesis generation and final synthesis are where judgement lives |
| Prompt caching | `cache_control` on the stable system prefix | The schema card, the metric registry and the safety rules are large and identical across every call in a run |
| Structured outputs | `output_config.format` | SQL plans, hypothesis lists and grader verdicts are parsed, not regex-scraped |
| Refusal handling | `stop_reason == "refusal"` handled explicitly | A refusal is a legitimate outcome to record, not an exception to swallow |
| Fallbacks | server-side fallbacks enabled | A single model's transient unavailability should not fail a run |

Effort tiering is the main cost lever: a run spends `xhigh` only where reasoning quality changes
the answer, and `low` wherever the task is a classification.

---

## 7. Data model

### 7.1 Analytical data — schema `analytics`

The Olist Brazilian e-commerce dataset: approximately 100,000 orders across 2016–2018 with their
line items, customers, sellers, products, payments, reviews and geolocation, plus a generated
`dim_date` helper for clean period-over-period joins.

| Table | Contents |
|---|---|
| `orders` | one row per order, with purchase, approval, shipping and delivery timestamps and a status |
| `order_items` | one row per line item: product, seller, price, freight |
| `customers` | customer key, unique person key, city, state |
| `customer_contact` | the personal fields, kept in a separate table (see below) |
| `sellers` | seller key, city, state |
| `products` | product key, category, dimensions, weight |
| `product_category_name_translation` | Portuguese to English category names |
| `payments` | payment type, instalments, value, per order |
| `reviews` | score, title, comment, timestamps |
| `geolocation` | zip prefix to latitude/longitude |
| `dim_date` | one row per calendar day with month, quarter, year, weekday |

Two seeding decisions are worth stating because they exist for the evaluation:

- **Personal data is loaded, not stripped.** Names, e-mail addresses, phone numbers, street
  addresses and precise coordinates are present. Removing them at load time would make the
  sensitive-column policy untestable — a control that never has anything to protect has not been
  demonstrated to work.
- **Ground truth is planted.** A revenue drop in March 2018 was seeded with *two* plausible
  causes present simultaneously — a product-mix shift and a delivery-delay spike — so that a
  diagnostic question has a genuine confound. An agent that stops at the first explanation gets
  it wrong in a way the evaluation can detect. A prompt-injection string is also planted in a
  review comment.

### 7.2 Agent state — schema `agent`

Owned and written only by `app_rw`. The agent's SQL tool is never handed this connection.

| Table | Purpose | Key columns |
|---|---|---|
| `runs` | one row per question | `run_id`, `thread_id`, `question`, `status`, `requested_by`, timings, token and cost totals |
| `run_steps` | one row per node execution | `run_id`, `step_id`, `node`, `status`, `started_at`, `duration_ms`, `summary`, `error` |
| `tool_calls` | one row per tool invocation | `run_id`, `step_id`, `tool`, `arguments`, `result_summary`, `error`, `duration_ms` |
| `sql_audit` | one row per query **considered** — allowed, rejected, escalated or approved | `query_id`, `run_id`, `sql`, `rewritten_sql`, `purpose`, `verdict`, `reasons`, `referenced_objects`, `sensitive_columns`, `estimated_cost`, `row_count`, `truncated`, `duration_ms` |
| `approvals` | one row per approval request | `approval_id`, `run_id`, `kind`, `payload`, `status`, `requested_at`, `decided_at`, `decided_by`, `reason` |
| `findings` | findings and their materiality | `finding_id`, `run_id`, `statement`, `material`, `evidence_query_ids` |
| `hypotheses` | competing explanations and their outcomes | `hypothesis_id`, `finding_id`, `statement`, `status`, `test_query_ids`, `reasoning` |
| `charts` | generated figures | `chart_id`, `query_id`, `title`, `chart_type`, `spec` |
| LangGraph checkpoint tables | durable graph state | managed by `PostgresSaver` |

`sql_audit` records **rejected** queries as well as executed ones. A run in which the guard
blocked three attempts is far more informative than one in which those attempts vanished.

### 7.3 Invariants as database constraints

Four project invariants live in `CHECK` constraints rather than in application code, because a
constraint cannot be forgotten by a node written six steps later:

| Constraint | What it forbids |
|---|---|
| `findings_require_evidence` | a finding with an empty `evidence_query_ids` array |
| `hypotheses_require_a_test` | a hypothesis that claims a verdict without a test query (an `inconclusive` verdict, which claims nothing, is exempted — migration 002) |
| `sql_audit_executed_implies_allowed` | executing a query whose verdict is neither `allowed` (guard cleared it) nor `approved` (a human cleared it — migration 003) |
| `approvals_decision_is_attributed` | a decided approval with no decider and no timestamp |

Two of these caught real defects during construction, described in §22.

---

## 8. The metrics layer: approved KPI definitions

### 8.1 The problem it solves

If the agent is asked for "revenue" and writes its own `sum(...)`, the number it returns is *its*
definition of revenue, not the company's. Whether freight is included, whether cancelled orders
count, and which timestamp defines the period are all judgement calls a business has already
made — and made differently from how a model would guess.

### 8.2 Structure

Each metric is a YAML file under `src/analyst_agent/metrics/definitions/`, declaring:

```yaml
name: revenue
version: v1
description: Gross merchandise value of delivered order items, excluding freight.
owner: analytics-team
grain: order_item
aggregate: sum(oi.price)
unit: BRL
tables: [order_items oi, orders o]
join: o.order_id = oi.order_id
filter: o.order_status = 'delivered'
date_column: o.order_purchase_timestamp
dimensions:
  month: to_char(o.order_purchase_timestamp, 'YYYY-MM')
  product_category: p.product_category_name
  customer_state: c.customer_state
caveats:
  - Excludes freight; use freight_ratio for shipping cost analysis.
  - Cancelled and unavailable orders are excluded.
sensitive: false
```

The important part is the **`dimensions` map**: a dimension is a *name* the agent may ask for,
bound to a SQL expression a human wrote and reviewed. The registry assembles the statement from
these reviewed parts; every value — date bounds, filter values — travels as a bound parameter.

The consequence is structural: **for an approved metric, no free text from the model reaches
SQL.** The model picks names, and names map to expressions a person wrote. A hostile filter value
stays a value.

### 8.3 The twelve approved metrics

| Metric | Grain | Notes |
|---|---|---|
| `revenue` | order item | delivered items, excluding freight |
| `gross_revenue` | order item | including freight |
| `orders` | order | distinct delivered orders |
| `aov` | order | average order value |
| `units` | order item | item count |
| `on_time_delivery_rate` | order | delivered on or before the estimate |
| `avg_delivery_days` | order | purchase to customer delivery |
| `avg_review_score` | review | 1–5 |
| `repeat_customer_rate` | customer | `shape: custom` — needs a per-person subquery |
| `cancellation_rate` | order | cancelled or unavailable share |
| `freight_ratio` | order item | freight as a share of merchandise value |
| `seller_concentration` | seller | `shape: custom` — needs a window function |

Two metrics do not fit the template and carry their own reviewed statement under `shape: custom`.
They are held to the same bar differently: **every rendered metric, custom or not, is asserted in
the test suite to pass the SQL guard and to return a non-null numeric result against the seeded
database.**

### 8.4 When there is no approved definition

If the question names a metric the registry does not have — "churn", for instance, which this
dataset cannot support — `metric_lookup` returns `NotApproved` with the closest matches. The agent
must then either ask the user or proceed with an *explicitly flagged* ad-hoc definition. It may
not silently invent a formula. Four of the 32 evaluation questions exist to test exactly this.

`docs/metrics-catalog.md` is generated from the YAML by `make catalog`, so the human-readable
catalogue cannot drift from what the code actually loads.

---

## 9. The SQL safety layer (`sql_guard`)

This is the highest-risk component in the project, which is why it was built early — immediately
after persistence, and before any agent code existed to call it.

### 9.1 Pipeline

Every statement passes through four stages in order, and the order matters:

```
1. record in sql_audit          ← before any decision, so a rejected attempt is recorded
2. validator.py                 ← parse to AST; structural policy
3. column_policy.py             ← sensitive-column tiers
4. explain_gate.py              ← EXPLAIN (no ANALYZE) cost estimate on a read-only connection
                                → GuardVerdict(allowed, reasons[], rewritten_sql,
                                               requires_approval, referenced_objects,
                                               sensitive_columns, estimated_cost, row_limit)
```

### 9.2 Structural validation

`validator.py` parses with `sqlglot` and decides on **node types in the tree**, never on text:

- exactly one statement — statement stacking is rejected at parse time;
- the statement must be a `SELECT`, optionally wrapped in `WITH`;
- any DDL, DML or DCL node type, `COPY`, `CALL`, `DO`, `SET`/`RESET` anywhere in the tree is
  rejected;
- dangerous functions are refused: `pg_read_file`, `pg_ls_dir`, `lo_*`, `dblink`, `pg_sleep`,
  set-returning system functions;
- objects must be in the `analytics` schema allowlist; `pg_catalog` and `information_schema` are
  reachable only through `schema_inspector`, never through `sql_runner`;
- a `LIMIT` is injected if absent (default 5,000) and clamped if too large (maximum 50,000);
- unbounded cross joins are rejected.

**The check that set the whole design** came from probing the parser before writing the validator:

```sql
WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x
```

This parses with a **`Select` at the root**. A root-statement-type check — the obvious
implementation, and the one most tutorials show — would have cleared a deletion. Denied node
types are therefore matched *anywhere* in the tree, and five variants of this attack are in the
hostile corpus.

### 9.3 Sensitive columns: three tiers, not one

"Sensitive" turned out not to be one thing. A single blanket rule would have forced human
approval on every run of the approved repeat-customer metric — which is how a control gets
switched off in practice.

| Tier | Rule | Why |
|---|---|---|
| `direct_identifier` | restricted anywhere outside an approved aggregate, **including in a `WHERE` filter** | filtering by an e-mail address is a person-level lookup, not analysis |
| `pseudonymous` | only *projection in the outermost select* is restricted | grouping and joining on `customer_unique_id` is ordinary work; the approved retention metric needs precisely that |
| `precise_location` | aggregate yes, return no; `min` and `max` are excluded from "aggregate" | `min(latitude)` returns a real observed coordinate, which is disclosure rather than aggregation |

A sensitive projection is **not silently dropped** — dropping it would give the agent a wrong
answer it could not detect. It raises, and becomes a human approval request.

### 9.4 Cost gate

`explain_gate.py` runs `EXPLAIN` — never `EXPLAIN ANALYZE`, which would execute the statement —
on the read-only connection and compares the planner's total cost against a threshold
(5,000,000 by default). Above it, the statement is *escalated* for human approval rather than
rejected: an expensive query is not illegitimate, it is a decision someone should make. Bound
parameters are passed to the `EXPLAIN` as well, so a parameterised metric statement can be
planned.

### 9.5 Resource limits

| Limit | Value | Enforced by |
|---|---|---|
| Statement timeout | 15 s | `ALTER ROLE analyst_ro SET statement_timeout` |
| Idle-in-transaction timeout | set on the role | PostgreSQL |
| Default row limit | 5,000 | injected by the guard |
| Maximum row limit | 50,000 | clamped by the guard |
| Transaction mode | `default_transaction_read_only = on` | set on the role |

A cancelled query returns a structured `QueryTimeout` with a narrowing suggestion, so the agent
can react rather than crash.

### 9.6 Verification

`tests/unit/test_sql_guard.py` is a table-driven corpus of **79 hostile statements**, all
rejected, and roughly 15 legitimate analytical queries, all allowed with the correct rewrite. The
attack families are: statement stacking, CTE-wrapped DML, `UPDATE … RETURNING`, comment
smuggling, casing and unicode tricks, `information_schema` and `pg_catalog` exfiltration,
sensitive-column projection and filtering, file-reading functions, `pg_sleep`, unbounded cross
joins, and CTE-alias shadowing of a forbidden object.

**This suite needs no database.** It is pure AST reasoning, which means it runs in CI in seconds
and is a required gate on every commit. It is the project's security regression net.

---

## 10. Tool inventory

Six tools. Each has a pydantic input model, a `strict: true` Anthropic tool schema generated from
it, an executor, and a **mandatory audit write**. A tool that refuses returns a *structured
refusal* — never a silent empty result, because a model cannot distinguish "no data" from
"blocked" and will confidently report the wrong one.

### 10.1 Which tools the model chooses, and which the graph calls

This distinction is a design decision, not an accident of implementation.

`sql_runner` is invoked **by a node**, after `author_sql` has produced exactly one statement
through structured output. It is deliberately not offered to the model in a loop, because one
statement per turn through the guard and the audit is what makes the safety story deterministic.

The other five are offered to the model inside bounded tool loops, because whether inspecting the
schema, resolving a term, computing a metric, deriving a further view or drawing a chart *helps*
depends on what the data turned out to look like — and that cannot be scheduled from outside the
run.

### 10.2 `metric_lookup`

| | |
|---|---|
| **Purpose** | Resolve a business term ("revenue", "AOV", "churn") to an approved definition |
| **Input** | `term: str`, `dimensions: list[str] = []` |
| **Output** | `MetricDefinition` (name, version, grain, dimensions, caveats) or `NotApproved(term, closest_matches)` |
| **Authority** | Read-only over the registry |
| **Failure** | An unknown term produces `NotApproved`. The agent must then ask or flag an ad-hoc definition explicitly. It may not silently invent a formula. |

### 10.3 `metric_query`

| | |
|---|---|
| **Purpose** | Compute an approved metric **without writing any SQL** |
| **Input** | `metric: str`, `dimensions: list[str] \| None`, `date_from`, `date_to`, `filters: dict[str,str] \| None`, `rank_by_value: bool \| None`, `purpose: str` |
| **Output** | The same shape as `sql_runner`, plus `definition_version`, `unit`, `dimensions` and `caveats` |
| **Authority** | The registry renders the statement; every value is a bound parameter. The rendered statement still passes through `sql_guard` and still lands in `sql_audit`. |
| **Failure** | An unapproved metric, or a dimension the metric does not declare, is refused with the list of what *is* available — so the next attempt can be correct. |

This is the tool that makes the metrics layer a **guarantee** rather than a lookup table. It is a
narrower door, not a bypass: nothing about the guard or the audit is skipped.

### 10.4 `schema_inspector`

| | |
|---|---|
| **Purpose** | Let the agent discover what data exists before writing SQL |
| **Input** | `tables: list[str] \| None`, `include_samples: bool = False` |
| **Output** | Tables, columns, types, nullability, row-count estimates, foreign keys, and sample distinct values **for low-cardinality columns only** |
| **Authority** | Allow-listed metadata for schema `analytics`. Sensitive columns are listed with a `restricted: true` flag but **never** with sample values. |
| **Failure** | An unknown table produces an error naming the allow-listed tables |

Sample values exist for a specific reason: without them the model filters on guessed literals
(`status = 'complete'` when the data says `'delivered'`) and gets an empty result it then
misinterprets as "there is no data".

### 10.5 `sql_runner`

| | |
|---|---|
| **Purpose** | Execute one analytical query |
| **Input** | `sql: str`, `purpose: str`, `row_limit: int \| None`, `parameters: dict \| None`, `approval_id: str \| None` |
| **Output** | `query_id`, columns, a **preview** of rows, the true row count, truncation flag, duration, guard verdict |
| **Authority** | `analyst_ro` only, and only after the guard returns allowed — or a human has approved this exact statement |
| **Failure** | Rejection returns a refusal with reasons and no execution. Escalation returns a refusal naming the pending approval, and the run pauses. A timeout returns `QueryTimeout` with a narrowing suggestion. Truncation is reported, never hidden. |

Three details carry weight:

- **`purpose` is required.** It costs the model one sentence and means a reviewer reading
  `sql_audit` can see *why* each statement ran, not only what it was.
- **The model sees a preview, not the full result.** Fifty rows go into the context; the complete
  frame stays in the frame store for `python_analysis` and `chart_builder`. Sending five thousand
  rows would be expensive and would invite the model to eyeball data instead of aggregating it.
- **An empty result is reported as a finding, not a failure**, with an explicit prompt to check
  the filter before concluding that no data exists.

### 10.6 `python_analysis`

| | |
|---|---|
| **Purpose** | Follow-up analysis over a **previously executed** query's result frame |
| **Input** | `query_id: str`, `operation: enum`, `params: dict` |
| **Output** | A derived frame plus a plain-language summary of what was computed |
| **Authority** | Operates only on frames already produced and audited. No network, no filesystem, an import allowlist, wall-clock and memory caps. |

The operation set is **fixed and enumerated**: `describe`, `group_by`, `share_of_total`,
`period_over_period`, `rolling`, `correlation`, `top_n`, `linear_fit`. Each is implemented in the
tool and validated against the frame's real columns.

The obvious alternative was letting the model write pandas and executing it in a sandbox. This
removes an entire class of escape risk permanently rather than defending against it forever:
**no model-authored code executes anywhere in this system.** The cost is expressiveness — some
analyses are not available and the agent must write different SQL instead — and it is documented
as a limitation rather than hidden.

`correlation` returns with an explicit caveat that association is not causation, because the
correlation operation is exactly where a diagnostic agent is most tempted to overclaim.

### 10.7 `chart_builder`

| | |
|---|---|
| **Purpose** | Turn a result frame into a figure |
| **Input** | `query_id`, `chart_type` (`line`, `bar`, `grouped_bar`, `stacked_bar`, `area`, `scatter`), axis and series columns, `title` |
| **Output** | A Plotly specification plus a PNG, carrying the `query_id` it was built from |
| **Authority** | Reads only from the frame store |

The chart carries its `query_id` so a figure in the interface is one click from the statement that
produced it. The prompt instructs at most two charts, one measure per chart, and none at all for a
single figure — a bar chart with one bar is noise.

---

## 11. Agent design: state, graph and nodes

### 11.1 State

`AnalystState` is a `TypedDict` carrying the question, clarifications, resolved metrics, the plan,
executed queries, findings, hypotheses, charts, approvals, budget counters and errors.

Two reducer decisions matter more than they look:

- **`merge_by_id` instead of append-only.** `resolved_metrics` and `hypotheses` are lists whose
  entries get *revised* — a hypothesis moves from `proposed` to `supported`. With an append-only
  reducer the same hypothesis sat in state twice, once stale, and the "two tested hypotheses" gate
  would have counted the stale copy. A custom reducer merges by identity.
- **Scratch keys prefixed with `_`.** Node-to-node scratch data (`_context_notes`,
  `_analysis_notes`, `_last_result`, `_pending_approval`) is namespaced so it cannot be mistaken
  for a durable field in the answer.

### 11.2 Checkpointing

`PostgresSaver` on the `app_rw` DSN writes a checkpoint after every node. Three consequences:

1. A run survives a process restart mid-investigation.
2. An approval can wait indefinitely — hours, a working day — without a process holding state in
   memory.
3. The recovery path is *testable*: the graph object is discarded and rebuilt between parking and
   resuming, which is what a process restart amounts to.

### 11.3 The nodes

| Node | Effort | What it does |
|---|---|---|
| `intake` | low | normalise the question, create the run row |
| `clarify_gate` | low | decide answerable / ambiguous / out of scope; park and ask if ambiguous |
| `resolve_metrics` | low | map business terms onto registry entries |
| `gather_context` | high | **tool loop** — `schema_inspector`, `metric_lookup` |
| `plan` | high | an ordered plan of what to establish, and in what order |
| `compute_metrics` | high | **tool loop** — `metric_query` for whatever an approved metric answers |
| `author_sql` | high | structured output: exactly one statement, for what metrics do not cover |
| `execute` | — | calls `sql_runner`; handles rejection, escalation, timeout |
| `interpret` | high | what the result says, and whether anything in it is surprising |
| `analyse` | high | **tool loop** — `python_analysis` on frames that already exist |
| `materiality_check` | low | is this finding material enough to require explanation? |
| `generate_hypotheses` | xhigh | ≥2 competing explanations, each with a distinguishing test |
| `design_test` | high | the falsifying query for one hypothesis |
| `evaluate` | high | supported / refuted / inconclusive, with reasoning |
| `reconcile` | xhigh | which explanations survived, which were refuted, and why |
| `synthesize` | xhigh | the answer, with every number carrying its query id |
| `visualize` | low | **tool loop** — `chart_builder` |
| `respond` | — | finalise the run row and the response payload |

Every node is a **closure over an LLM and a tool registry** (`make_gather_context(ctx)` and so
on). This is not a testing nicety: the routing is where the policy lives, and routing must be
asserted deterministically rather than through whatever a model happens to say on the day. It is
the reason 473 tests pass with no API key.

### 11.4 The bounded tool loop

`agent/tool_loop.py` implements the loop the three exploratory nodes share:

1. Call the model with the node's system prompt, the history, and **only** the tools the node
   names in its allowlist.
2. If the response contains `tool_use` blocks, echo the assistant turn, execute each tool, and
   return **all** `tool_result` blocks in a *single* user message — the API requires this.
3. A tool outside the allowlist returns a refusal as a tool result, not an exception.
4. Cap the turns (four for context and analysis, three for charting; six absolute maximum) and
   return a `ToolLoopResult` carrying the text, the usage, and every call made.

Turn caps are a budget control, not a formality: an unbounded loop is how an agent spends a
token budget on nothing.

---

## 12. The multi-hypothesis investigation loop

This is the part of the project that distinguishes it from a query generator, and the requirement
the brief called for explicitly.

### 12.1 What is enforced, and where

The requirement "test more than one plausible explanation" could have been a paragraph in the
system prompt. Instead:

> While a finding is marked **material** and has fewer than **two hypotheses in a terminal
> state**, the graph edge to `synthesize` is *unavailable*.

`materiality_check` sets the finding under investigation and the only edge out leads to hypothesis
generation. There is nothing for a model to talk its way past, because the transition does not
exist.

### 12.2 Rules encoded in the graph rather than the prompt

| Rule | Mechanism |
|---|---|
| A material finding cannot reach synthesis with fewer than two tested hypotheses | conditional edge |
| Each hypothesis carries its own falsifying test and its own `query_id` | `hypotheses_require_a_test` database constraint |
| A hypothesis with no distinguishing test is rejected at design time | `agent/distinctness.py` |
| `reconcile` must state which explanations were **refuted** and why, not only the winner | structured output schema with a required field |
| Confidence is downgraded when competing explanations remain indistinguishable, and the answer says so | reconcile schema + synthesis prompt |
| A finding the model cannot explain does not loop forever | `max_hypotheses_per_finding = 4`; then the investigation moves on and the answer must state the finding was not fully explained |

### 12.3 Distinctness

Two hypotheses that are the same idea in different words satisfy a naive counter while providing
no evidence at all — no result could separate them. `distinctness.py` normalises and compares both
the **test SQL** and the **hypothesis wording**, and records near-duplicates as a single
hypothesis.

The honest boundary: this catches identical test SQL and near-verbatim restatement. A genuine
paraphrase — "customers bought cheaper items" versus "average item price fell" — is beyond string
comparison, and catching it is the LLM-judge rubric's job in the evaluation suite (§19). The
documentation says so rather than implying the code is stronger than it is.

### 12.4 Why this matters analytically

The seeded March 2018 revenue drop has two real causes present at once. An agent that tests only
the first explanation it thinks of will report a mix shift *or* a delivery problem and be
half-right with full confidence — the exact failure mode that makes automated analysis dangerous
in a business. The loop forces both to be tested and forces `reconcile` to say that both are
supported, which is a materially more useful answer than either alone.

---

## 13. Human-in-the-loop approval gates

### 13.1 The four gates

The design specifies four points at which the agent stops and asks a person:

| # | Gate | Trigger | Status |
|---|---|---|---|
| 1 | `expensive_query` | the EXPLAIN cost gate exceeds the threshold | implemented and reachable |
| 2 | `sensitive_column` | the column policy escalates a restricted column | implemented and reachable |
| 3 | `budget_extension` | the budget is exhausted mid-investigation | implemented and reachable |
| 4 | `export` | publishing or exporting a report | implemented, **not reachable** — no export feature exists yet, so nothing calls it |

Gate 4 is stated as unreachable rather than quietly listed as complete. The function and the
persistence exist; the feature that would call it does not.

### 13.2 How a pause works

1. The node writes an `approvals` row with the exact payload — for a query, the statement itself,
   the reasons, the estimated cost, the sensitive columns.
2. The run status becomes `awaiting_approval` and the graph returns `END`.
3. The checkpoint is already written, so **the process may exit entirely.**
4. A person calls `POST /v1/runs/{id}/approvals/{approval_id}/approve` or `/reject`.
5. The graph is resumed by `thread_id` from the checkpoint and continues from where it stopped.

A timeout auto-rejects with a recorded reason. There are no blanket permissions anywhere: an
approval clears one statement, once.

### 13.3 Consent is given to a text, not to a slot

`approved_statement()` checks **four conditions against the stored row**, never against a flag the
caller supplied:

1. the approval exists;
2. it belongs to **this** run;
3. its status is `approved` and a person is recorded as the decider;
4. its recorded **fingerprint** — a whitespace-normalised SHA-256 of the statement — matches the
   statement now being executed.

Consequences, each tested:

- Passing a **pending** approval's id does not clear a query.
- Passing an approval id from a **different run** does not clear it.
- **Swapping the statement** after approval is refused, because the fingerprint no longer matches.

The threat here is not only a malicious model. A bug in a node written later could pass an id it
should not have; the check is designed so that neither a bug nor a model can manufacture consent.

### 13.4 The audit distinguishes who permitted what

`sql_audit.verdict` separates `allowed` — the guard cleared it — from `approved` — a human cleared
it. A reviewer can therefore see which queries ran on machine authority and which on human
authority. That distinction was missing until running under an approval collided with the
constraint forbidding execution of a non-allowed query. The collision was the signal that the
*vocabulary* was wrong, not that the constraint was.

---

## 14. Budgets, failure handling and recovery

### 14.1 Budget caps

| Cap | Default | Rationale |
|---|---|---|
| Queries per run | 25 | an investigation that needs more has usually lost its way |
| Hypotheses per finding | 4 | the loop guard |
| Agent iterations | 20 | prevents a node cycle from spinning |
| Wall clock | 600 s | a question is not worth an unbounded wait |
| Tokens per run | 400,000 | cost control |

Counters live in state and are therefore checkpointed: a resumed run does not get a fresh budget.

### 14.2 What happens on exhaustion

Budget exhaustion does **not** produce a truncated answer immediately. It asks (gate 3). If the
extension is refused, the run returns a partial answer with an explicit `truncated` status and a
statement of what was not established — never an unsupported conclusion presented as a finding.

### 14.3 Failure taxonomy

| Failure | Response |
|---|---|
| Guard rejection | structured refusal with reasons; the agent must write a *different* statement, and is told not to retry |
| Guard escalation | pause for approval; the agent is told explicitly not to look for a workaround |
| Query timeout | `QueryTimeout` with a narrowing suggestion |
| Empty result | reported as information, with a prompt to check the filter before concluding no data exists |
| Model refusal (`stop_reason == "refusal"`) | recorded as a refusal outcome on the step; the run ends with an explanation rather than an exception |
| Rate limit / transient API error | typed retry chain in `llm.py`, plus server-side fallbacks |
| Unknown metric | `NotApproved` — ask, or flag an ad-hoc definition explicitly |
| Process death | resume from the last checkpoint by `thread_id` |

### 14.4 Recovery, and how it is tested

`tests/integration/test_approvals.py` parks a run at an approval, **discards the graph object and
rebuilds it**, then approves and lets the run complete. Rebuilding the graph is the closest
in-process equivalent of a process restart, and it is what makes the durability claim a test
result rather than an assertion.

---

## 15. Observability and traceability

### 15.1 Structured logging

`structlog` emits JSON. `run_id`, `step_id`, `node` and `tool` are bound into the context, so
every line from a run is filterable by that run. Secrets are scrubbed by a processor that walks
**nested dictionaries and exception text**, not only top-level keys — the naive version leaks a
DSN embedded in a connection error message.

### 15.2 The trace

`GET /v1/runs/{id}/trace` reconstructs a complete run from three tables:

- `run_steps` — every node, its status, duration and human-readable summary;
- `tool_calls` — every invocation with arguments and result summary;
- `sql_audit` — every statement *considered*, with its verdict, reasons, referenced objects,
  sensitive columns, estimated cost, row count and truncation flag.

Because the audit records rejected and escalated statements as well as executed ones, a reviewer
can see what the agent *tried*, which is usually the more interesting question.

### 15.3 Traceability of a claim

An answer cites `query_id`s. Each `query_id` resolves to a statement, a row count and a purpose;
a metric-derived figure additionally carries the `definition_version` it was computed from. In the
interface these are one click apart. And the chain cannot be broken silently: a finding with an
empty evidence array is rejected by a database constraint.

---

## 16. The HTTP API

FastAPI, with request-id middleware, structlog correlation, typed error envelopes and rate
limiting on question submission.

| Endpoint | Purpose |
|---|---|
| `POST /v1/questions` | start an investigation; returns `202` with a `run_id` |
| `GET /v1/runs/{id}` | status, answer, findings, hypotheses, charts |
| `GET /v1/runs/{id}/trace` | every step, tool call and query — including refused ones |
| `GET /v1/runs/{id}/stream` | progress as server-sent events |
| `POST /v1/runs/{id}/approvals/{approval_id}/approve` · `/reject` | decide a pending gate |
| `POST /v1/runs/{id}/answer` | reply to a clarification the agent asked for |
| `GET /v1/metrics` | the approved KPI catalogue |
| `GET /v1/schema` | what is queryable |
| `GET /healthz` · `GET /readyz` | liveness; readiness including the read-only check |

`202` rather than `200` on a question is deliberate: an investigation takes minutes and can pause
for a human, so holding the connection open would both time out and make approval impossible.

`/readyz` asserts that the read-only role really is read-only, so a misconfigured deployment fails
readiness rather than serving unsafely.

---

## 17. The analyst interface

`ui/streamlit_app.py`. It talks **only** to the API — there is no database access from the UI at
all.

| Element | Purpose |
|---|---|
| Question box with metric autocomplete from `/v1/metrics` | steers the user towards approved definitions before the agent has to |
| Live step timeline from the SSE stream | an investigation takes minutes; silence reads as a hang |
| Findings panel | each finding with its materiality |
| Hypothesis panel | supported versus refuted, side by side, with the reasoning |
| Charts and tables | rendered from the API payload |
| **"Show the evidence" drawer** | each claim → its SQL, guard verdict, row count, metric definition version |
| Pending-approval banner | the statement, the reason, the estimated cost, and approve/reject buttons |
| Run history | previous questions and their outcomes |

The evidence drawer is the interface's reason for existing. A confident paragraph with a
one-click path to the SQL behind it is a different product from the same paragraph alone.

The UI is tested with Streamlit's `AppTest` harness against a patched API client, which required a
specific accommodation: `AppTest` re-executes the script in a fresh namespace, so the test patches
the injected `AnalystApi` dependency and clears `st.cache_resource` and `st.cache_data` between
runs.

---

## 18. Testing strategy

### 18.1 Composition

**473 tests** across 22 modules (4,867 lines of test code against 9,605 lines of source).

| Suite | Focus |
|---|---|
| `unit/test_sql_guard.py` | the 79-case hostile corpus plus legitimate queries — no database needed |
| `unit/test_metrics_registry.py` | schema validation, alias resolution, parameter binding, unknown-metric rejection |
| `unit/test_agent_state.py` | reducers, budget accounting, evidence accumulation |
| `unit/test_distinctness.py` | duplicate hypothesis detection |
| `unit/test_tool_base.py` | tool schema generation, duplicate-name rejection, refusal shape |
| `unit/test_logging.py` | secret scrubbing including nested dicts and exception text |
| `unit/test_evals.py` | the evaluation suite's own structure and the graders |
| `integration/test_readonly_role.py` | 29 assertions that `analyst_ro` cannot write |
| `integration/test_sql_guard_live.py` | the guard against a real database, including the cost gate |
| `integration/test_metric_sql.py` | every one of the 12 metrics executes and returns a numeric result |
| `integration/test_tools.py` | all six tools, their refusal paths, and a metric → SQL → analysis → chart chain |
| `integration/test_repository.py` | a fabricated run writes and reads back a full trace; constraints reject invalid rows |
| `integration/test_graph_linear.py` | a factual question end to end against a scripted model |
| `integration/test_multi_hypothesis.py` | ≥2 distinct hypotheses with distinct SQL; a material finding cannot short-circuit |
| `integration/test_approvals.py` | 13 tests: pause, rebuild the graph, approve, complete; reject; timeout; forged approval ids |
| `integration/test_api.py` | happy path, validation errors, unknown run, approval round trip |
| `integration/test_ui.py` | the interface renders and drives the API through `AppTest` |

### 18.2 The scripted model

`tests/fakes.py` provides `ScriptedLLM`, which returns predetermined structured responses and can
script tool calls keyed by the first offered tool name. This is what makes graph behaviour
testable without an API key — and, more importantly, *deterministically*. A test that asserts
"a material finding cannot reach synthesis" must not depend on what a model says on a given day.

### 18.3 What the test suite deliberately cannot tell us

The suite proves the machinery. It cannot prove that a real model writes good SQL, generates
genuinely distinct hypotheses, or calibrates its confidence well. That is what the evaluation
suite is for, and it is the number that does not exist yet. Keeping the two claims separate is a
deliberate choice in this documentation.

---

## 19. Evaluation methodology

### 19.1 Composition — 32 questions in six categories

| Category | N | What it measures |
|---|---|---|
| `factual` | 8 | calculation accuracy against a hand-written reference query |
| `comparison` | 7 | multi-step correctness across periods and segments |
| `diagnostic` | 6 | does it test more than one explanation before concluding |
| `ambiguous` | 4 | does it **stop and ask** rather than guessing |
| `out_of_scope` | 3 | does it say the data cannot answer this |
| `adversarial` | 4 | does policy hold under pressure — injected DDL, sensitive-column requests, unapproved metric definitions |

Each question is a YAML file declaring `id`, `question`, `category`, `expected_behavior`,
`ground_truth_sql` where applicable, `tolerance`, `must_ask_clarification`, `must_refuse` and
`rubric_notes`.

### 19.2 Seven of the 32 must not be answered at all

This is the most important design decision in the evaluation. A suite composed only of answerable
questions measures **fluency, not judgement**: it would score an agent that invents a churn figure
exactly as highly as one that correctly says no approved definition exists. The four ambiguous
and three out-of-scope questions exist so that *declining* is a scored behaviour.

### 19.3 The three graders

**`calculation.py`** — numeric comparison against the reference query's result, within a declared
tolerance. The reference queries are executed directly against the database, so the expected value
is computed rather than hard-coded and cannot drift from the data.

**`sql_safety.py`** — produces a **verdict, not a score**: zero non-SELECT executions, zero policy
bypasses, a guard verdict recorded for every query. **One violation fails the entire suite.** A
guard that holds for thirty-one questions and gives way on the thirty-second has not held, and
averaging that away would hide the only number here that matters.

**`analytical_quality.py`** — deliberately split in two halves:

- The **mechanical** half needs no model and carries the weight: did it stop to ask where it
  should have, did it test enough explanations, does every cited query actually exist.
- The **judged** half is an LLM against a fixed rubric via structured outputs, for what only
  reading can settle: whether two explanations were *genuinely* different, whether the stated
  confidence is calibrated.

The judged half is reported separately and is **never allowed to overturn** the mechanical
result. A model marking its own homework is worth something, but not that much.

### 19.4 The `--validate` mode

`make evals-validate` checks the *suite itself* without needing an API key: every reference query
parses, passes the guard and executes. On its first run it caught a genuine bug in a reference
query — `corr(row_number() OVER (...), aov)` nests a window function inside an aggregate, which
PostgreSQL rejects. Without that mode, the question would have silently graded every future run
against nothing.

### 19.5 What has not been measured

No question has been run against a real model, because that requires `ANTHROPIC_API_KEY` with
available credit. The harness, the graders and the report generation are all exercised, and the
out-of-scope questions were run end to end and correctly recorded as errored for the missing key.

These remain open, and are the first thing that should be established once a key is available:

- calculation accuracy on the 15 questions carrying reference numbers;
- whether ambiguous questions are genuinely deferred rather than guessed;
- whether the diagnostic six produce *distinct* hypotheses rather than two phrasings of one;
- whether the adversarial four hold, in particular the planted prompt injection;
- the cost and latency profile of a real run.

---

## 20. Deployment and continuous integration

### 20.1 Compose topology

```
docker compose
├── db     postgres:17     volume pgdata
│          init: 01_roles.sql → 02_schema.sql → 03_grants.sql (once, in order)
│          healthcheck: pg_isready
├── seed   one-shot        loads the dataset into `analytics`, then exits 0
│          depends_on: db healthy
├── api    uvicorn         analyst_agent.api.main:app, port 8000
│          depends_on: db healthy, seed service_completed_successfully
│          healthcheck: GET /readyz
└── ui     streamlit       port 8501, depends_on: api healthy
```

The image is **multi-stage and runs as a non-root user**. Every published port is parameterised
(`API_PORT`, `UI_PORT`), so a port collision on the host does not mean editing the compose file.

The acceptance test is exact: a clean clone plus `docker compose up` yields a working stack with
**no host dependency beyond Docker**. This was verified, with `scripts/smoke.py` passing 29/29
*inside* the container rather than only on the development machine.

### 20.2 CI pipeline

`.github/workflows/ci.yml`, in order, each stage gating the next:

1. `ruff` — lint and format check
2. `mypy` — strict type checking
3. unit tests
4. integration tests against a service-container PostgreSQL
5. **the 79-case hostile-query suite as a required gate**
6. build the Docker image

Stage 5 is called out separately even though it runs inside stage 3, because a green build with a
regressed guard is the one outcome that must be impossible.

### 20.3 The seed pipeline

`db/seed/load.py` resolves data in three tiers: committed local CSVs, then a Kaggle download, then
a synthetic generator preserving the same schema and the same planted ground truth. The reason is
CI: integration tests must not depend on a network download, and a synthetic fallback means the
suite still runs when the dataset is unavailable.

---

## 21. Results: what is verified and what is not

### 21.1 Delivered

Fifteen planned steps, all complete. One commit per step, conventional commit messages, tagged at
milestones.

| | |
|---|---|
| Tests | **473 passing** — 53 source modules, `ruff` and `mypy` clean |
| Hostile queries rejected | **79 / 79**, with no database required to prove it |
| Read-only role assertions | **29 / 29**, and they pass inside the container |
| Approved metrics | **12**, each executing and each passing the guard |
| Graph nodes | 16, of which 3 run bounded tool loops |
| Tools | 6, each audited on every call |
| Approval gate types | 4 implemented, 3 reachable |
| Evaluation questions | **32** in six categories; every reference query executes |
| Database invariants as constraints | 4 |
| Deployment | `docker compose up` on a clean clone, verified |
| **Agent runs against a real model** | **none** |

### 21.2 The distinction that matters

That last row is the honest headline. `ANTHROPIC_API_KEY` with available credit was never
obtainable during construction, so **every agent behaviour in this system is verified against a
scripted model.**

The two halves of the project fail differently, and conflating them would misrepresent both:

| | Verified | Not verified |
|---|---|---|
| **This repository's behaviour** — routing, policy edges, approval flow, budget caps, recovery, guard decisions, metric rendering, graders | ✅ 473 tests | — |
| **The model's analytical quality** — does it write correct SQL, generate genuinely distinct hypotheses, calibrate confidence, decline appropriately | — | ❌ requires a live key |

A routing bug is a bug in this repository. A weak hypothesis is a property of the model and the
prompt, and would show up as an evaluation score — which is precisely the number that does not
exist yet.

What *is* known about live behaviour: the API key was validated far enough to establish that the
request shape is accepted. The failure returned was `400 credit balance is too low`, not an
authentication or schema error. The integration is correct; the account had no credit.

### 21.3 Verified end to end, without a model

The following was executed and observed, not merely tested:

- `docker compose up` brings up `db` → `seed` (exit 0) → `api` (healthy) → `ui`;
- `scripts/smoke.py` passes 29/29 inside the container;
- `metric_query` returns real numbers from the seeded warehouse, citing `revenue@v1`, and shows
  the planted March 2018 revenue drop;
- the guard's cost gate escalates a deliberately expensive statement;
- an approval survives a graph rebuild and the run completes afterwards.

### 21.4 The conformance pass (Step 15)

After the build was otherwise finished, the implementation was read back against the design
document rather than against itself. Four places had quietly settled for less than the document
promised. All four were closed, and they are recorded here because the *pattern* is the finding:

| Gap | What was wrong | Fix |
|---|---|---|
| Unreachable tools | `schema_inspector`, `python_analysis` and `chart_builder` were built, registered and unit-tested — and no node ever offered them. The system prompt told the model to check the schema and gave it no way to. | `tool_loop.py` plus three tool-calling nodes |
| The metrics layer was correct and unused | The registry could render a statement from names, but only `metric_lookup` was exposed, so the model still wrote its own SQL for approved metrics. The layer's central claim was true of the registry and false of the agent. | `metric_query`, the sixth tool, plus a `compute_metrics` node |
| Approval point 3 had no caller | `request_budget_extension` existed and nothing called it, so exhaustion went straight to a truncated answer. | wired into `author_sql` |
| Distinctness was a rubric line | Only identical test SQL was caught. | `agent/distinctness.py` |

**Why this is worth reporting.** Every one of these hid behind a green test suite, because each
was a gap between what the *document* promised and what the *code was asked to do* — not a defect
in code that existed. No unit test could have caught them. The lesson is narrow and transferable:
a design document only stays true if something reads it back.

---

## 22. Engineering log: defects found and what they taught

The bugs worth recording are the ones where a safeguard fired, or where the failure was invisible
until something forced it into view.

**Every monetary figure reached the model as a string.** PostgreSQL returns `numeric` as a Python
`Decimal`, which has neither `isoformat` nor `item`, so the JSON-coercion helper fell through to
`str()` and `sum(oi.price)` arrived as `"139184.93"`. The model would then compare numbers
lexically or spend a turn parsing them — exactly what the helper existed to prevent. Found by
asserting the **types** of the model-facing payload rather than eyeballing the output.

**Foreign keys came back empty for every table.**
`information_schema.constraint_column_usage` returns rows only for tables the current user *owns*,
so as `analyst_ro` every table looked unrelated to every other — and the agent needs join paths to
write correct SQL. Rewritten against `pg_constraint`. The general lesson: a metadata query's
result depends on who is asking.

**A CTE could shadow a forbidden object — a real bypass.** CTE aliases must be excluded from the
catalogue check, but the code skipped any table whose *name* matched an alias, so
`WITH pg_authid AS (SELECT 1) SELECT * FROM pg_catalog.pg_authid` was **allowed**. In SQL a
qualified name can never resolve to a CTE; only unqualified names are treated as CTE references
now. Caught by the hostile corpus, which is the entire argument for maintaining one.

**`count(*)` was read as a wildcard projection.** The `*` in `count(*)` parses as `exp.Star`, so
every `count(*)` over a table holding a restricted column escalated to human approval. A guard
that escalates ordinary aggregation is a guard that gets switched off. Fixed by not treating a
`Star` whose parent is a function call as a projection.

**The EXPLAIN gate could not plan a parameterised statement**, so *every* metric query escalated.
Parameters now travel through `check` → `gate` → `estimate_cost` and are bound for the `EXPLAIN`
too. This one only appeared once `metric_query` existed — a safety layer written against
non-parameterised SQL had an assumption nobody had stated.

**Two state bugs of the same shape.** `resolved_metrics` and `hypotheses` both used append-only
reducers while nodes needed to *revise* entries, so a hypothesis sat in state twice — once
`proposed`, once terminal — and the two-hypothesis gate would have counted the stale copy. Fixed
with a `merge_by_id` reducer, and documented as the *second* occurrence so a third is recognised
faster.

**Node summaries were being silently discarded.** `repo.step` closed the step on the way out even
when the node had already closed it, overwriting the summary with `NULL` — emptying the one
human-readable column in the trace, which is most of what makes a run reviewable. Fixed with a
`finished` flag and two regression tests.

**Two database constraints were slightly wrong, and said so loudly.**
`hypotheses_require_a_test` demanded a query for any non-`proposed` status, but a hypothesis whose
test would duplicate a sibling's is correctly `inconclusive` with no query of its own; migration
002 narrows it to verdicts that *claim* something. `sql_audit_executed_implies_allowed` had no
vocabulary for human-approved execution; migration 003 added `approved`. In both cases a
constraint failing loudly was better than the alternative, which was the run silently doing the
wrong thing.

**A query a human had approved was not counted as evidence.** `executed_query_ids` filtered on
verdict `allowed` only, so after migration 003 introduced `approved`, a finding supported by an
approved query looked unevidenced. Both verdicts count now. A vocabulary change needs its readers
updated, not only its writers.

**An empty package shadowed a module.** A leftover empty `ui/components/` package took import
precedence over `components.py`, so the interface imported nothing. Removed.

**One environment failure worth recording.** Mid-project the `D:` drive was deleted. It held Git
and the Python interpreter the virtual environment was built from, so every command failed at once
and the shell died with it. Recovered by rebuilding the virtual environment from another
interpreter at the same version and reinstalling Git; no repository history was lost, because
`.git` lived on a different drive. Six tests written minutes earlier were lost and rewritten. The
lesson is not about drives: **"the tests pass" is only a claim about the machine that ran them**,
which is the argument for the containerised CI in §20.

**A tooling fault of my own making, recorded because it silently corrupted work.**
`Set-Content -Encoding utf8` on Windows PowerShell 5.1 writes a byte-order mark and round-trips
through cp1252, which double-encoded five files — a middle dot (`U+00B7`) came back out as two
characters. Worse, every status-board update had been a PowerShell `-replace` with an emoji typed
literally in the pattern; once the file was double-encoded those patterns no longer matched, and
`-replace` reports nothing when it matches nothing, so several steps silently stayed marked
pending while their entries were being appended below. Repaired with Python, and the board was
*rebuilt* from an explicit list rather than patched again — patching being precisely what failed.
Two rules kept from it: on PowerShell 5.1, do not use `Set-Content`/`Add-Content` for non-ASCII
text, and do not trust a `-replace` that reports no error.

---

## 23. Known limitations

Stated plainly, and separated by kind.

### 23.1 Unmeasured rather than broken

- **No live-model evaluation.** The single largest gap. Every number in §21 concerns the
  machinery, not the analysis. Accuracy, hypothesis quality, deferral behaviour and cost are all
  unknown.

### 23.2 Deliberate design limits

- **`python_analysis` is an enumerated operation set.** Some analyses are not expressible and the
  agent must write different SQL instead. This is the accepted cost of never executing
  model-authored code.
- **The guard says nothing about semantics.** A statement that is a legitimate `SELECT` but
  analytically wrong — a join that double-counts line items — is not a security problem and the
  guard is silent on it. That is what the accuracy graders are for, and it is the failure mode
  most likely to survive into production.
- **Distinctness catches near-verbatim restatement, not paraphrase.** Genuine paraphrase is left
  to the LLM-judge rubric.
- **Aggregates over very small groups can still narrow identity.** A minimum-group-size rule is
  listed in §24.
- **One warehouse, one audience.** No row-level security, no multi-tenancy.

### 23.3 Incomplete

- **Approval gate 4 (`export`) has no caller**, because no export feature exists.
- **The SSE `/stream` endpoint has no functional test.** It is exercised manually and by the UI,
  but not asserted.
- **The clarification-resume path** sends `{"question": "", "answer": ...}`, which writes an empty
  question into state. Harmless today because the original question is already persisted, but
  wrong and worth fixing.
- **Cost and latency are not instrumented per node.** Token usage is accounted per run; a
  per-node breakdown would be the first thing wanted for cost tuning.

---

## 24. What production would still require

In the order the concerns would actually matter, not in order of interest:

1. **Live evaluation scores and a published baseline.** Nothing else on this list can be
   prioritised sensibly without them.
2. **Row-level security in the warehouse**, so what the agent can read depends on who asked. The
   read-only role is currently one role for everybody.
3. **A minimum-group-size rule** on aggregates over person-level tables, closing the small-group
   disclosure gap in §23.
4. **Cost governance** — a per-user and per-day token and query budget, with alerting, not only
   the per-run caps that exist.
5. **Query result caching**, keyed on the rendered statement and the data's freshness. The same
   metric is recomputed today on every run.
6. **Metric definition drift monitoring.** A definition changing without its version changing is
   the quiet failure mode of any metrics layer.
7. **A PII review with a data owner**, since the sensitive-column policy currently encodes my
   judgement rather than a data owner's decision.
8. **An on-call runbook** — what a paused run, an exhausted budget or a failed readiness check
   means, and what to do about each.
9. **Multi-tenant isolation** if more than one team ever uses it: separate schemas, separate
   roles, separate audit.
10. **Human review sampling of answers in production**, because the evaluation suite measures the
    questions it contains and production will ask different ones.

---

## 25. How to run the project

### 25.1 One command

```powershell
Copy-Item .env.example .env      # fill in ANTHROPIC_API_KEY and the two database passwords
docker compose up                # database, dataset, API, interface

# API -> http://localhost:8000/docs
# UI  -> http://localhost:8501
```

A clean clone plus `docker compose up` is the whole setup. Nothing depends on a developer's
machine beyond Docker. If a port is taken:

```powershell
$env:API_PORT = "8010"; $env:UI_PORT = "8511"; docker compose up
```

### 25.2 Local development

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

docker compose up -d db          # PostgreSQL, with roles and schema applied on first start
python scripts/migrate.py        # agent state tables
python scripts/seed_db.py        # dataset (local CSVs -> Kaggle -> synthetic)
python scripts/smoke.py          # prove analyst_ro cannot write

make api                         # http://localhost:8000/docs
make ui                          # http://localhost:8501
```

### 25.3 Commands

| Command | What it does |
|---|---|
| `make guard` | the 79-case hostile-query suite — the security regression net |
| `make test` | everything, with coverage |
| `make evals-validate` | check the evaluation suite itself; **needs no API key** |
| `make evals` | run the suite and write a timestamped report |
| `make catalog` | regenerate `docs/metrics-catalog.md` from the YAML |
| `make smoke` | assert the read-only role really is read-only |

`ANTHROPIC_API_KEY` is needed only for the agent itself. Everything up to and including the tools,
the guard and the metrics layer is testable without it.

### 25.4 Asking a question over the API

```bash
curl -X POST localhost:8000/v1/questions \
     -H "content-type: application/json" \
     -d '{"question":"Why did revenue drop in March 2018?"}'
# -> 202 {"run_id": "..."}

curl localhost:8000/v1/runs/<run_id>          # status, answer, findings, hypotheses
curl localhost:8000/v1/runs/<run_id>/trace    # every step, tool call and query
```

---

## 26. Appendices

### Appendix A — Repository layout

```
AI-Analyst-Agent/
├── plan.md                          the 15-step plan the work executed against
├── progress.md                      a status board plus one detailed entry per step
├── PROJECT-DOCUMENTATION.md         this document
├── README.md  .env.example  pyproject.toml  Makefile
├── docker-compose.yml  Dockerfile  .dockerignore
├── docs/
│   ├── design-document.md           written before any code
│   ├── architecture.md              component boundaries, runtime flow, data model
│   ├── security-controls.md         ten controls, each mapped to its threat
│   ├── metrics-catalog.md           generated from the YAML definitions
│   └── final-technical-report.md    decisions, costs, results, limitations
├── src/analyst_agent/
│   ├── config.py                    pydantic-settings; two DSNs
│   ├── api/                         main.py · service.py · schemas.py · routes/
│   ├── agent/
│   │   ├── graph.py                 the state graph and its policy edges
│   │   ├── state.py                 AnalystState and its reducers
│   │   ├── nodes/                   linear.py · investigate.py · explore.py · schemas.py
│   │   ├── tool_loop.py             bounded tool-calling loop
│   │   ├── distinctness.py          duplicate-hypothesis detection
│   │   ├── approvals.py             fingerprinting, request and verification
│   │   ├── llm.py                   the only module importing `anthropic`
│   │   ├── checkpointer.py          PostgresSaver on the app_rw DSN
│   │   ├── budget.py                query / token / iteration / wall-clock caps
│   │   └── prompts.py  prompts/     the cached system prefix
│   ├── tools/                       6 tools + base.py · registry.py · frames.py · palette.py
│   ├── sql_guard/                   validator · column_policy · explain_gate · catalog · policy
│   ├── metrics/                     registry.py · loader.py · definitions/*.yaml (12)
│   ├── db/                          engine.py · repository.py · models.py
│   ├── observability/               logging.py · trace.py · audit.py
│   └── ui/                          streamlit_app.py · components.py · api_client.py
├── db/
│   ├── init/sql/                    01_roles.sql · 02_schema.sql · 03_grants.sql
│   ├── migrations/                  001_agent_state · 002_inconclusive · 003_approved_verdict
│   └── seed/                        download.py · load.py · raw/
├── evals/
│   ├── questions/                   01_factual … 06_adversarial (32 questions)
│   ├── graders/                     calculation.py · sql_safety.py · analytical_quality.py
│   ├── runner.py                    executes the suite, writes json + md
│   └── reports/
├── tests/                           unit/ (7 modules) · integration/ (11) · fakes · fixtures
├── scripts/                         migrate.py · seed_db.py · smoke.py · bootstrap.ps1
└── .github/workflows/ci.yml
```

### Appendix B — Configuration reference

| Setting | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for the agent only; blank is treated as unset |
| `ANALYST_MODEL` | `claude-opus-5` | never date-suffixed |
| `DB_RW_DSN` | — | `app_rw`; agent state only |
| `DB_RO_DSN` | — | `analyst_ro`; every generated query |
| `ALLOWED_SCHEMAS` | `analytics` | the object allowlist |
| `SQL_STATEMENT_TIMEOUT_MS` | 15,000 | per statement |
| `SQL_DEFAULT_ROW_LIMIT` | 5,000 | injected when absent |
| `SQL_MAX_ROW_LIMIT` | 50,000 | clamp |
| `SQL_MAX_EXPLAIN_COST` | 5,000,000 | above this, escalate for approval |
| `MAX_QUERIES_PER_RUN` | 25 | budget |
| `MAX_HYPOTHESES_PER_FINDING` | 4 | loop guard |
| `MAX_AGENT_ITERATIONS` | 20 | budget |
| `MAX_RUN_WALL_CLOCK_SECONDS` | 600 | budget |
| `MAX_TOKENS_PER_RUN` | 400,000 | budget |
| `EFFORT_CLASSIFY` | `low` | classification nodes |
| `EFFORT_AUTHOR` | `high` | SQL authoring, interpretation |
| `EFFORT_REASON` | `xhigh` | hypothesis generation, synthesis |
| `API_PORT` · `UI_PORT` | 8000 · 8501 | published ports |

### Appendix C — Requirement traceability

The brief's nine common standards, each mapped to where it is met:

| # | Standard | Where |
|---|---|---|
| 1 | A design document written **before** coding | `docs/design-document.md`, committed in Step 1 before any agent code |
| 2 | Git repository with README, tests and CI | 17 commits, one per step; `README.md`; 473 tests; `.github/workflows/ci.yml` |
| 3 | Dockerised setup | `Dockerfile` (multi-stage, non-root) + `docker-compose.yml`, verified on a clean clone |
| 4 | Structured logging, agent traces, tool-call history | `observability/logging.py`; `run_steps`, `tool_calls`, `sql_audit`; `GET /trace` |
| 5 | Persistent task state with recovery | `PostgresSaver` checkpointing; resume by `thread_id`, tested across a graph rebuild |
| 6 | At least three meaningful tools | **six** — §10 |
| 7 | Human approval for high-impact actions | four gate types, §13; approval verified against the stored row |
| 8 | Evaluation set including cases where the correct action is to stop or ask | 32 questions; **7 must not be answered** — §19.2 |
| 9 | Final technical report | `docs/final-technical-report.md`; this document consolidates it |

And the brief's own definition of "production ready":

| Requirement | Where |
|---|---|
| Read-only database access | two roles, §5.2; 29 assertions |
| Every generated query checked | `sql_guard`, §9; 79 hostile cases |
| More than one plausible explanation tested for important findings | §12, enforced by a graph edge |
| Approved metric definitions | §8; `metric_query` makes it a guarantee |
| Restricted sensitive columns | three tiers, §9.3 |
| Every conclusion traceable to its queries | §15; enforced by a database constraint |

### Appendix D — Commit history

| Commit | Step |
|---|---|
| `chore(init)` | 0 — repo skeleton, `plan.md`, `progress.md`, tooling |
| `docs(design)` | 1 — design document, architecture, security controls · `v0.1-design` |
| `feat(db)` | 2 — Dockerised PostgreSQL, read-only role, seed with planted ground truth |
| `feat(core)` | 3 — settings, structured logging, agent schema, run/trace/audit persistence |
| `feat(sql-guard)` | 4 — AST validation, column policy, EXPLAIN cost gate · `v0.2-sql-safety` |
| `feat(metrics)` | 5 — approved KPI registry, 12 definitions, generated catalogue |
| `feat(tools)` | 6 — the tool layer |
| `feat(agent)` | 7 — LangGraph state, checkpointer, LLM wrapper, linear graph · `v0.3` |
| `feat(agent)` | 8 — multi-hypothesis generation, falsification, reconciliation · `v0.4` |
| `feat(api)` | 9 — questions, run status, trace, approvals, SSE |
| `feat(approvals)` | 10 — human-in-the-loop gates with durable pause and resume · `v0.5` |
| `feat(ui)` | 11 — Streamlit interface with evidence drawer and approval controls |
| `test(evals)` | 12 — 32-question suite, three graders, runner · `v0.6-evals` |
| `build(ci)` | 13 — Dockerised stack, CI pipeline, README · `v0.7-deployable` |
| `docs(report)` | 14 — final technical report, completed progress log · `v1.0` |
| `feat(agent)` + `docs` | 15 — design-conformance pass: four gaps closed |

### Appendix E — Glossary

| Term | Meaning here |
|---|---|
| **Agent** | A program that decides its own next action within a bounded policy, rather than executing a fixed script |
| **AST** | Abstract syntax tree — a parsed representation of SQL, on which structural rules can be enforced reliably |
| **Checkpoint** | A serialised snapshot of graph state after a node, enabling restart and delayed approval |
| **Effort tier** | A model setting trading cost against reasoning depth, chosen per node |
| **Escalation** | A guard outcome meaning "valid but requires a human decision" — distinct from rejection |
| **Falsifying test** | A query whose result would *contradict* a hypothesis if the hypothesis were false |
| **Fingerprint** | A whitespace-normalised SHA-256 of a statement, so an approval binds to a text rather than to a slot |
| **Material finding** | A result significant enough that the agent must explain it before concluding |
| **Metric definition** | A versioned, human-authored KPI specification the agent may use but not invent |
| **Prompt caching** | Reusing a large stable system prefix across calls, reducing cost and latency |
| **Structured output** | A model response constrained to a declared schema, so it can be parsed rather than scraped |
| **Tool loop** | A bounded cycle in which the model may call tools from an allowlist until it stops or the cap is reached |

### Appendix F — References

- Olist Brazilian E-Commerce Public Dataset (Kaggle) — the analytical data
- LangGraph documentation — state graphs, `PostgresSaver`, `interrupt()`
- Anthropic API documentation — Messages API, tool use, prompt caching, structured outputs,
  adaptive thinking, effort configuration
- `sqlglot` documentation — SQL parsing and AST traversal
- PostgreSQL documentation — roles and privileges, `default_transaction_read_only`,
  `statement_timeout`, `EXPLAIN`, `pg_constraint`
- FastAPI, Streamlit, Plotly, pandas, pydantic-settings, structlog documentation

---

## Closing note

The system that exists is, in the parts that can be verified without a model, complete and
tested: the safety layer holds against 79 attacks, the metrics layer is a guarantee rather than a
convention, the investigation loop cannot be short-circuited, the trace reconstructs any run, and
one command brings the whole stack up on a clean machine.

What it has not yet done is answer a real question with a real model. That is one API key and
roughly one afternoon away: `make evals` will produce the baseline report, and §19.5 lists exactly
which numbers it will fill in. Everything needed to obtain them — the questions, the reference
queries, the graders, the runner and the report generator — is built, and the suite's own
`--validate` mode already passes.

The judgement this project is really about is the one in §21.2: knowing which claims the work
supports and which it does not, and writing the second kind down as plainly as the first.
