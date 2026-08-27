# Deployment Guide

Three ways to run this, in increasing order of how much you should trust it with real data. Each
section says what it is *for*, because the difference between them is not configuration — it is
which of the security controls are actually on.

---

## 1. One command, for a demo

```powershell
Copy-Item .env.example .env      # fill in the model key and the two database passwords
docker compose up
```

- **UI** → <http://localhost:8000/app/>
- **API docs** → <http://localhost:8000/docs>

Compose brings up `db` → `seed` (one-shot, exits 0) → `api`, which serves both the `/v1` endpoints
and the interface. There is no second UI container: same origin means no CORS to configure and one
process to run.

Ports are parameterised, so a collision does not mean editing the compose file:

```powershell
$env:API_PORT = "8010"; docker compose up
```

This is not hypothetical: on a machine already running something else on 8000, compose fails with
`Bind for 0.0.0.0:8000 failed: port is already allocated` and names the container holding it.
`API_PORT` moves both the API and the interface together, because they are one process.

**What is off in this mode.** `REQUIRE_AUTHENTICATION` defaults to `false`, so any request with no
key is the default organisation's owner. That is deliberate — a demo has to work without an
account — and it is why this mode is for a demo.

---

## 2. Local development

Postgres in a container, everything else on the host.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

docker compose up -d db          # roles and schema applied on first start
python scripts/migrate.py        # agent state tables, including tenancy
python scripts/seed_db.py        # dataset: local CSVs → Kaggle → synthetic
python scripts/smoke.py          # prove analyst_ro cannot write

make api                         # UI at /app/, docs at /docs
```

| Command | What it does |
|---|---|
| `make guard` | the 79-case hostile-query suite — the security regression net |
| `make test` | everything, with coverage |
| `make evals-validate` | check the evaluation suite itself; **needs no model key** |
| `make evals` | run the suite and write a timestamped report |
| `make catalog` | regenerate `docs/metrics-catalog.md` from the YAML |
| `make smoke` | assert the read-only role really is read-only |

`python scripts/migrate.py --status` lists what is applied and what is pending; `--check` exits 1
if anything is pending, which is what CI runs.

---

## 3. Serving more than one company

Everything above, plus five things that are not optional.

### 3.1 Turn authentication on

```
REQUIRE_AUTHENTICATION=true
```

Without it, anybody who can reach the port is the default organisation's owner. This is the single
most important line in this document.

### 3.2 Generate an encryption key, and keep it elsewhere

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it as `SECRETS_KEY`. Two rules:

- It belongs in a **secret manager**, not in `.env` on the host and not in the repository.
- It must **not** be in the same backup as the database. A key stored beside the ciphertext it
  protects is obfuscation: one dump would contain both halves.

Without it, registering a data source fails with `503` rather than storing credentials in the
clear. That is the intended behaviour, not a bug to work around.

There is **no key rotation path**: rotating it would require re-encrypting every stored config, and
that migration is not written. Decide the key before onboarding anybody.

### 3.3 Create real organisations and keys

```bash
# An organisation and its first owner
curl -X POST localhost:8000/v1/organizations \
     -H 'content-type: application/json' \
     -d '{"name":"Acme Analytics","owner_email":"owner@acme.example"}'

# A key for that owner (as that owner, once they have one — or seed the first key directly
# with tenancy.issue_api_key from a shell on the host)
curl -X POST localhost:8000/v1/team/keys \
     -H 'authorization: Bearer <existing-admin-key>' \
     -H 'content-type: application/json' \
     -d '{"name":"owner laptop"}'
```

The token comes back **once**. Only its hash is stored, so a lost key is replaced rather than
recovered.

Then: revoke any key ever issued for the default organisation. It exists to own the pre-tenancy
data, not to be used.

### 3.4 Terminate TLS in front of the service

Bearer tokens over plain HTTP are shared secrets in transit. The container speaks HTTP; put a
reverse proxy or a platform load balancer in front of it and redirect port 80.

### 3.5 Decide about public sharing

`GET /v1/shared/{token}` is the only unauthenticated read path in the system, and it is that way on
purpose — a public link that required a login would not be a public link. If public sharing is not
wanted, block that one path at the edge. Team-audience links still work, because they check
membership as well as the token.

---

## 4. Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | or `groq` |
| `ANTHROPIC_API_KEY` / `GROQ_API_KEY` | — | only the chosen provider's key is needed |
| `ANALYST_MODEL` / `GROQ_MODEL` | `claude-opus-5` / `openai/gpt-oss-120b` | |
| `GROQ_MAX_TOKENS` | 4096 | counts against the per-minute allowance *before* generating |
| `SECRETS_KEY` | — | **required** to register a data source |
| `REQUIRE_AUTHENTICATION` | `false` | **set to `true` in production** |
| `DB_RW_DSN` | — | `app_rw`; agent state only |
| `DB_RO_DSN` | — | `analyst_ro`; every generated query |
| `ALLOWED_SCHEMAS` | `analytics` | the object allowlist |
| `SQL_STATEMENT_TIMEOUT_MS` | 15000 | |
| `SQL_DEFAULT_ROW_LIMIT` / `SQL_MAX_ROW_LIMIT` | 5000 / 50000 | injected and clamped by the guard |
| `SQL_MAX_EXPLAIN_COST` | 5000000 | above this, escalate for approval |
| `MAX_QUERIES_PER_RUN` | 25 | |
| `MAX_TOKENS_PER_RUN` | 400000 | |
| `MAX_RUN_WALL_CLOCK_SECONDS` | 600 | |
| `APPROVAL_TIMEOUT_SECONDS` | 1800 | then auto-rejected, with a recorded reason |
| `API_PORT` | 8000 | the UI is on the same port at `/app/` |

---

## 5. Migrations

Plain SQL files under `db/migrations/`, applied in order and recorded with a checksum — so editing
one that has already run is *detected* rather than silently diverging.

| Migration | What it does |
|---|---|
| `001_agent_state` | runs, steps, tool calls, sql_audit, approvals, findings, hypotheses, charts |
| `002_inconclusive_needs_no_test` | a hypothesis that claims nothing needs no test query |
| `003_approved_verdict` | `approved` as a distinct verdict from `allowed` |
| `004_saved_reports` | reports as immutable snapshots |
| `005_organizations` | tenancy, keys, data sources, shares, alerts, audit; backfills existing rows into a default organisation |
| `006_audit_parameters` | records the values bound to a statement, so a parameterised query can be reproduced |

Migration 005 is the one to read before deploying an upgrade: it adds `NOT NULL` columns to `runs`
and `reports` and backfills them, so it is not instant on a large table.

---

## 6. Hosting it somewhere

The image is multi-stage and runs as a non-root user, and the stack is one process plus a
database. That makes the deployment options unremarkable, which is the point:

| Platform | Shape |
|---|---|
| **Hugging Face Spaces (Docker)** | free, no card; pair with a free Postgres (Neon, Supabase) |
| **Render** | one web service from the Dockerfile + their managed Postgres |
| **Fly.io / Railway / Koyeb** | same shape; set the environment variables as secrets |
| **Any VM** | `docker compose up -d` behind nginx or Caddy for TLS |

Two things to remember wherever it goes:

1. The seed step is a **one-shot** container. On a platform without one, run
   `python scripts/seed_db.py` once against the managed database.
1. The interface is **package data**, declared in `pyproject.toml`. It has to be, because the image
   installs the wheel rather than running from the checkout — see §8.
2. `/readyz` asserts that the read-only role really is read-only, so a misconfigured deployment
   fails readiness instead of serving unsafely. Point the platform's health check at it.

---

## 7. What to check after deploying

```bash
curl -s localhost:8000/healthz            # {"status":"ok"}
curl -s localhost:8000/readyz             # read_only_verified: true
curl -s localhost:8000/v1/me              # 401 if REQUIRE_AUTHENTICATION is on
python scripts/smoke.py                   # 29 assertions that analyst_ro cannot write
python scripts/migrate.py --check         # exits 1 if a migration is pending
```

If `/readyz` reports `read_only_verified: false`, stop. It means generated SQL would run with write
permission, which is the one failure mode this project exists to prevent.

---

## 8. A bug that only a real container run could find

The first genuine `docker compose up` of the finished stack served the API correctly and answered
**404 at `/app/`**. Locally the interface had always worked.

The cause: `pyproject.toml` declared `package-data` for the metric YAML and the prompt files but not
for `api/static/`. A wheel therefore contained only Python modules, and the container — which
installs the wheel into `/opt/venv` rather than running from the source tree — had no `static`
directory to serve. Every local check passed because a checkout has the files sitting right there.

That is the worst shape a bug can have, and it is worth stating for two reasons:

- **The claim it broke was the headline one.** "A clean clone plus `docker compose up` is the whole
  setup" was false for the part a user sees first.
- **No amount of unit testing would have caught it.** The fix is one line of packaging
  configuration; the *lesson* is that "it works on my machine" and "it works from a checkout" are
  the same sentence, and neither is "it works from the artefact you ship".

There is now a test asserting the declaration exists in `pyproject.toml` — asserted against the
configuration rather than the filesystem, because the filesystem is exactly what already looked
fine.
