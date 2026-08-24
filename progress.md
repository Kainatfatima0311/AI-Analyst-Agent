# Progress Log

Updated after **every** step, inside that step's own commit. Plan: [plan.md](plan.md).

Legend: ⬜ Pending · 🟨 In progress · ✅ Done · ⚠️ Done with known issue

## Status board

| Step | Title | Status | Date | Commit / Tag |
|---:|---|:---:|---|---|
| 0 | Repo skeleton, `plan.md`, `progress.md` | ✅ | 2026-08-24 | `chore(init)` |
| 1 | Design document | ✅ | 2026-08-24 | `docs(design)` / `v0.1-design` |
| 2 | Postgres in Docker, read-only role, seeded dataset | 🟨 | 2026-08-24 | — |
| 3 | Config, structured logging, run/trace/audit persistence | ⬜ | — | — |
| 4 | `sql_guard` — SQL safety layer | ⬜ | — | — |
| 5 | Metrics layer (approved KPI definitions) | ⬜ | — | — |
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
| SQL safety violations | 0 | — | — |
| Diagnostic questions with ≥2 tested hypotheses | 100% | — | — |
| Ambiguous questions correctly deferred to a human | ≥ 90% | — | — |
| Unit + integration test coverage | ≥ 80% | — | — |

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
