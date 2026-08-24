# AI Data Analyst & Business Intelligence Agent

An agent that takes a business question, identifies the data it needs, writes **safe** SQL,
inspects the results, **tests more than one plausible explanation**, runs follow-up Python
analysis, and presents a supported conclusion with charts or tables — where every claim is
traceable back to the exact queries and rows that produced it.

> 🚧 **Under construction.** Work follows [plan.md](plan.md) step by step; current state is
> recorded in [progress.md](progress.md).

## Why this is not a chat box over a database

Three rules are enforced by the architecture, not by prompt wording:

1. **Read-only database access, and every generated query is validated before execution.**
   Queries are parsed into an AST with `sqlglot` and checked against an allowlist; they then run
   through a Postgres role that physically cannot write, under a statement timeout and a row cap.
2. **Multi-hypothesis investigation.** For any material finding the agent must generate at least
   two competing explanations and test each with its own query before a conclusion is permitted.
   The graph's edge conditions block a short-circuit; the prompt alone is not trusted.
3. **Traceability.** Every conclusion carries the run id, the SQL that produced each number, the
   row counts, and the version of the approved metric definition used.

## Stack

FastAPI · LangGraph · Anthropic Claude (`claude-opus-5`) · PostgreSQL 17 · sqlglot · pandas ·
Plotly · Streamlit, with a YAML metrics layer defining approved business KPIs.

## Quick start

```powershell
# 1. configuration
Copy-Item .env.example .env      # then fill in ANTHROPIC_API_KEY and the passwords

# 2. everything, in containers
docker compose up

# API   -> http://localhost:8000/docs
# UI    -> http://localhost:8501
```

Local development without Docker (Postgres still runs in a container):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d db
python scripts/seed_db.py
pytest -v
```

## Repository layout

| Path | Contents |
|---|---|
| `src/analyst_agent/api/` | FastAPI application, routes, schemas |
| `src/analyst_agent/agent/` | LangGraph graph, state, nodes, prompts, LLM wrapper |
| `src/analyst_agent/tools/` | The agent's tools: metric lookup, schema inspector, SQL runner, Python analysis, charts |
| `src/analyst_agent/sql_guard/` | AST-based SQL validation, column policy, EXPLAIN cost gate |
| `src/analyst_agent/metrics/` | Approved KPI registry and YAML definitions |
| `src/analyst_agent/db/` | Engines, models, repositories |
| `src/analyst_agent/observability/` | Structured logging, traces, audit |
| `src/analyst_agent/ui/` | Streamlit analyst interface |
| `db/` | Database init scripts (roles, grants) and the dataset seed pipeline |
| `evals/` | Evaluation questions, graders, runner, reports |
| `docs/` | Design document, architecture, security controls, final technical report |
| `tests/` | Unit and integration tests |

## Documentation

- [plan.md](plan.md) — the step-by-step build plan
- [progress.md](progress.md) — what is done, what is measured
- [docs/design-document.md](docs/design-document.md) — agent goal, tools, state, approval points,
  failure handling, security limits
- [docs/security-controls.md](docs/security-controls.md) — each control and the threat it answers
- [docs/final-technical-report.md](docs/final-technical-report.md) — architecture, tradeoffs,
  evaluation results, limitations

## License

MIT
