# AI Data Analyst & Business Intelligence Agent

An agent that takes a business question, writes **safe** SQL, tests more than one explanation for
what it finds, and returns a conclusion you can trace back to the exact queries behind it.

```powershell
Copy-Item .env.example .env      # fill in ANTHROPIC_API_KEY and the two passwords
docker compose up                # database, dataset, API, interface

# UI   -> http://localhost:8000/app/
# API  -> http://localhost:8000/docs
```

A clean clone plus `docker compose up` is the whole setup. Nothing depends on a developer's
machine beyond Docker.

Every published port is parameterised, so a port already taken by something else on the machine
does not mean editing the compose file:

```powershell
$env:API_PORT = "8010"; docker compose up      # UI at /app/ on the same port
```

---

## Why this is not a chat box over a database

Five rules, and none of them live in the prompt.

**1 · Read-only by construction, and every query checked before it runs.**
Statements are parsed into an AST with `sqlglot` — not matched with regex, which is bypassable by
comments, casing, unicode and nesting. Exactly one statement is allowed and its root must be a
`SELECT`. The check that mattered most came from probing the parser first:

```sql
WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x
```

parses with a **`Select` at the root**, so a root-type check — the obvious implementation — would
clear a deletion. Denied node types are matched anywhere in the tree. Beneath all of it the
queries run as a Postgres role that physically cannot write, so even a total validator bypass
cannot change data.

**2 · A material finding cannot become an answer untested.**
While a finding is marked material and has fewer than two hypotheses in a terminal state, the
graph edge to synthesis is *unavailable*. There is nothing for a model to talk its way past. Each
hypothesis needs its own falsifying test, and two hypotheses whose tests are the same SQL are
recorded as one — no result could separate them.

**3 · An approved metric is computed, not reconstructed.**
Ask for revenue and the agent does not write a `sum(...)` of its own. It names the metric and the
declared dimensions it wants; the registry — twelve reviewed YAML definitions — assembles the
statement and binds every value as a parameter. An undeclared dimension is refused rather than
interpolated, so for anything the registry covers **no free text from the model reaches SQL**, and
the answer cites a definition version rather than a formula the model remembered.

**4 · Confidence is a number you can decompose.**
Every answer carries a score out of 100 *and the factors that produced it* — supporting queries,
explanations tested, whether an alternative was actually refuted, what the data allowed, what is
still open. A percentage on its own is a claim; a percentage beside the five things it was computed
from is something you can disagree with. The agent's own stated band is a **ceiling, never a
floor**: a run it called `low` cannot come out at 90 for having run four queries.

**5 · Every number leads back to a query.**
A finding with no evidence is refused by a database constraint, not by a convention. The answer
cites `query_id`s, the trace holds the statement, and the interface puts them one click apart —
including the queries the guard **refused**, because what the agent tried is usually what a
reviewer wants to know.

---

## The answer is a report, not a chat reply

Eight sections, in the order a reader needs them:

| | |
|---|---|
| **1 · Executive summary** | the conclusion in the first line of the page, with its confidence |
| **2 · Key findings** | cards: title, measured impact, severity. A finding without a figure the agent measured is left out |
| **3 · Investigation process** | metrics checked, tables analysed, questions tested, steps taken — **read off the audit trail**, not described by the model |
| **4 · Hypothesis testing** | every competing explanation with its verdict and the evidence that settled it |
| **5 · Visual analytics** | each chart with the purpose of the query behind it |
| **6 · Evidence & traceability** | analysis id, queries executed, metrics used, data sources — and the SQL **and its rows** one click away, including the queries the guard refused |
| **7 · Confidence** | a score out of 100 with the five factors that produced it |
| **8 · Recommended actions** | prioritised, each naming the finding it follows from |

Two rules hold the page honest. The sections that are *judgements* — headlines, severities,
recommendations — come from the model's structured output and are filtered against the queries that
actually ran, so a card can never carry a number from a query that did not execute. The sections
that are *record* — the investigation process, the evidence, the confidence factors — are derived
from the trace, because asking a model to describe its own process produces a plausible paragraph
rather than the truth.

Where the analysis produced no headlines, the page falls back to the findings the investigation
raised and **says so**, rather than inventing a card.

---

## Organisations, teams and data sources

| | |
|---|---|
| **Multi-tenant** | every run, report, data source, alert and audit entry belongs to an organisation, and every tenant-scoped query filters by it **in SQL**. Another organisation's resource answers 404, never 403 — a 403 confirms it exists |
| **Teams** | invite, promote, remove; four roles on a ladder (`viewer < analyst < admin < owner`). The last owner cannot be removed or demoted, and owner is not invitable |
| **Identity** | `Authorization: Bearer <key>`. Keys are stored as hashes and shown once. `REQUIRE_AUTHENTICATION=true` removes the anonymous path entirely |
| **Data sources** | PostgreSQL, CSV, Excel. Configuration is encrypted with a key held outside the database, and the API returns an allowlisted redaction — a withheld field is *named*, not starred out |
| **Sharing** | private / team / public, with expiry and revocation. A team link still requires membership; unknown, expired and revoked links are the same 404 |
| **Alerts** | drop, spike, below, above — over an **approved metric**, never free SQL. Every evaluation is recorded, fired or not |
| **Audit** | append-only: who did what, in which organisation. No update or delete path exists |

Read [docs/security-model.md](docs/security-model.md) for what this does and, more usefully, what
it does not. [docs/deployment.md](docs/deployment.md) has the five things that are not optional
before serving more than one company.

---

## Dashboards, reports and exports

| | |
|---|---|
| **Dashboard** | analyses run, saved reports, success rate over *finished* runs, what is still open, most-used metric definitions, and the most recent findings |
| **Saved reports** | save a finished analysis, rename it, delete it, read it back. A report is a **snapshot**, not a pointer: it keeps the question, the answer, the confidence, the charts, the SQL behind every cited number and the metric definition versions used, frozen as they read when you saved it |
| **Exports** | PDF to read, Excel to work with (one sheet per kind of thing, SQL in a column), PNG for a chart on its own. All three carry the findings and the evidence section |

A report does not change when the system behind it does. That is the whole reason it is a copy —
somebody re-opening a report from March must not find different figures under the same name
because a definition was revised in between.

---

## Stack

FastAPI · LangGraph · Anthropic Claude (`claude-opus-5`) · PostgreSQL 17 · sqlglot · pandas ·
Plotly, plus a YAML metrics layer holding approved KPI definitions. The interface is
hand-written HTML/CSS/JS served by the API itself at `/app/` — same origin, no build step, no
third-party request.

LangGraph supplies the state graph, the Postgres checkpointer and human-approval pauses. The LLM
calls go through one thin wrapper on the official `anthropic` SDK rather than a LangChain
abstraction, so prompt caching, adaptive thinking, per-node effort tiers and structured outputs
stay under direct control.

---

## Local development

Postgres still runs in a container; everything else is local.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

docker compose up -d db          # Postgres, with roles and schema applied on first start
python scripts/migrate.py        # agent state tables
python scripts/seed_db.py        # dataset (local CSVs -> Kaggle -> synthetic)
python scripts/smoke.py          # prove analyst_ro cannot write

make api                         # UI at /app/, API docs at /docs
```

| Command | What it does |
|---|---|
| `make guard` | the hostile-query suite — the security regression net |
| `make test` | everything, with coverage |
| `make evals-validate` | check the evaluation suite itself; needs no API key |
| `make evals` | run the suite and write a report |
| `make catalog` | regenerate `docs/metrics-catalog.md` from the YAML |
| `make smoke` | assert the read-only role really is read-only |

`ANTHROPIC_API_KEY` is needed only for the agent itself. Everything up to and including the tools,
the guard and the metrics layer is testable without it.

---

## The API

| | |
|---|---|
| `POST /v1/questions` | start an investigation; returns `202` with a `run_id` |
| `GET /v1/runs/{id}` | status, answer, findings, hypotheses, charts |
| `GET /v1/runs/{id}/trace` | every step, tool call and query — including refused ones |
| `GET /v1/runs/{id}/stream` | progress, server-sent events |
| `POST /v1/runs/{id}/approvals/{id}/approve` · `/reject` | decide a pending gate |
| `POST /v1/runs/{id}/answer` | reply to a clarification the agent asked for |
| `GET /v1/metrics` · `GET /v1/schema` | the approved definitions; what is queryable |
| `GET /v1/dashboard/summary` | totals, outcome rates, recent questions, most-used definitions, recent findings |
| `GET /v1/runs/{id}/queries/{qid}/rows` | the rows behind one query, rebuilt from the recorded statement |
| `GET /v1/me` · `POST /v1/organizations` | who the caller is; create an organisation |
| `GET /v1/team` · `POST /v1/team/invite` · `PATCH /v1/team/member/{id}` | the team, and managing it |
| `GET` · `POST` · `DELETE /v1/team/keys[/{id}]` | issue and revoke API keys |
| `GET` · `POST` · `DELETE /v1/data-sources[/{id}]` | data sources, credentials never returned |
| `POST /v1/reports/{id}/shares` · `GET /v1/shared/{token}` | share links, with expiry |
| `GET` · `POST` · `PATCH` · `DELETE /v1/alerts[/{id}]` · `POST /v1/alerts/{id}/check` | monitoring alerts |
| `GET /v1/audit` | the append-only audit trail |
| `POST /v1/reports` · `GET /v1/reports` | save a finished run as a report; list what is saved |
| `GET` · `PATCH` · `DELETE /v1/reports/{id}` | read, rename, delete |
| `GET /v1/reports/{id}/export.pdf` · `.xlsx` | the report as a file, evidence included |
| `GET /v1/charts/{id}/export.png` | one chart, as it was rendered when it was built |
| `GET /healthz` · `GET /readyz` | liveness; readiness including the read-only check |

`202` rather than `200` on a question is deliberate: an investigation takes minutes and can pause
for a human, so holding the connection open would both time out and make approval impossible.

---

## Repository layout

| Path | Contents |
|---|---|
| `src/analyst_agent/sql_guard/` | AST validation, column policy, EXPLAIN cost gate |
| `src/analyst_agent/agent/` | the graph, its nodes, state, budget, LLM wrapper |
| `src/analyst_agent/tools/` | metric lookup, metric query, schema inspector, SQL runner, analysis, charts |
| `src/analyst_agent/metrics/` | approved KPI registry and its YAML definitions |
| `src/analyst_agent/api/` · `ui/` | the service and the interface |
| `db/` | roles, schema, grants, migrations, the seed pipeline |
| `evals/` | 32 questions, three graders, the runner |
| `docs/` | design document, architecture, security controls, final report |

---

## Documentation

- [plan.md](plan.md) · [progress.md](progress.md) — the plan, and what each step actually did
- [docs/design-document.md](docs/design-document.md) — goal, tools, state, approval points,
  failure handling, security limits
- [docs/security-controls.md](docs/security-controls.md) — ten controls, each mapped to the
  threat it answers
- [docs/metrics-catalog.md](docs/metrics-catalog.md) — the approved definitions (generated)
- [docs/final-technical-report.md](docs/final-technical-report.md) — tradeoffs, evaluation
  results, limitations, what production would still need

## Evaluation

32 questions in six categories. **Seven of them must not be answered at all** — four need a
clarification, three a refusal. A suite of only answerable questions measures fluency rather than
judgement: it would score an agent that invents a churn figure as highly as one that says no
approved definition exists.

The safety grader produces a verdict rather than a score: **one violation fails the whole suite.**

```powershell
python -m evals.runner --validate    # the suite itself; no API key needed
python -m evals.runner --all
```

## Security

Ten controls, layered so no single failure is sufficient — read-only role, AST validation, object
allowlist, sensitive-column policy, resource limits, prompt-injection containment, budget caps,
four human-approval gates, secret handling, and a full audit trail. Each one, the threat it
answers, and what remains residual is in [docs/security-controls.md](docs/security-controls.md).

The seeded dataset contains a review comment that attempts a prompt injection, so that path is
exercised on the agent's real working path rather than in a contrived test.

## License

MIT
