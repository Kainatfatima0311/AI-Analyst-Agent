-- ---------------------------------------------------------------------------
-- Agent state, traces and audit. Schema `agent`, owned and used by app_rw only.
-- analyst_ro has no privileges here (asserted in tests/integration/test_readonly_role.py).
--
-- Two design-document invariants are enforced here as CHECK constraints rather than left to
-- application code, because a constraint cannot be forgotten by a future node:
--   * a finding must cite at least one query as evidence;
--   * a hypothesis cannot leave `proposed` without at least one test query.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS agent;

CREATE TABLE IF NOT EXISTS agent.schema_migrations (
    version      TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum     TEXT NOT NULL
);

-- --- runs ------------------------------------------------------------------

CREATE TABLE agent.runs (
    run_id              UUID PRIMARY KEY,
    thread_id           TEXT NOT NULL UNIQUE,
    question            TEXT NOT NULL,
    requested_by        TEXT,
    status              TEXT NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received', 'clarifying', 'investigating',
                                          'awaiting_approval', 'completed', 'failed',
                                          'truncated')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    duration_ms         INTEGER,
    iterations          INTEGER NOT NULL DEFAULT 0,
    queries_used        INTEGER NOT NULL DEFAULT 0,
    tokens_in           BIGINT  NOT NULL DEFAULT 0,
    tokens_out          BIGINT  NOT NULL DEFAULT 0,
    cache_read_tokens   BIGINT  NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(12, 6) NOT NULL DEFAULT 0,
    answer              JSONB,
    error               JSONB
);
COMMENT ON COLUMN agent.runs.thread_id IS
    'LangGraph thread key. Resuming after a restart or a delayed approval happens by this id.';
COMMENT ON COLUMN agent.runs.status IS
    'received, clarifying, investigating and awaiting_approval are resumable; completed, failed and truncated are terminal.';

CREATE INDEX idx_runs_status     ON agent.runs (status);
CREATE INDEX idx_runs_created_at ON agent.runs (created_at DESC);

-- --- node executions -------------------------------------------------------

CREATE TABLE agent.run_steps (
    step_id             UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,
    node                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'started'
                        CHECK (status IN ('started', 'ok', 'error', 'skipped', 'paused')),
    effort              TEXT,
    attempt             INTEGER NOT NULL DEFAULT 1,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    duration_ms         INTEGER,
    tokens_in           BIGINT NOT NULL DEFAULT 0,
    tokens_out          BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens   BIGINT NOT NULL DEFAULT 0,
    summary             TEXT,
    error               JSONB,
    UNIQUE (run_id, seq)
);
COMMENT ON COLUMN agent.run_steps.attempt IS
    'Retry counter. A retry storm is visible in the trace rather than hidden inside it.';

CREATE INDEX idx_run_steps_run ON agent.run_steps (run_id, seq);

-- --- tool calls ------------------------------------------------------------

CREATE TABLE agent.tool_calls (
    tool_call_id    UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    step_id         UUID REFERENCES agent.run_steps(step_id) ON DELETE SET NULL,
    tool            TEXT NOT NULL,
    arguments       JSONB NOT NULL,
    result_summary  JSONB,
    ok              BOOLEAN NOT NULL,
    refusal         TEXT,
    error           JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms     INTEGER
);
COMMENT ON COLUMN agent.tool_calls.refusal IS
    'Set when a tool declined rather than failed - a refusal is a result, not an error.';

CREATE INDEX idx_tool_calls_run  ON agent.tool_calls (run_id, started_at);
CREATE INDEX idx_tool_calls_tool ON agent.tool_calls (tool);

-- --- SQL audit -------------------------------------------------------------

CREATE TABLE agent.sql_audit (
    query_id            UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    step_id             UUID REFERENCES agent.run_steps(step_id) ON DELETE SET NULL,
    tool_call_id        UUID REFERENCES agent.tool_calls(tool_call_id) ON DELETE SET NULL,
    purpose             TEXT NOT NULL,
    sql_text            TEXT NOT NULL,
    rewritten_sql       TEXT,
    verdict             TEXT NOT NULL
                        CHECK (verdict IN ('allowed', 'rejected', 'escalated')),
    reasons             JSONB NOT NULL DEFAULT '[]'::jsonb,
    referenced_objects  JSONB NOT NULL DEFAULT '[]'::jsonb,
    sensitive_columns   JSONB NOT NULL DEFAULT '[]'::jsonb,
    estimated_cost      NUMERIC,
    executed            BOOLEAN NOT NULL DEFAULT false,
    row_count           INTEGER,
    truncated           BOOLEAN NOT NULL DEFAULT false,
    duration_ms         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Nothing may be recorded as executed unless the guard allowed it.
    CONSTRAINT sql_audit_executed_implies_allowed
        CHECK (NOT executed OR verdict = 'allowed')
);
COMMENT ON TABLE agent.sql_audit IS
    'Every query considered, including rejected and escalated ones. A run where the guard blocked three attempts is more informative than one where those attempts vanished.';

CREATE INDEX idx_sql_audit_run     ON agent.sql_audit (run_id, created_at);
CREATE INDEX idx_sql_audit_verdict ON agent.sql_audit (verdict);

-- --- approvals -------------------------------------------------------------

CREATE TABLE agent.approvals (
    approval_id      UUID PRIMARY KEY,
    run_id           UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    kind             TEXT NOT NULL
                     CHECK (kind IN ('expensive_query', 'sensitive_column',
                                     'budget_extension', 'export')),
    reason           TEXT NOT NULL,
    payload          JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected', 'timed_out')),
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    decision_reason  TEXT,
    -- A decision must record when it was made and, unless it timed out, who made it.
    CONSTRAINT approvals_decision_is_attributed
        CHECK (status = 'pending' OR (decided_at IS NOT NULL
               AND (status = 'timed_out' OR decided_by IS NOT NULL)))
);

CREATE INDEX idx_approvals_run     ON agent.approvals (run_id);
CREATE INDEX idx_approvals_pending ON agent.approvals (status) WHERE status = 'pending';

-- --- findings and hypotheses ----------------------------------------------

CREATE TABLE agent.findings (
    finding_id          UUID PRIMARY KEY,
    run_id              UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    statement           TEXT NOT NULL,
    material            BOOLEAN NOT NULL DEFAULT false,
    evidence_query_ids  UUID[] NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Design-document invariant: a finding may not be reported without evidence.
    CONSTRAINT findings_require_evidence
        CHECK (cardinality(evidence_query_ids) > 0)
);

CREATE INDEX idx_findings_run ON agent.findings (run_id);

CREATE TABLE agent.hypotheses (
    hypothesis_id    UUID PRIMARY KEY,
    run_id           UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    finding_id       UUID NOT NULL REFERENCES agent.findings(finding_id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    test_design      TEXT NOT NULL,
    test_query_ids   UUID[] NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'testing', 'supported',
                                       'refuted', 'inconclusive')),
    reasoning        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Design-document invariant: a hypothesis cannot leave `proposed` untested.
    CONSTRAINT hypotheses_require_a_test
        CHECK (status = 'proposed' OR cardinality(test_query_ids) > 0)
);

CREATE INDEX idx_hypotheses_run     ON agent.hypotheses (run_id);
CREATE INDEX idx_hypotheses_finding ON agent.hypotheses (finding_id, status);

-- --- charts ----------------------------------------------------------------

CREATE TABLE agent.charts (
    chart_id     UUID PRIMARY KEY,
    run_id       UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    query_id     UUID NOT NULL REFERENCES agent.sql_audit(query_id) ON DELETE CASCADE,
    chart_type   TEXT NOT NULL,
    title        TEXT,
    spec         JSONB NOT NULL,
    png          BYTEA,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE agent.charts IS
    'query_id is NOT NULL by design: a chart in the UI is always one click from its SQL.';

CREATE INDEX idx_charts_run ON agent.charts (run_id);

-- analyst_ro must never reach any of this.
REVOKE ALL ON ALL TABLES IN SCHEMA agent FROM PUBLIC;
