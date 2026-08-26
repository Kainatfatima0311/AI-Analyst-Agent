# Final Technical Report

**AI Data Analyst & Business Intelligence Agent** · 2026-08-26

An agent that takes a business question, writes SQL that is checked before it runs, tests more
than one explanation for what it finds, and returns a conclusion traceable to the queries behind
it.

This report covers what was built, the decisions that shaped it and what they cost, what has and
has not been verified, and what would still be required before this ran anywhere real. Step-level
detail is in [../progress.md](../progress.md); the design intent, written before implementation,
is in [design-document.md](design-document.md).

---

## 1. Where the project actually stands

Fourteen planned steps; thirteen complete, one built but unmeasured.

| | |
|---|---|
| Tests | **470 passing** — 51 source files, `ruff` and `mypy` clean |
| Hostile queries rejected | **79 / 79**, with no database required to prove it |
| Read-only role assertions | **29 / 29** |
| Approved metrics | 12, each executing and each passing the guard |
| Evaluation questions | 32, in six categories; every reference query executes |
| Agent runs against a real model | **none** |

That last row is the honest headline. `ANTHROPIC_API_KEY` was never available during
construction, so every agent behaviour in this system is verified against a **scripted model**.
The graph's routing, the approval flow, the budget caps, the recovery path and the graders are all
tested and all pass. Whether Claude writes good SQL for these twenty-five answerable questions is
unmeasured, and this report does not estimate it. §6 says exactly what that leaves open.

The distinction matters because the two halves fail differently. A routing bug is a bug in this
repository. A weak hypothesis is a property of the model and the prompt, and would show up as an
eval score — which is precisely the number that does not exist yet.

---

## 2. Architecture

```
Streamlit UI ──HTTP/SSE──> FastAPI ──> LangGraph agent ──> Postgres checkpointer (app_rw)
                              │             │
                              │             ├─ metric_lookup      approved KPI registry
                              │             ├─ schema_inspector    allow-listed metadata
                              │             ├─ sql_runner         ─┐
                              │             ├─ python_analysis     │ every statement is
                              │             └─ chart_builder       │ validated before it runs
                              │                                    │
                              └─ runs / run_steps / tool_calls /   │
                                 sql_audit / approvals / findings  ▼
                                                    analytics schema, analyst_ro role
                                                    (read-only, timeout, row cap)
```

Four boundaries hold everywhere, and each one exists because the alternative failed in a specific
way:

- **The UI never touches the database.** Everything it shows came through the API, so it cannot
  display something the API would not.
- **No node reads analytical data directly.** Every read goes through `sql_runner`, which means
  every read goes through the guard. There is no second path to add a check to later.
- **Two database roles, never merged.** `app_rw` owns agent state; `analyst_ro` runs every
  generated query. The tool layer is only ever handed the read-only DSN.
- **One module imports `anthropic`.** Nodes describe what they want; `agent/llm.py` owns model
  id, thinking, effort, caching, retries and refusal handling.

### The graph

```
intake → clarify_gate → resolve_metrics → plan → author_sql → execute → interpret
       → materiality_check → generate_hypotheses → [test each] → reconcile → synthesize
```

There is **no `interpret → synthesize` edge**. Every path to an answer passes the materiality
gate, which is what makes the multi-hypothesis requirement structural rather than aspirational.

---

## 3. The decisions that shaped it

### 3.1 Enforce in the graph, not the prompt

The requirement "test more than one plausible explanation" could have been a paragraph in the
system prompt. Instead: while a material finding has fewer than two hypotheses in a terminal
state, the edge to synthesis is *unavailable*. `materiality_check` sets the finding under
investigation, and the only edge out goes to hypothesis generation.

**Cost.** More graph, more state, and a loop guard — a finding for which the model cannot produce
two genuinely different explanations would otherwise be picked again forever. Once a finding has
as many hypotheses as it is allowed and still lacks two tested ones, the investigation moves on
and the answer must say the finding was not fully explained.

**Why it was worth it.** A prompt instruction is advice. An edge is a fact. The same reasoning
put four invariants into database CHECK constraints rather than application code: a constraint
cannot be forgotten by a node written six steps later, and two of them caught real bugs during
construction (§5).

### 3.2 Parse, do not match

`sql_guard` parses every statement with `sqlglot` and decides on node types. The alternative — a
keyword denylist — is bypassable by comments, casing, unicode escapes, nesting and statement
stacking.

Probing the parser *before* writing the validator is what set its shape:

```sql
WITH x AS (DELETE FROM analytics.orders RETURNING *) SELECT * FROM x
```

parses with a **`Select` at the root**. A root-statement-type check would have cleared a
deletion. Denied node types are therefore matched anywhere in the tree, and five variants of this
attack are in the suite.

**Cost.** A parser dependency, and rejecting anything that will not parse — which occasionally
refuses valid but exotic SQL.

**Residual.** A statement that is a legitimate `SELECT` but semantically wrong — a join that
double counts — is not a security problem and the guard says nothing about it. That is what the
accuracy graders are for, and it is the failure mode most likely to survive into production.

### 3.3 An enumerated operation set instead of executing model code

`python_analysis` exposes eight operations — describe, group_by, share_of_total,
period_over_period, rolling, correlation, top_n, linear_fit — each implemented in the tool and
validated against the frame's real columns. The obvious alternative is letting the model write
pandas and `exec`ing it in a sandbox.

**Cost.** Some analyses are not expressible, and the agent must fall back to writing different
SQL. This is a real limitation, not a hidden one.

**Why.** It removes an entire class of sandbox-escape risk permanently rather than defending
against it forever. No model-authored code executes anywhere in this system.

### 3.4 Three sensitivity tiers, not one

"Sensitive" turned out not to be one thing.

| Tier | Rule | Why |
|---|---|---|
| `direct_identifier` | restricted anywhere outside an approved aggregate, **including a WHERE filter** | filtering by an email address is a person-level lookup, not analysis |
| `pseudonymous` | only *projection* in the outermost select is restricted | grouping and joining on `customer_unique_id` is ordinary work — the approved repeat-customer metric needs exactly that |
| `precise_location` | aggregate yes, return no; `min`/`max` excluded | `min(lat)` returns a real observed coordinate, which is disclosure rather than aggregation |

A single blanket rule would have forced human approval on every run of the approved retention
metric, which is how a control gets switched off.

**Residual.** An aggregate over a very small group can still narrow identity. A minimum-group-size
rule is listed in §7.

### 3.5 A metric is not a blob of SQL

Each approved metric declares an aggregate expression, its tables, its filter, its date column,
and an allow-list of dimensions each carrying a reviewed SQL expression. The registry assembles
the statement; values travel as bound parameters.

The consequence is structural: **for an approved metric, no free text from the model reaches
SQL.** The model picks names, and names map to expressions a human wrote. A hostile filter value
stays a value.

**Cost.** Two of the twelve metrics do not fit the mould — one needs a per-person subquery, one a
window function — and carry their own statement under `shape: custom`. They are held to the same
bar differently: every rendered metric, custom or not, is asserted to pass the guard.

### 3.6 LangGraph, and the SDK underneath it

LangGraph was chosen for three things this project actually needs: edge conditions that can carry
policy, a Postgres checkpointer, and pauses that survive a process restart. The LLM calls go
through the official `anthropic` SDK directly rather than a LangChain wrapper, because prompt
caching of the large stable prefix, adaptive thinking, per-node effort tiers and structured
outputs are each a lever worth controlling, and the abstraction hides all four.

**Cost.** More wrapper code, and one documented `type: ignore`: mypy cannot bind LangGraph's
`add_node` overloads when the node arrives as a `Callable` alias, which is how it arrives because
the nodes are built by factories so tests can inject a scripted model. Verified with a minimal
repro rather than assumed.

### 3.7 Dependency injection so routing is testable without a key

Every node is a closure over an LLM and a tool registry. This is not a testing nicety — the
routing is where the policy lives, and routing has to be asserted deterministically rather than
through whatever a model says on the day. It is the reason 470 tests pass with no API key, and
the reason the recovery path could be tested at all: the graph object is discarded and rebuilt
between parking and resuming, which is what a process restart amounts to.

---

## 4. Security

Ten controls, layered so no single failure is sufficient. Full threat mapping in
[security-controls.md](security-controls.md).

| Control | Verified by |
|---|---|
| C1 read-only role | 29 assertions; every write raises SQLSTATE 25006 |
| C2 AST validation | 79 hostile queries rejected, no database needed |
| C3 object allowlist | 11 catalog / forbidden-schema cases |
| C4 sensitive columns | 14 escalation cases across three tiers |
| C5 resource limits | statement timeout fires; cost gate escalates |
| C6 injection containment | adversarial eval category; a real injection is seeded in the data |
| C7 budget caps | a spent budget produces a truncated answer, not an exception |
| C8 approval gates | 13 tests, including approval surviving a restart |
| C9 secret handling | nested dicts and exception text both scrubbed |
| C10 audit trail | rejected queries recorded, not only executed ones |

Two properties are worth stating plainly:

**Nothing runs without clearance.** A query executes only if the guard allowed it or a named human
approved it — and approval is checked against the **stored row**, not a flag. Four conditions
hold: the approval exists, belongs to this run, was approved by a person, and its recorded
fingerprint matches the statement being run. Passing a *pending* approval's id does not clear it;
swapping the statement after approval is refused. Consent was given to a text, not to a slot.

**The audit distinguishes who permitted what.** `sql_audit.verdict` separates `allowed` (the guard
cleared it) from `approved` (a human did), so a reviewer can see which. That distinction was
missing until running under an approval collided with the constraint forbidding execution of a
non-allowed query — the collision was the signal that the vocabulary was wrong, not that the
constraint was.

---

## 5. What went wrong, and what it taught

The bugs worth recording are the ones where a safeguard fired.

**Every monetary figure reached the model as a string.** Postgres returns `numeric` as `Decimal`,
which has neither `isoformat` nor `item`, so the coercion helper fell through to `str()` and
`sum(oi.price)` arrived as `"139184.93"`. The model would then compare numbers lexically or spend
a turn parsing them — exactly what the helper existed to prevent. Found by asserting the *types*
of the model-facing payload rather than eyeballing it.

**Foreign keys came back empty for every table.**
`information_schema.constraint_column_usage` only returns rows for tables the current user
*owns*, so as `analyst_ro` every table looked unrelated to every other — and the agent needs join
paths to write correct SQL. Rewritten against `pg_constraint`.

**Two state bugs of the same shape.** `resolved_metrics` and `hypotheses` both used append-only
reducers while nodes needed to *revise* entries, so a hypothesis sat in state twice — once
`proposed`, once terminal — and the "two tested hypotheses" gate would have counted the stale
one. Fixed with a `merge_by_id` reducer, and the fix is documented as the second occurrence so a
third is recognised faster.

**Node summaries were being silently discarded.** `repo.step` closed the step on the way out even
when the node had already closed it, overwriting the summary with NULL — emptying the one
human-readable column in the trace, which is most of what makes a run reviewable.

**Two database constraints were slightly wrong, and said so loudly.**
`hypotheses_require_a_test` demanded a query for any non-`proposed` status; a hypothesis whose
test would duplicate a sibling's is correctly `inconclusive` with no query of its own. Migration
002 narrows it: a query is required for a verdict that *claims* something, not for one that
declines to. `sql_audit_executed_implies_allowed` had no vocabulary for human-approved execution;
migration 003 added `approved`. In both cases the constraint failing was better than the
alternative, which was the run silently doing the wrong thing.

**A CTE could shadow a forbidden object — a real bypass.** CTE aliases must be excluded from the
catalog check, but the code skipped any table whose *name* matched an alias, so
`WITH pg_authid AS (SELECT 1) SELECT * FROM pg_catalog.pg_authid` was **allowed**. In SQL a
qualified name can never resolve to a CTE; only unqualified names are treated as CTE references
now. Caught by the hostile-query corpus.

**`count(*)` was read as a wildcard projection.** The `*` in `count(*)` is an `exp.Star`, so every
`count(*)` over a table holding a restricted column escalated. A guard that escalates ordinary
aggregation is a guard that gets disabled.

**`--validate` caught a bug in the evaluation suite itself** on its first run:
`corr(row_number() OVER (...), aov)` nests a window function inside an aggregate. Without that
mode the question would have graded every future run against nothing.

**One environment failure worth recording.** Mid-project the `D:` drive was deleted. It held Git
and the Python the virtualenv was built from, so every command failed at once and the shell died
with it. Recovered by rebuilding the virtualenv from another interpreter at the same version and
reinstalling Git; no repository history was lost, since `.git` lived on a different drive. Six
tests written minutes before were lost and rewritten. The lesson is not about drives: it is that
"the tests pass" is only a claim about the machine that ran them, which is the argument for the
containerised CI in §8.

---

## 6. Evaluation: what exists, and what it has not told us yet

**Built and verified.** 32 questions in six categories. Every reference query executes and passes
the guard. Three graders, each tested against runs whose correct score is known.

| Category | N | What it measures |
|---|---|---|
| factual | 8 | calculation accuracy against a hand-written reference query |
| comparison | 7 | multi-step correctness across periods and segments |
| diagnostic | 6 | does it test more than one explanation before concluding |
| ambiguous | 4 | does it stop and ask rather than guessing |
| out_of_scope | 3 | does it say the data cannot answer this |
| adversarial | 4 | does policy hold under pressure |

**Seven of the 32 must not be answered at all.** A suite of only answerable questions measures
fluency, not judgement: it would score an agent that invents a churn figure exactly as highly as
one that says no approved definition exists.

The safety grader produces a verdict rather than a score — **one violation fails the whole
suite**. A guard that holds for thirty-one questions and gives way on the thirty-second has not
held, and averaging that away would hide the only number here that matters.

The quality grader is deliberately split. The mechanical half needs no model and carries the
weight: did it stop to ask, did it test enough explanations, does every cited query exist. The
judged half is an LLM against a fixed rubric for what only reading can settle — whether two
explanations were genuinely different, whether the confidence is calibrated — reported separately
and never allowed to overturn the mechanical result. A model marking its own homework is worth
something, but not that much.

**Not measured.** No question has been run against a real model. Running the suite requires
`ANTHROPIC_API_KEY`; the harness, graders and report generation are all exercised, and the
out-of-scope questions were run end to end and correctly recorded as errored for the missing key.

So these remain open, and would be the first thing to establish:

- calculation accuracy on the 15 questions with reference numbers;
- whether ambiguous questions are actually deferred rather than guessed;
- whether the diagnostic six produce *distinct* hypotheses rather than two phrasings of one — the
  code catches near-verbatim duplicates and identical test SQL, but paraphrase is beyond it, and
  that is what the rubric is for;
- whether the seeded injection is reported rather than obeyed;
- token cost per question, and whether the cached prefix actually hits.

---

## 7. Known limitations

**Semantically wrong SQL passes.** The guard checks safety, not meaning. A join that fans out and
double counts is a valid `SELECT`. Detected by the accuracy graders, not prevented — and the most
likely way a wrong answer reaches a user.

**Distinctness is only partly enforceable in code.** Near-verbatim restatements and identical test
SQL are caught. Two genuinely different wordings of one idea are not, because lexical similarity
cannot see paraphrase. A test documents that limitation rather than papering over it.

**No aggregate suppression.** `count(*)` over a group of two still reveals a great deal. There is
no minimum-group-size rule.

**The enumerated Python operations bound what follow-up analysis is possible.** Cohort retention
curves, seasonal decomposition and anything genuinely bespoke are not expressible.

**Confidence is capped, never raised.** Structurally right, but it means a well-supported answer
following one inconclusive test cannot be reported as high confidence even when it deserves to be.

**Wall-clock budget resets on resume.** Deliberate — counting an hour spent waiting for a human
against the work budget would make approvals self-defeating — but it means total elapsed time is
not bounded across a run that pauses repeatedly.

**Single-tenant, single-user.** No authentication, no per-user data scoping, no row-level
security. Everyone who can reach the API sees everything.

**The dataset is not a real warehouse.** Schema and metrics are configuration rather than code, so
pointing this at real data is a configuration exercise — but the sensitive-column policy and the
metric definitions would both need a fresh review against it.

---

## 8. What production would still require

Roughly in the order it would matter.

**1 · Authentication and per-user scoping.** SSO, and row-level security so a question answers
only over data the asker may see. Today the API is open and the agent sees the whole schema.

**2 · A real secret store.** Credentials come from the environment. Production wants a vault with
rotation, and the DSNs should be short-lived.

**3 · Aggregate suppression.** A minimum group size on any aggregate over a sensitive column, and
a review of which quasi-identifiers — zip prefix, city — need the same treatment.

**4 · Cost governance.** Per-question and per-user token budgets, a spend dashboard, and an alert
before the bill rather than after. The caps exist; the visibility does not.

**5 · Result caching.** Identical questions re-run the whole investigation. A cache keyed on the
question plus the schema version would cut both cost and latency substantially.

**6 · Metric-definition drift monitoring.** A definition edited without re-running the evaluation
suite silently changes every future answer. The catalogue-staleness check is a start; a version
bump gate on the affected questions is what is needed.

**7 · A queue instead of background tasks.** Runs currently execute in FastAPI background tasks,
so a deploy mid-run loses the in-flight work — the checkpoint makes it *resumable*, but nothing
resumes it automatically. A worker queue with visible retries is the fix.

**8 · Concurrency limits on the read-only pool.** Ten simultaneous investigations would contend.
The role has a statement timeout but no connection cap of its own.

**9 · An on-call runbook.** What to do when the guard rejects everything, when the model refuses,
when approvals pile up. The trace makes diagnosis possible; nothing yet says what to do next.

**10 · Retention policy for stored frames and traces.** `sql_audit` holds every statement and
`charts` holds rendered PNGs. Both grow without bound and both may contain business-sensitive
detail.

---

## 9. Assessment

What the project set out to prove was that an analyst agent can be made *reviewable* — that a
conclusion can be traced to its evidence, that safety can be enforced outside the prompt, and
that an investigation can be made to test its own first answer. On those three, the mechanisms
are in place and tested: 79 hostile queries rejected without a database, a synthesis gate that
cannot be argued past, an approval that cannot be manufactured, and a finding that the database
itself refuses to store without evidence.

What it has not shown is how well the agent reasons, because that was never measurable here. The
evaluation suite is the instrument for it and the instrument is built, calibrated against known
inputs, and unused. Anyone continuing this work should set `ANTHROPIC_API_KEY`, run
`python -m evals.runner --all`, and treat the resulting report as the first real result — while
noting that the safety half of that report should read zero, and that a number other than zero is
the only outcome here that would invalidate the rest.
