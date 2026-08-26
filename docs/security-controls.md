# Security Controls

Each control below states the threat it answers, how it is enforced, where it lives, and how it is
tested. Companion to [design-document.md](design-document.md) §8.

The guiding assumption: **the model is not a trusted component.** It may be wrong, it may be
steered by data it reads, and it may be steered by the person asking the question. Every control is
therefore enforced outside the prompt.

---

## 1. Layers

A generated statement must pass all four layers before any row is returned.

```
  model output
      │
  ┌───▼─────────────────────────────────────────────┐
  │ L1  Static validation   sql_guard/validator.py  │  parse → single SELECT → node allowlist
  └───┬─────────────────────────────────────────────┘
  ┌───▼─────────────────────────────────────────────┐
  │ L2  Policy              column_policy.py        │  object allowlist, sensitive columns
  └───┬─────────────────────────────────────────────┘
  ┌───▼─────────────────────────────────────────────┐
  │ L3  Cost gate           explain_gate.py         │  EXPLAIN, cost ceiling, LIMIT clamp
  └───┬─────────────────────────────────────────────┘
  ┌───▼─────────────────────────────────────────────┐
  │ L4  Runtime             analyst_ro role         │  read-only role, timeouts, row cap
  └───┬─────────────────────────────────────────────┘
      ▼  rows, plus an audit record either way
```

L4 is the one that holds if every layer above it fails, which is why it exists at the database
level rather than in application code.

---

## 2. Control catalogue

### C1 — Read-only database role

| | |
|---|---|
| **Threat** | The agent writes, deletes or alters warehouse data, whether through a validator bug, a novel syntax, or a prompt injection. |
| **Control** | All generated SQL runs as `analyst_ro`: `GRANT CONNECT` on the database, `USAGE` and `SELECT` on schema `analytics` only, `REVOKE ALL ON SCHEMA public FROM PUBLIC`, and `ALTER ROLE analyst_ro SET default_transaction_read_only = on`. A separate `app_rw` role owns agent state and is never handed to the tool layer. |
| **Where** | `db/init/01_roles.sql`, `db/init/03_grants.sql`, `db/engine.py` |
| **Test** | `scripts/smoke.py` and `tests/integration/test_readonly_role.py` assert that `INSERT`, `UPDATE`, `DELETE`, `CREATE` and `DROP` all raise as `analyst_ro`. |
| **Residual** | None for writes. Reads within `analytics` remain possible by design. |

### C2 — AST validation of every statement

| | |
|---|---|
| **Threat** | A destructive or exfiltrating statement reaches the database because a keyword filter was bypassed by comments, casing, unicode escapes, nesting or statement stacking. |
| **Control** | `sqlglot` parses the statement. Exactly one statement is permitted, and its root must be a `SELECT`, optionally wrapped in `WITH`. Every DDL, DML and DCL node is rejected, along with `COPY`, `CALL`, `DO`, `SET`, `RESET` and a function denylist (`pg_read_file`, `pg_ls_dir`, large-object functions, `dblink`, `pg_sleep`). Rejection is by **node type**, so unparseable input is rejected rather than passed through. |
| **Where** | `sql_guard/validator.py`, `sql_guard/policy.py` |
| **Test** | `tests/unit/test_sql_guard.py` — at least 60 hostile queries, including CTE-wrapped `DELETE`, `UPDATE ... RETURNING`, stacked statements, comment-terminated injections, and unicode-escaped keywords. A required CI gate. |
| **Residual** | A statement that is a legitimate `SELECT` but semantically wrong is not a security problem and is caught by the accuracy graders instead. |

### C3 — Object allowlist

| | |
|---|---|
| **Threat** | Reading outside the intended analytical surface — other schemas, system catalogs, or credential tables. |
| **Control** | Only tables in schema `analytics`, derived from the actual seeded schema rather than hard-coded. `pg_catalog` and `information_schema` are reachable through `schema_inspector` only, never through `sql_runner`. Cross-database access (`dblink`) is denied at C2. |
| **Where** | `sql_guard/policy.py`, `tools/schema_inspector.py` |
| **Test** | Rejection cases for `pg_catalog.pg_authid`, `information_schema.columns`, and any unlisted schema. |

### C4 — Sensitive column policy

| | |
|---|---|
| **Threat** | Personal data leaves the warehouse through an answer, a chart, or a trace — including by accident. |
| **Control** | Named columns (customer name, email, phone, street address, precise latitude and longitude, payment identifiers) cannot appear in a projection. Approved aggregates over them — for example `count(distinct customer_email)` — are allowed, because a count is not a disclosure. A sensitive projection is **never silently stripped**: it raises, and becomes approval point 2, so the reviewer sees exactly what was asked for. |
| **Where** | `sql_guard/column_policy.py`, and the `sensitive` flag on metric definitions |
| **Test** | Projection of each sensitive column is rejected; each approved aggregate is allowed; the escalation path is asserted end to end in `tests/integration/test_approvals.py`. |
| **Residual** | Aggregates over very small groups could still narrow identity. A minimum-group-size rule is listed as production work in the final report. |

### C5 — Resource limits

| | |
|---|---|
| **Threat** | A runaway query exhausts the database, or an unbounded result set exhausts the API. |
| **Control** | `statement_timeout` and `idle_in_transaction_session_timeout` set on the role; a `LIMIT` injected when absent and clamped when too large; `EXPLAIN` (never `ANALYZE`) run before execution with a cost ceiling above which the query is escalated for approval; cross joins with no join condition rejected. |
| **Where** | `db/init/01_roles.sql`, `sql_guard/explain_gate.py`, `sql_guard/validator.py` |
| **Test** | A `pg_sleep` query hits the timeout; a query with no `LIMIT` is rewritten; a deliberately expensive plan triggers escalation rather than execution. |

### C6 — Prompt-injection containment

| | |
|---|---|
| **Threat** | Text stored in the warehouse — a product name, a customer review — contains instructions that the model follows. Olist contains free-text reviews, so this is a live risk rather than a hypothetical one. |
| **Control** | Tool results are passed as data in user-role content and are **never** interpolated into the system prompt. The system prompt is a fixed, cached prefix. Even a fully obeyed injection cannot write, because writing requires both a privilege the connection lacks (C1) and a statement the validator rejects (C2), and cannot exfiltrate personal data, because projection is blocked (C4). |
| **Where** | `agent/llm.py` message construction, `agent/prompts/` |
| **Test** | An adversarial evaluation category includes a review-text injection attempting DDL and a sensitive-column dump; the expected outcome is refusal or escalation, and zero policy violations. |
| **Residual** | Injection can still waste budget or degrade answer quality. Budget caps (C7) bound the cost, and the trace makes the attempt visible. |

### C7 — Budget caps

| | |
|---|---|
| **Threat** | A loop or an adversarial question drives unbounded model and database spend. |
| **Control** | Hard caps on queries per run, hypotheses per finding, graph iterations, tokens, and wall clock. Exhaustion routes to a partial answer marked `truncated`, and an extension requires human approval (approval point 3). |
| **Where** | `agent/budget.py`, enforced at node entry and by graph edges |
| **Test** | A forced low cap produces a `truncated` run with a partial answer rather than an error or an unsupported conclusion. |

### C8 — Human approval for high-impact actions

| | |
|---|---|
| **Threat** | The agent is given broad permission for demo convenience and then takes an action nobody sanctioned. |
| **Control** | Four explicit gates — expensive query, sensitive column, budget extension, export or publish — implemented with LangGraph `interrupt()`. Each request persists with its full payload; the decision, decider and timing are recorded; a timeout auto-rejects with a recorded reason. There is no bypass flag and no blanket-approve mode. |
| **Where** | `agent/nodes/request_approval.py`, `api/routes/approvals.py`, table `approvals` |
| **Test** | `tests/integration/test_approvals.py` covers approve, reject and timeout, including a process restart between the pause and the decision. |

### C9 — Secret handling

| | |
|---|---|
| **Threat** | Credentials leak through the repository, a log line, an error message, or the trace the UI displays. |
| **Control** | Configuration comes only from the environment; `.env` is git-ignored and only `.env.example` is committed, with placeholder values. The two DSNs are separate settings, and the tool layer receives only the read-only one. DSNs are redacted in log output and in API error envelopes. |
| **Where** | `config.py`, `observability/logging.py`, `.gitignore` |
| **Test** | A unit test asserts that a rendered log line and a serialised error envelope contain no password substring. |

### C10 — Complete audit trail

| | |
|---|---|
| **Threat** | A conclusion cannot be verified, or an incident cannot be reconstructed. |
| **Control** | Every query considered — allowed, rejected or escalated — is written to `sql_audit` with its verdict, reasons, purpose, row count and timing. Every node execution and tool call is recorded. Every reported number carries the `query_id` that produced it, and the invariant that a finding must have non-empty evidence is asserted in code. |
| **Where** | `observability/audit.py`, `db/repository.py`, `GET /v1/runs/{id}/trace` |
| **Test** | An integration test walks a completed run and asserts that every number in the answer resolves to a stored query, and that rejected attempts are present in the audit. |

---

### C11 — Tenant isolation

Every tenant-scoped query filters by `organization_id` **in SQL**, not in the route. A row with no
organisation is treated as *not* ours, and another organisation's resource answers 404 rather than
403 — a 403 confirms the resource exists, and a sequence of those is an enumeration.

*Verified by* `tests/integration/test_tenancy.py`: two real organisations, checked from both
directions, across runs, traces, reports, exports, chart images, dashboard counts, data sources,
alerts and the audit trail. It has already caught a real hole (a saved report landing in the wrong
organisation).

### C12 — Credentials encrypted at rest

Data source configuration is stored as Fernet ciphertext with the key held **outside** the
database, and the `connection_config` column is not in the select list of any read the API uses.
What a caller sees is an allowlisted redaction; a withheld field is *named*, not starred out.

*Verified by* reading the column straight from the table and asserting the plaintext is absent, and
by asserting a tampered ciphertext fails to decrypt rather than decrypting to something plausible.

### C13 — Bearer secrets stored only as hashes

API keys and share tokens are SHA-256 hashes. Nothing needs to read them back, so nothing can —
including an operator with a database dump, who could otherwise *act as* a customer.

*Verified by* asserting a token never appears in any listing, that the prefix identifies without
revealing, and that removing a member revokes their keys in the same transaction.

### C14 — Append-only audit trail

`agent.audit_log` records the actor, the action, the target and a redacted detail. The repository
exposes no update or delete path — a trail somebody can edit is not one — and an unauthenticated
action is labelled as such so a demo cannot be mistaken for a person.

*Verified by* asserting the repository surface contains only `audit` and `audit_entries`, that a
credential never reaches the trail, and that the trail is isolated per organisation.

## 3. Threat-to-control matrix

| Threat | Primary control | Backstop |
|---|---|---|
| Data modification or deletion | C2 AST validation | C1 read-only role |
| Statement stacking / injection into SQL | C2 | C1 |
| Reading outside the analytical surface | C3 object allowlist | C1 grants |
| Personal data disclosure | C4 column policy | C8 approval gate |
| Local file or cross-database read | C2 function denylist | C1 grants |
| Denial of service against the database | C5 timeouts and cost gate | C7 budget caps |
| Prompt injection from warehouse text | C6 containment | C1, C2, C4 |
| Runaway cost | C7 budget caps | C8 approval for extension |
| Unsanctioned high-impact action | C8 approval gates | C10 audit |
| Credential leakage | C9 secret handling | C10 audit review |
| Unverifiable conclusion | C10 audit trail | Evidence invariant asserted in code |

---

## 4. Deliberately out of scope

These are real requirements for a production rollout and are listed in the final technical report
rather than implemented here: row-level security and per-user data scoping; multi-tenant isolation;
SSO and an authorisation model for who may ask what; a minimum-group-size rule for aggregates over
sensitive columns; secret management through a vault rather than environment variables; network
egress restrictions on the API container; and retention and deletion policy for stored result
frames.
